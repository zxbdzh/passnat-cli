from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error

from . import __version__
from .core import Client, Failure, SCHEMA_VERSION, add_port, create_tunnel, credential_store_available, delete_port, filtered_config, mutation_lock, owned_path, port_number, redact, save_credential, write_private


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("invalid_arguments", message, 3)


def parser() -> argparse.ArgumentParser:
    root = Parser(prog="passnat", description="独立 PassNAT CLI；默认输出 JSON，写操作默认预演。")
    root.add_argument("--version", action="version", version=__version__)
    groups = root.add_subparsers(dest="resource", required=True)
    auth = groups.add_parser("auth", help="验证凭证或使用系统钥匙串/DPAPI 保存凭证").add_subparsers(dest="action", required=True)
    login = auth.add_parser("login")
    login.add_argument("--stdin", action="store_true", required=True)
    auth.add_parser("check")
    account = groups.add_parser("account", help="查询当前账号").add_subparsers(dest="action", required=True)
    account.add_parser("info")
    tunnels = groups.add_parser("tunnels", help="查询已有隧道").add_subparsers(dest="action", required=True)
    tunnels.add_parser("list")
    get = tunnels.add_parser("get")
    get.add_argument("--tunnel", required=True)
    create = tunnels.add_parser("create", help="创建独立隧道及首个映射；协议必须显式声明，不会默认写 HTTP")
    create.add_argument("name")
    create.add_argument("--node-id", required=True, type=int)
    create.add_argument("--port-name", required=True)
    create.add_argument("--local-port", type=int, help="本地映射端口；需同时显式给出 --protocol")
    create.add_argument("--subdomain", required=True)
    create.add_argument("--domain-id", required=True, type=int)
    create.add_argument("--protocol", choices=["http", "https", "udp"], default=None, help="必须显式选择：http 表示本地应用是普通 HTTP 上游；https 仅用于本地端口本身提供 TLS，且必须同时加 --local-tls；udp 当前不支持。不会默认 http")
    create.add_argument("--local-tls", action="store_true", help="确认 --local-port 本身是 TLS 监听（例如 Caddy 的 HTTPS 端口）；普通 HTTP 应用不要使用")
    create.add_argument("--caddy-tls-port", type=int, metavar="PORT", help="Caddy 的 TLS 监听端口：固定 local_port=PORT、protocol=https、remote_port=443，无 HTTP 回退；与 --local-port/--protocol/--local-tls 互斥。与 shared init --tls-port（共享网关初始化端口）不是同一个参数")
    create.add_argument("--apply", action="store_true")
    domains = groups.add_parser("domains", help="查询可用于 HTTP 映射的域名").add_subparsers(dest="action", required=True)
    domains.add_parser("list")
    tools = groups.add_parser("tools", help="安装校验过的预览代理").add_subparsers(dest="action", required=True)
    tools.add_parser("install-caddy")
    ports = groups.add_parser("ports", help="添加或回收本工具拥有的 HTTP/TCP 端口").add_subparsers(dest="action", required=True)
    listing = ports.add_parser("list")
    listing.add_argument("--tunnel", required=True)
    add = ports.add_parser("add")
    add.add_argument("name")
    add.add_argument("--tunnel", required=True)
    add.add_argument("--protocol", choices=["http", "https", "tcp", "udp"], required=True, help="http 表示普通 HTTP 上游；https 仅用于本地端口本身提供 TLS，且必须同时加 --local-tls；tcp 为裸 TCP；udp 当前不支持")
    add.add_argument("--local-tls", action="store_true", help="确认 --local-port 本身是 TLS 监听（例如 Caddy 的 HTTPS 端口）；普通 HTTP 应用不要使用")
    add.add_argument("--local-port", type=int, required=True)
    add.add_argument("--remote-port", type=int)
    add.add_argument("--subdomain")
    add.add_argument("--domain-id", type=int)
    add.add_argument("--apply", action="store_true")
    remove = ports.add_parser("delete")
    remove.add_argument("name")
    remove.add_argument("--apply", action="store_true")
    config = groups.add_parser("config", help="导出指定映射的私有 frpc 配置").add_subparsers(dest="action", required=True)
    export = config.add_parser("export")
    export.add_argument("name")
    export.add_argument("--tunnel", required=True)
    export.add_argument("--proxy-name", action="append", required=True)
    export.add_argument("--apply", action="store_true")
    preview = groups.add_parser("preview", help="创建可信 HTTPS 预览；不改应用代码").add_subparsers(dest="action", required=True)
    up = preview.add_parser("up", help="配置预览映射、frpc 与 Caddy（443 用单个 HTTPS 映射，其他公网端口用 HTTP 挑战和 TCP 映射）；默认只预演")
    up.add_argument("name")
    up.add_argument("--tunnel", required=True)
    up.add_argument("--local-port", type=int, required=True)
    up.add_argument("--public-port", type=int, required=True)
    up.add_argument("--challenge-port", type=int, default=18280)
    up.add_argument("--admin-port", type=int, default=54214)
    up.add_argument("--domain-id", type=int, required=True)
    up.add_argument("--subdomain", required=True)
    up.add_argument("--frpc")
    up.add_argument("--apply", action="store_true")
    run = preview.add_parser("run", help="前台运行，交给 Herdr 等终端管理")
    run.add_argument("name")
    run.add_argument("component", choices=["frpc", "proxy"])
    stat = preview.add_parser("status")
    stat.add_argument("name")
    down = preview.add_parser("down")
    down.add_argument("name")
    down.add_argument("--apply", action="store_true")
    shared = groups.add_parser("shared", help="仅 HTTPS 共享：统一隧道、Caddy TLS 与站点白名单；HTTP/TCP/UDP 不要进入此流程").add_subparsers(dest="action", required=True)
    initialize = shared.add_parser("init")
    initialize.add_argument("name")
    initialize.add_argument("--tunnel", required=True)
    initialize.add_argument("--tls-port", type=int, default=18446)
    initialize.add_argument("--admin-port", type=int, default=54216)
    initialize.add_argument("--challenge-port", type=int, default=18283)
    initialize.add_argument("--apply", action="store_true")
    site = shared.add_parser("site")
    site.add_argument("name")
    site.add_argument("site_name")
    site.add_argument("--subdomain", required=True)
    site.add_argument("--domain-id", type=int, required=True)
    site.add_argument("--upstream-port", type=int)
    site.add_argument("--root")
    site.add_argument("--file", action="append", default=[])
    site.add_argument("--apply", action="store_true")
    synchronize = shared.add_parser("sync")
    synchronize.add_argument("name")
    synchronize.add_argument("--reload", action="store_true")
    synchronize.add_argument("--apply", action="store_true")
    shared_run = shared.add_parser("run")
    shared_run.add_argument("name")
    shared_run.add_argument("component", choices=["proxy", "frpc"])
    shared_status = shared.add_parser("status")
    shared_status.add_argument("name")
    migration = shared.add_parser("migrate")
    migration.add_argument("name")
    migration.add_argument("--source", required=True)
    migration.add_argument("--site", required=True)
    migration.add_argument("--upstream-port", type=int, required=True)
    migration.add_argument("--apply", action="store_true")
    planner = groups.add_parser("plan", help="纯本地分流建议：不读凭证、不联网、不写状态")
    planner.add_argument("--device", choices=["same", "other"], required=True, help="应用是否都运行在当前这台设备；必须显式选择，CLI 不猜设备")
    planner.add_argument("--protocol", choices=["https", "http", "tcp", "udp"], required=True, help="要对外发布的协议；https 才可能复用共享 Caddy")
    planner.add_argument("--apps", type=int, default=1, help="同一设备上要发布的 HTTPS 应用数量；>=2 时复用共享底座")
    groups.add_parser("schema", help="查询稳定输出与退出码约定")
    return root


