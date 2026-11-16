from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import shutil
import ssl
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .core import Client, Failure, add_port, delete_port, ensure_home, filtered_config, load_state, matches, mutation_lock, owned_path, save_state, write_private

CADDY_VERSION = "2.11.7"
CADDY_ASSET = f"caddy_{CADDY_VERSION}_windows_amd64.zip"
CADDY_RELEASE = f"https://github.com/caddyserver/caddy/releases/download/v{CADDY_VERSION}/"


def find_frpc(explicit: str | None = None) -> str:
    candidates = [explicit, os.environ.get("PASSNAT_FRPC"), shutil.which("frpc")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise Failure("frpc_missing", "请设置 PASSNAT_FRPC 或通过 --frpc 指定原版 frpc 程序。", 3)


def install_caddy() -> dict:
    if os.name != "nt":
        raise Failure("unsupported_platform", "当前自动安装只支持 Windows amd64，其他平台请设置 PASSNAT_CADDY。", 3)
    root = ensure_home() / "bin"
    root.mkdir(exist_ok=True)
    target = root / "caddy.exe"
    if target.exists():
        return {"installed": True, "path": str(target), "reused": True}
    try:
        with urllib.request.urlopen(CADDY_RELEASE + f"caddy_{CADDY_VERSION}_checksums.txt", timeout=40) as response:
            checksums = response.read().decode("utf-8")
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == CADDY_ASSET)
        with urllib.request.urlopen(CADDY_RELEASE + CADDY_ASSET, timeout=90) as response:
            data = response.read()
        algorithm = hashlib.sha512 if len(expected) == 128 else hashlib.sha256
        if algorithm(data).hexdigest() != expected:
            raise Failure("checksum_mismatch", "Caddy 下载文件校验失败。")
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read("caddy.exe")
        write_private(target, binary)
    except (urllib.error.URLError, OSError, StopIteration, zipfile.BadZipFile):
        raise Failure("download_failed", "无法下载并校验 Caddy 官方发行包。") from None
    return {"installed": True, "path": str(target), "version": CADDY_VERSION, "checksum_verified": True, "checksum_algorithm": algorithm().name}


def paths(name: str) -> tuple[Path, Path]:
    return owned_path("preview-" + name, ".frpc.toml"), owned_path("preview-" + name, ".Caddyfile")


def caddyfile(state: dict) -> str:
    # 使用域名的标准 80 端口完成证书校验，实际 HTTPS 入口可通过 TCP 映射使用高端口。
    return f'''{{
    admin 127.0.0.1:{state["admin_port"]}
    http_port {state["challenge_port"]}
    https_port {state["public_port"]}
    auto_https disable_redirects
}}

https://{state["domain"]}:{state["public_port"]} {{
    bind 127.0.0.1
    tls {{
        issuer acme {{
            {"disable_http_challenge" if state["public_port"] == 443 else "disable_tlsalpn_challenge"}
        }}
    }}
    reverse_proxy http://localhost:{state["local_port"]} {{
        flush_interval -1
        header_down Set-Cookie "^(.*)$" "$1; Secure"
    }}
}}

http://{state["domain"]} {{
    bind 127.0.0.1
    redir https://{state["domain"]}:{state["public_port"]}{{uri}} 308
}}
'''