def read_secret_key() -> str:
    # TTY：从控制终端无回显读取，避免用户在终端直接运行时静默等待 stdin EOF。
    # 非 TTY：保留管道语义，只读一次到 EOF，由管道提供方负责关闭输入。
    raw = getpass.getpass("SecretKey（输入不回显）: ") if sys.stdin.isatty() else sys.stdin.read()
    key = raw.strip()
    if not key or "\n" in key or "\r" in key:
        raise Failure("invalid_credential", "SecretKey 不能为空，且只能包含一行。", 3)
    return key


def require_local_tls(protocol: str, local_tls: bool) -> None:
    # 平台把 protocol=https 解释为“本地端口本身是 TLS”。普通 HTTP 应用必须先经过
    # Caddy 终结 TLS，否则访客会直接连到明文端口，表现为协议错配。
    if protocol == "https" and not local_tls:
        raise Failure("https_requires_local_tls", "直接 HTTPS 映射要求本地端口本身提供 TLS；普通 HTTP 上游请使用 `passnat shared site --upstream-port` 或 `passnat preview up`，由本地 Caddy 终结 TLS。确认本地端口已是 TLS 监听时才加 --local-tls。", 3)


def require_supported_protocol(protocol) -> None:
    # 平台与 CLI 目前只实现 http/https/tcp。UDP 不能靠 shared/preview 假装可用，
    # 也不默认降级成 TCP，必须在读取凭证前明确拒绝。
    if protocol == "udp":
        raise Failure("unsupported_protocol", "PassNAT CLI 当前不支持 UDP 映射，平台协议只有 http、https、tcp。不要改用 shared 或 preview 绕过；需要 UDP 时请先确认平台能力，或改用 TCP 并自行在应用层处理。", 3)


def plan(device: str, protocol: str, apps: int) -> dict:
    # 纯本地分流：不读凭证、不访问网络、不写状态，只输出该走哪条路径。
    require_supported_protocol(protocol)
    if apps < 1:
        raise Failure("invalid_arguments", "--apps 至少为 1。", 3)
    base = dict(device=device, protocol=protocol, apps=apps, reuse_shared=False, local_only=True, credentials_read=False, network_accessed=False, state_written=False)
    if device == "other":
        return dict(base, mode="independent", reason="不同设备必须使用独立隧道与独立 frpc 身份，不要复用当前设备的共享底座。", steps=[
            "在目标设备安装并验证 CLI：passnat auth check",
            "需要 HTTPS 时在该设备单独执行 passnat preview up NAME --tunnel NEW_TUNNEL_ID --local-port APP_HTTP_PORT --public-port 443 --domain-id DOMAIN_ID --subdomain SUB --apply；需要 HTTP 时执行 passnat tunnels create NAME --node-id NODE_ID --port-name PORT_NAME --protocol http --local-port APP_HTTP_PORT --subdomain SUB --domain-id DOMAIN_ID --apply",
            "如果该设备确实有多个 HTTPS 应用，也只在该设备单独 init 一个共享底座，不复用当前设备的",
        ], notes=["不要复制当前设备的 frpc 配置、隧道 ID 或进程身份。", "同一本机端口号不算冲突；归属按 port_name 判定。"])
    if protocol == "https":
        if apps >= 2:
            return dict(base, mode="shared_https", reuse_shared=True, reason="同一设备、多个 HTTPS 应用：复用一条隧道、一个 Caddy、一个 frpc。", steps=[
                "首条映射指向 Caddy TLS 端口：passnat tunnels create NAME --node-id NODE_ID --port-name PORT_NAME --caddy-tls-port TLS_PORT --subdomain SUB --domain-id DOMAIN_ID --apply",
                "初始化共享底座（--tls-port 与上一步一致）：passnat shared init NAME --tunnel TUNNEL_ID --tls-port TLS_PORT --apply",
                "逐个登记应用：passnat shared site NAME APP --subdomain SUB --domain-id DOMAIN_ID --upstream-port APP_HTTP_PORT --apply",
                # 首次运行 Caddy 尚未启动，必须先 sync 再启动 proxy，之后新增站点才热重载。
                "首次写入并校验配置：passnat shared sync NAME --apply",
                "先 passnat shared run NAME proxy，确认 TLS 端口监听后再 passnat shared run NAME frpc",
            ], notes=[
                "不要再为第二个应用创建第二条隧道或第二个 frpc。",
                "底座已在运行后再新增站点时，才使用 passnat shared sync NAME --reload --apply 热重载。",
                "HTTP/TCP/UDP 不得进入这个 Caddy 共享流程。",
            ])
        return dict(base, mode="single_https", reason="单设备只发布一个 HTTPS 应用：使用独立预览，不要自动套用共享复用。", steps=[
            "先创建专用隧道并对齐初始端口名：passnat tunnels create TUNNEL_NAME --node-id NODE_ID --port-name NAME-tcp --caddy-tls-port 443 --subdomain SUB --domain-id DOMAIN_ID --apply",
            # local-port 是应用 HTTP 端口，public-port 是公网 HTTPS 端口，两者不能相同。
            "passnat preview up NAME --tunnel TUNNEL_ID --local-port APP_HTTP_PORT --public-port 443 --domain-id DOMAIN_ID --subdomain SUB --apply",
            "先 passnat preview run NAME proxy，再 passnat preview run NAME frpc",
        ], notes=["preview 与 shared 不能同时占用公网 80/443。", "不要因为本机还有别的应用就顺手建共享底座。"])
    if protocol == "http":
        return dict(base, mode="protocol_tunnel", reason="发布 HTTP（非 HTTPS）时不经过 Caddy 共享流程，按协议单独建映射。", steps=[
            "passnat tunnels create NAME --node-id NODE_ID --port-name PORT_NAME --protocol http --local-port PORT --subdomain SUB --domain-id DOMAIN_ID --apply",
        ], notes=["HTTP 映射明文对外，不要宣称已有可信 HTTPS。", "不要用 shared site 承载 HTTP 对外发布。"])
    return dict(base, mode="protocol_tunnel", reason="TCP 不经过 Caddy 共享流程，在已有独立隧道上登记裸 TCP 映射。", steps=[
        "passnat ports add PORT_NAME --tunnel TUNNEL_ID --protocol tcp --local-port LOCAL_PORT --remote-port REMOTE_PORT --apply",
    ], notes=["TCP 不会自动获得 TLS，也不进入 shared/preview 的 Caddy 流程。", "同一本机端口号不算冲突；归属按 port_name 判定。"])