def up(client: Client, name: str, tunnel_id: str, local_port: int, public_port: int, challenge_port: int, admin_port: int, domain_id: int, subdomain: str, frpc: str | None, apply: bool) -> dict:
    from .core import port_number
    for port in (local_port, public_port, challenge_port, admin_port, admin_port + 1):
        port_number(port)
    if len({local_port, public_port, challenge_port, admin_port, admin_port + 1}) != 5:
        raise Failure("port_conflict", "本地服务、TLS、挑战与两个管理端口必须互不相同。", 3)
    import re
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?", subdomain):
        raise Failure("invalid_domain", "预览子域名格式无效。", 3)
    domains = client.request("/user/domain/list")
    selected = next((item for item in domains if item["id"] == domain_id and item.get("status") == 1), None)
    if not selected:
        raise Failure("domain_unavailable", "指定域名不存在或未启用。", 3)
    domain = subdomain + "." + selected["domain"]
    target_tunnel = client.tunnel(tunnel_id)
    owned_ids = {record["port_id"] for suffix in ("http", "tcp") if (record := load_state(name + "-" + suffix)) and record.get("tunnel_id") == tunnel_id and record.get("port_id")}
    if any(row["id"] not in owned_ids for row in target_tunnel.get("proxy_info", [])):
        raise Failure("shared_tunnel_refused", "该隧道含有其他服务，禁止为预览启动第二个客户端；请新建独立隧道。", 3)
    public_origin = f"https://{domain}" + (f":{public_port}" if public_port != 443 else "")
    state = {"name": name, "tunnel_id": tunnel_id, "local_port": local_port, "public_port": public_port, "challenge_port": challenge_port, "admin_port": admin_port, "domain_id": domain_id, "subdomain": subdomain, "domain": domain, "url": f"{public_origin}/"}
    old = load_state(name, "preview-")
    tcp_record = load_state(name + "-tcp")
    can_change_rejected_port = old and old.get("status") == "preparing" and tcp_record and tcp_record.get("status") == "rejected" and not tcp_record.get("port_id")
    if old and any(old.get(key) != value for key, value in state.items() if key != "url" and not (can_change_rejected_port and key == "public_port")):
        raise Failure("preview_conflict", "同名预览已有其他参数，请使用新的预览名称。", 3)
    state = dict(old or {}, **state)
    state["public_ip"] = target_tunnel["node_info"]["remote_ip"]
    frpc_path, proxy_path = paths(name)
    http_spec = {"local_ip": "127.0.0.1", "local_port": challenge_port, "protocol": "http", "remote_port": 80, "sub_domain": subdomain, "domain_id": domain_id}
    tcp_spec = {"local_ip": "127.0.0.1", "local_port": public_port, "protocol": "https" if public_port == 443 else "tcp", "remote_port": public_port}
    if public_port == 443:
        tcp_spec.update(sub_domain=subdomain, domain_id=domain_id)
    if not apply:
        return {"dry_run": True, "preview": state, "http": add_port(client, name + "-http", tunnel_id, http_spec, False) if public_port != 443 else None, "tcp": add_port(client, name + "-tcp", tunnel_id, tcp_spec, False), "changes_application_source": False}
    state["frpc"] = find_frpc(frpc)
    state["caddy"] = str(Path(os.environ.get("PASSNAT_CADDY") or ensure_home() / "bin/caddy.exe").resolve())
    if not Path(state["caddy"]).is_file():
        raise Failure("caddy_missing", "请先运行 passnat tools install-caddy，或设置 PASSNAT_CADDY。", 3)
    # 记录每一步，部分成功时也能精确回收自己新增的端口，不误删隧道其他映射。
    state["status"] = "preparing"
    save_state(name, state, "preview-")
    http = add_port(client, name + "-http", tunnel_id, http_spec, True) if public_port != 443 else None
    tcp = add_port(client, name + "-tcp", tunnel_id, tcp_spec, True)
    state["proxy_names"] = ([http["port"]["proxy_name"]] if http else []) + [tcp["port"]["proxy_name"]]
    raw = filtered_config(client.config(tunnel_id), set(state["proxy_names"]))
    _, first_proxy = raw.split("[[proxies]]", 1)
    admin_password = state.get("frpc_admin_password") or __import__("secrets").token_urlsafe(30)
    state["frpc_admin_password"] = admin_password
    # 管理接口只供本机停止当前预览；原桌面客户端无此配置，也不会被重载或接管。
    # 原配置最后可能停留在 [transport.tls] 等表内，必须用显式表定义避免字段落入错误层级。
    import tomllib
    parsed = tomllib.loads(raw)
    if "webServer" in parsed:
        raise Failure("unsafe_config", "平台已提供管理接口，拒绝覆盖。", 3)
    common = raw.split("[[proxies]]", 1)[0].rstrip() + f'\n\n[webServer]\naddr = "127.0.0.1"\nport = {admin_port + 1}\nuser = "preview"\npassword = {json.dumps(admin_password)}\n\n'
    write_private(frpc_path, common + "[[proxies]]" + first_proxy)
    write_private(proxy_path, caddyfile(state))
    result = subprocess.run([state["frpc"], "verify", "-c", str(frpc_path)], capture_output=True, timeout=15)
    if result.returncode:
        raise Failure("invalid_config", "frpc 未通过预览配置校验，端口已记录可回收。")
    state["status"] = "configured"
    save_state(name, state, "preview-")
    return {"configured": True, "name": name, "url": state["url"], "frpc_config": str(frpc_path), "proxy_config": str(proxy_path), "run": [f"passnat preview run {name} proxy", f"passnat preview run {name} frpc"]}


def run(name: str, component: str) -> dict:
    state = load_state(name, "preview-")
    if not state:
        raise Failure("not_configured", "请先执行 preview up --apply。", 3)
    key = hashlib.sha256(state["tunnel_id"].encode()).hexdigest()[:16]
    with mutation_lock(f"run-{key}-{component}"):
        return _run(name, component)


def _run(name: str, component: str) -> dict:
    state = load_state(name, "preview-")
    if not state or state.get("status") not in ("configured", "running"):
        raise Failure("not_configured", "请先执行 preview up --apply。", 3)
    frpc_path, proxy_path = paths(name)
    env = os.environ.copy()
    if component == "proxy":
        env["XDG_DATA_HOME"] = str(ensure_home() / "caddy-data")
        env["XDG_CONFIG_HOME"] = str(ensure_home() / "caddy-config")
        command = [state["caddy"], "run", "--config", str(proxy_path), "--adapter", "caddyfile"]
    else:
        live = Client().tunnel(state["tunnel_id"])
        if any(row.get("proxy_name") not in state["proxy_names"] for row in live.get("proxy_info", [])):
            raise Failure("shared_tunnel_refused", "隧道出现非本预览映射，拒绝启动以免影响其他服务。", 3)
        if live.get("status") == 1:
            raise Failure("tunnel_in_use", "该隧道已有在线客户端，拒绝再次启动。", 3)
        command = [state["frpc"], "-c", str(frpc_path)]
    secret_values = [state.get("frpc_admin_password", "")]
    if component == "frpc":
        import tomllib
        cfg = tomllib.loads(frpc_path.read_text("utf-8"))
        def collect(value):
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, str) and len(value) >= 8:
                secret_values.append(value)
        collect(cfg.get("metadatas", {}))
        collect(cfg.get("auth", {}))
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env, shell=False)
    print(json.dumps({"event": "started", "component": component, "pid": child.pid, "name": name}), file=sys.stderr, flush=True)
    try:
        for line in child.stdout:
            for secret in secret_values:
                if secret:
                    line = line.replace(secret, "[REDACTED]")
            sys.stderr.write(line)
        code = child.wait()
        return {"stopped": True, "component": component, "exit_code": code}
    except KeyboardInterrupt:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        return {"stopped": True, "component": component}


def status(name: str) -> dict:
    state = load_state(name, "preview-")
    if not state:
        raise Failure("not_found", "未找到该预览。", 3)
    result = {"name": name, "state": state["status"], "url": state["url"], "tls_verified": False}
    address = state.get("public_ip") or Client().tunnel(state["tunnel_id"])["node_info"]["remote_ip"]
    if not ipaddress.ip_address(address).is_global:
        raise Failure("invalid_public_address", "平台节点不是公网地址，拒绝将本机直连判定为预览成功。")
    try:
        # 固定连接平台公网 IP，同时保留域名 SNI/Host 校验证书，避免本机 DNS 回环产生假阳性。
        with socket.create_connection((address, state["public_port"]), timeout=15) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=state["domain"]) as stream:
                origin = state["domain"] + (f':{state["public_port"]}' if state["public_port"] != 443 else "")
                stream.sendall(f"GET / HTTP/1.1\r\nHost: {origin}\r\nConnection: close\r\n\r\n".encode("ascii"))
                response = http.client.HTTPResponse(stream)
                response.begin()
                result.update(tls_verified=True, http_status=response.status, reachable=True, public_ip=address, tls_version=stream.version())
    except (OSError, TimeoutError, http.client.HTTPException):
        result["reachable"] = False
    return result