def resolve_create_spec(args) -> dict:
    # tunnels create 会立刻写入首个映射；缺省协议曾把普通应用端口静默登记成 HTTP，
    # 在 Caddy 的 HTTPS 分享里留下一条错误的首映射。这里要求显式协议，
    # 并为 Caddy 的 TLS 监听提供一次写死全部字段的入口。
    if args.caddy_tls_port is not None:
        if args.local_port is not None or args.protocol is not None or args.local_tls:
            raise Failure("conflicting_mapping_options", "--caddy-tls-port 已完整声明 HTTPS 映射（本地端口=Caddy TLS 监听、远端 443、protocol=https），不能再与 --local-port、--protocol 或 --local-tls 组合。请只保留 --caddy-tls-port。", 3)
        port = port_number(args.caddy_tls_port)
        # 明确固定为 HTTPS：不回退到 HTTP，也不把应用 HTTP 端口当成 TLS。
        return {"local_ip": "127.0.0.1", "local_port": port, "protocol": "https", "remote_port": 443, "sub_domain": args.subdomain, "domain_id": args.domain_id}
    require_supported_protocol(args.protocol)
    if args.protocol is None:
        raise Failure("protocol_required", "必须显式选择映射协议：普通 HTTP 上游加 `--protocol http`；本地端口本身提供 TLS 时加 `--protocol https --local-tls`；Caddy 的 TLS 监听端口请改用 `--caddy-tls-port PORT`（自动使用 protocol=https、远端 443）。缺省不再静默创建 HTTP 映射。", 3)
    if args.local_port is None:
        raise Failure("invalid_arguments", "--protocol 映射需要 --local-port；若目标是 Caddy 的 TLS 监听端口，请改用 --caddy-tls-port。", 3)
    require_local_tls(args.protocol, args.local_tls)
    return {"local_ip": "127.0.0.1", "local_port": port_number(args.local_port), "protocol": args.protocol, "remote_port": 443 if args.protocol == "https" else 80, "sub_domain": args.subdomain, "domain_id": args.domain_id}