def down(client: Client, name: str, apply: bool) -> dict:
    state = load_state(name, "preview-")
    if not state:
        raise Failure("not_found", "未找到该预览。", 3)
    live = client.tunnel(state["tunnel_id"])
    if any(row.get("proxy_name") not in state.get("proxy_names", []) for row in live.get("proxy_info", [])):
        raise Failure("shared_tunnel_refused", "隧道中出现其他映射，拒绝自动下线。", 3)
    # 代理名不等于归属：同名映射可能已被改作其他服务，停进程或删隧道前须核对完整登记。
    records = {}
    for suffix in ("http", "tcp"):
        item = load_state(name + "-" + suffix)
        if item and item.get("tunnel_id") == state["tunnel_id"] and item.get("status") == "active":
            records[item.get("port_id")] = item
    for row in live.get("proxy_info", []):
        item = records.get(row.get("id"))
        if not item or row.get("proxy_name") != item.get("proxy_name") or not matches(row, item["spec"]):
            raise Failure("ownership_conflict", "预览映射未登记或参数已变化，拒绝自动下线。", 3)
    creation = None
    for path in ensure_home().glob("create-*.json"):
        item = json.loads(path.read_text("utf-8"))
        if item.get("tunnel_id") == state["tunnel_id"] and item.get("status") == "active" and item.get("name") == live.get("name"):
            creation = item
            break
    # 平台禁止删最后一条映射；只有本工具创建且仍纯属本预览的隧道才整体回收。
    if not creation and live.get("proxy_info"):
        raise Failure("last_port_protected", "平台不允许删除最后一个端口，且此隧道不是本工具创建；请在平台管理。", 3)
    planned = {"dry_run": not apply, "name": name, "remove_owned_tunnel": state["tunnel_id"], "port_ids": [row["id"] for row in live.get("proxy_info", [])]}
    if not apply:
        return planned
    # 先停止本工具独立 frpc，再回收本预览拥有的映射；不会停止桌面客户端。
    config, _ = paths(name)
    subprocess.run([state["frpc"], "stop", "-c", str(config)], capture_output=True, timeout=15)
    client.request("/user/tunnel/del?" + urllib.parse.urlencode({"tid": state["tunnel_id"]}), "DELETE")
    remaining = client.request("/user/tunnel/list?page=1&size=100")
    if any(item["tunnel_id"] == state["tunnel_id"] for item in remaining):
        raise Failure("reconciliation_required", "专用隧道下线结果尚未确认。")
    if creation:
        creation["status"] = "removed"
        save_state(creation["name"], creation, "create-")
    for suffix in ("http", "tcp"):
        port = load_state(name + "-" + suffix)
        if port:
            port["status"] = "removed"
            save_state(name + "-" + suffix, port)
    # Caddy 的管理接口只绑定回环；核对唯一域名后才允许关闭，避免端口重用误停其他实例。
    admin = f'http://127.0.0.1:{state["admin_port"]}'
    try:
        with urllib.request.urlopen(admin + "/config/", timeout=5) as response:
            cfg = response.read().decode("utf-8")
        if state["domain"] not in cfg:
            raise Failure("process_conflict", "本地代理身份不匹配，未停止该进程。")
        with urllib.request.urlopen(urllib.request.Request(admin + "/stop", data=b"", method="POST"), timeout=5):
            pass
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    state["status"] = "removed"
    save_state(name, state, "preview-")
    return {"removed": True, "name": name, "url": state["url"]}