def dispatch(args):
    resource, action = args.resource, getattr(args, "action", None)
    if (resource, action) == ("ports", "add"):
        # 在任何凭证读取或网络请求之前拒绝误用。
        require_supported_protocol(args.protocol)
        require_local_tls(args.protocol, args.local_tls)
        if args.protocol in ("http", "https") and args.remote_port is not None:
            expected_remote = 80 if args.protocol == "http" else 443
            if args.remote_port != expected_remote:
                raise Failure("conflicting_mapping_options", f"{args.protocol.upper()} 映射固定使用远端端口 {expected_remote}，不能指定 --remote-port {args.remote_port}。", 3)
    create_spec = resolve_create_spec(args) if (resource, action) == ("tunnels", "create") else None
    if resource == "plan":
        return plan(args.device, args.protocol, args.apps)
    if resource == "shared":
        from . import shared
        if action == "run":
            return shared.run(args.name, args.component)
        client = Client()
        if action == "init":
            return shared.init(client, args.name, args.tunnel, args.tls_port, args.admin_port, args.challenge_port, args.apply)
        if action == "site":
            return shared.add_site(client, args.name, args.site_name, args.subdomain, args.domain_id, args.upstream_port, args.root, args.file, args.apply)
        if action == "sync":
            return shared.sync(client, args.name, args.apply, args.reload)
        if action == "migrate":
            return shared.migrate(client, args.name, args.source, args.site, args.upstream_port, args.apply)
        return shared.status(client, args.name)
    if resource == "schema":
        return {"schema_version": SCHEMA_VERSION, "exit_codes": {"0": "success", "1": "runtime_or_api", "2": "authentication", "3": "validation"}, "credentials": "PASSNAT_SECRET_KEY, macOS Keychain, or Windows DPAPI", "writes": "--apply required; no POST retry", "https_mapping": "tunnels create requires an explicit --protocol (http or https --local-tls); for a Caddy TLS listener use --caddy-tls-port PORT (local_port=PORT, protocol=https, remote_port=443, no HTTP fallback); HTTP upstreams should use shared site --upstream-port or preview up", "protocols": ["http", "https", "tcp"], "unsupported_protocols": ["udp"], "reuse_policy": "same device + multiple HTTPS apps => one shared tunnel/Caddy/frpc (shared site + shared sync --reload --apply); single HTTPS app => independent tunnels create/preview up; HTTP/TCP/UDP => independent protocol tunnel, never the shared Caddy; different devices => independent tunnel and frpc identity. Use `passnat plan --device same|other --protocol ... --apps N` for a local-only decision.", "resources": ["auth", "account", "tunnels", "domains", "ports", "config", "tools", "preview", "shared", "plan"]}
    if resource == "tools":
        from .preview import install_caddy
        return install_caddy()
    if resource == "auth" and action == "login":
        if not credential_store_available():
            # 先判平台，避免在 TTY 上无提示地阻塞等待 EOF，再暴露无法保存凭证。
            raise Failure("unsupported_platform", "当前系统没有内置的安全凭证存储，也不会读取标准输入。macOS 使用钥匙串、Windows 使用 DPAPI；Linux 请通过可信的本地凭证工具将 PASSNAT_SECRET_KEY 注入环境变量，再运行 passnat auth check 验证；不要在命令参数、脚本或聊天中粘贴真实密钥。", 3)
        key = read_secret_key()
        Client(key).request("/user/userInfo")
        return {"authenticated": True, "storage": save_credential(key)}
    if resource == "preview" and action in ("run", "status"):
        from . import preview
        return preview.run(args.name, args.component) if action == "run" else preview.status(args.name)
    client = Client()
    if resource == "auth":
        client.request("/user/userInfo")
        return {"authenticated": True}
    if resource == "account":
        return client.request("/user/userInfo")
    if resource == "tunnels":
        if action == "create":
            return create_tunnel(client, args.name, args.node_id, args.port_name, create_spec, args.apply)
        return client.request("/user/tunnel/list?page=1&size=100") if action == "list" else client.tunnel(args.tunnel)
    if resource == "domains":
        return client.request("/user/domain/list")
    if resource == "ports":
        if action == "list":
            return client.tunnel(args.tunnel)["proxy_info"]
        if action == "delete":
            return delete_port(client, args.name, args.apply)
        if args.protocol in ("http", "https") and (not args.subdomain or not args.domain_id):
            raise Failure("invalid_arguments", "HTTP/HTTPS 映射需要 --subdomain 和 --domain-id。", 3)
        if args.protocol == "tcp" and not args.remote_port:
            raise Failure("invalid_arguments", "TCP 映射需要 --remote-port。", 3)
        remote = 80 if args.protocol == "http" else 443 if args.protocol == "https" else args.remote_port
        spec = {"local_ip": "127.0.0.1", "local_port": args.local_port, "remote_port": remote, "protocol": args.protocol}
        if args.protocol in ("http", "https"):
            spec.update(sub_domain=args.subdomain, domain_id=args.domain_id)
        return add_port(client, args.name, args.tunnel, spec, args.apply)
    if resource == "config":
        target = owned_path("export-" + args.name, ".toml")
        if not args.apply:
            return {"dry_run": True, "path": str(target), "proxy_names": args.proxy_name}
        write_private(target, filtered_config(client.config(args.tunnel), set(args.proxy_name)))
        return {"path": str(target), "proxy_names": args.proxy_name, "secrets_printed": False}
    if resource == "preview":
        from . import preview
        if action == "down":
            return preview.down(client, args.name, args.apply)
        return preview.up(client, args.name, args.tunnel, args.local_port, args.public_port, args.challenge_port, args.admin_port, args.domain_id, args.subdomain, args.frpc, args.apply)
    raise Failure("unknown_command", "未知命令。", 3)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args(argv)
        if getattr(args, "apply", False):
            with mutation_lock():
                data = dispatch(args)
        else:
            data = dispatch(args)
        print(json.dumps({"ok": True, "data": redact(data), "meta": {"schema_version": SCHEMA_VERSION, "cli_version": __version__}}, ensure_ascii=False))
        return 0
    except Failure as error:
        print(json.dumps({"ok": False, "error": {"code": error.code, "message": str(error), "retryable": False}, "meta": {"schema_version": SCHEMA_VERSION}}, ensure_ascii=False))
        return error.exit_code
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": {"code": "interrupted", "message": "操作已中断。", "retryable": False}}))
        return 130
    except urllib.error.HTTPError:
        # 兜底：HTTPError 是 URLError 的子类，不能把服务端响应误报成网络故障。
        print(json.dumps({"ok": False, "error": {"code": "http_error", "message": "服务返回 HTTP 错误，未输出可能含凭据的详情。", "retryable": False}}, ensure_ascii=False))
        return 1
    except (urllib.error.URLError, TimeoutError):
        # 兜底捕获 Client 之外（如管理接口探测）逃逸的网络异常，不回显 URL、密钥或异常原文。
        print(json.dumps({"ok": False, "error": {"code": "network_error", "message": "网络请求失败；请先检查网络与本机服务。若为写操作，结果可能已受理，请先查询对账，不要直接重试。", "retryable": False}}, ensure_ascii=False))
        return 1
    except Exception as error:
        # 不把 HTTP 请求、凭据、私有配置或子进程原始异常展开到控制台。
        print(json.dumps({"ok": False, "error": {"code": "internal_error", "message": "操作失败，未输出可能含凭据的异常详情。", "error_type": type(error).__name__, "retryable": False}}, ensure_ascii=False))
        return 1
