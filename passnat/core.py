from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from contextlib import contextmanager

API = "https://api.passnat.com"
SCHEMA_VERSION = "1"


class Failure(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def home() -> Path:
    return Path(os.environ.get("PASSNAT_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "PassNatCLI").resolve()


def ensure_home() -> Path:
    root = home()
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
        if os.name == "nt":
            # 生成的 frpc 配置包含平台连接凭据，单独目录只允许当前用户与 SYSTEM 访问。
            who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            sid = re.search(r"S-1-\d+(?:-\d+)+", who.stdout)
            if not sid:
                raise Failure("private_directory_failed", "无法识别当前 Windows 用户。")
            result = subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"*{sid[0]}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise Failure("private_directory_failed", "无法限制预览目录的访问权限。")
    return root


def owned_path(name: str, suffix: str = ".json") -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", name):
        raise Failure("invalid_name", "名称仅允许 1–48 位字母、数字、下划线和短横线。", 3)
    return ensure_home() / (name + suffix)


def write_private(path: Path, content: str | bytes) -> None:
    root = ensure_home()
    if not path.resolve().is_relative_to(root):
        raise Failure("unsafe_path", "敏感配置只能写入本工具的私有目录。", 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Failure("unsafe_path", "拒绝改写符号链接。", 3)
    handle, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state(name: str, prefix: str = "port-") -> dict | None:
    path = owned_path(prefix + name)
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def save_state(name: str, state: dict, prefix: str = "port-") -> None:
    write_private(owned_path(prefix + name), json.dumps(state, ensure_ascii=False, indent=2))


@contextmanager
def mutation_lock(name: str = "mutation"):
    # 锁由操作系统随进程释放；避免两个 CLI 同时通过查询后各发一次创建请求。
    path = owned_path(name, ".lock")
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"1")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Failure("operation_in_progress", "已有 CLI 写操作进行中，请稍后重试。") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def dpapi_available() -> bool:
    # 凭据只能由当前 Windows 用户的 DPAPI 加密保管；其他平台不落盘。
    return os.name == "nt"


def dpapi(value: bytes, decrypt: bool = False) -> bytes:
    if not dpapi_available():
        raise Failure("unsupported_platform", "凭证持久保存目前使用 Windows DPAPI；其他系统请设置 PASSNAT_SECRET_KEY。", 3)

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise Failure("credential_error", "无法处理当前 Windows 用户的加密凭证。", 2)
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


KEYCHAIN_SERVICE = "passnat-cli"
KEYCHAIN_ACCOUNT = "default"
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300

_framework = None


def keychain_available() -> bool:
    # macOS 使用系统钥匙串；条目由本进程读写，凭据不落明文文件。
    return sys.platform == "darwin"


def _security() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    # 绑定 Security.framework 的旧版钥匙串 API：直接传字节，不经过 argv 或命令行解析。
    global _framework
    if _framework is None:
        try:
            security = ctypes.CDLL(ctypes.util.find_library("Security") or "Security")
            corefoundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or "CoreFoundation")
        except OSError:
            raise Failure("credential_error", "无法加载 macOS 钥匙串接口，凭证未保存。", 2) from None
        pointer = ctypes.c_void_p
        security.SecKeychainAddGenericPassword.argtypes = [pointer, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, pointer, ctypes.POINTER(pointer)]
        security.SecKeychainFindGenericPassword.argtypes = [pointer, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, ctypes.c_uint32, pointer]
        security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        corefoundation.CFRelease.argtypes = [pointer]
        for function in (security.SecKeychainAddGenericPassword, security.SecKeychainFindGenericPassword, security.SecKeychainItemModifyAttributesAndData, security.SecKeychainItemFreeContent):
            function.restype = ctypes.c_int32
        corefoundation.CFRelease.restype = None
        _framework = (security, corefoundation)
    return _framework


def _keychain_release(item) -> None:
    if item:
        _security()[1].CFRelease(item)


def _keychain_add(payload: bytes) -> tuple[int, ctypes.c_void_p | None]:
    security, _ = _security()
    item = ctypes.c_void_p()
    buffer = ctypes.create_string_buffer(payload, len(payload))
    service, account = KEYCHAIN_SERVICE.encode(), KEYCHAIN_ACCOUNT.encode()
    status = security.SecKeychainAddGenericPassword(None, len(service), service, len(account), account, len(payload), ctypes.cast(buffer, ctypes.c_void_p), ctypes.byref(item))
    return status, item if item.value else None


def _keychain_find() -> tuple[int, ctypes.c_void_p | None, bytes | None]:
    security, _ = _security()
    item, length, data = ctypes.c_void_p(), ctypes.c_uint32(), ctypes.c_void_p()
    service, account = KEYCHAIN_SERVICE.encode(), KEYCHAIN_ACCOUNT.encode()
    status = security.SecKeychainFindGenericPassword(None, len(service), service, len(account), account, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
    if status:
        return status, None, None
    try:
        return status, item if item.value else None, ctypes.string_at(data, length.value)
    finally:
        security.SecKeychainItemFreeContent(None, data)


def _keychain_modify(item: ctypes.c_void_p, payload: bytes) -> int:
    security, _ = _security()
    buffer = ctypes.create_string_buffer(payload, len(payload))
    return security.SecKeychainItemModifyAttributesAndData(item, None, len(payload), ctypes.cast(buffer, ctypes.c_void_p))


def _keychain_message(status: int) -> str:
    # 只报告 Security 状态码，不回显任何凭据内容。
    return f"无法读写 macOS 钥匙串（Security 状态 {status}）。若钥匙串已锁定，请先解锁后重试。"


def keychain_store(key: str) -> None:
    payload = key.encode("utf-8")
    status, item = _keychain_add(payload)
    if status == ERR_SEC_DUPLICATE_ITEM:
        status, item, _ = _keychain_find()
        if status == 0:
            try:
                status = _keychain_modify(item, payload)
            finally:
                _keychain_release(item)
        item = None
    if status != 0:
        raise Failure("credential_error", _keychain_message(status), 2)
    if item is not None:
        _keychain_release(item)


def keychain_load() -> str | None:
    status, item, payload = _keychain_find()
    if status == ERR_SEC_ITEM_NOT_FOUND:
        return None
    if status != 0:
        raise Failure("credential_error", _keychain_message(status), 2)
    _keychain_release(item)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Failure("credential_error", "钥匙串中的凭证格式无效，请重新运行 passnat auth login --stdin。", 2) from None


def credential_store_available() -> bool:
    return dpapi_available() or keychain_available()


def load_stored_credential() -> str | None:
    if dpapi_available():
        path = home() / "credential.dpapi"
        return dpapi(path.read_bytes(), decrypt=True).decode("utf-8") if path.exists() else None
    if keychain_available():
        return keychain_load()
    return None


def save_credential(key: str) -> str:
    if dpapi_available():
        write_private(owned_path("credential", ".dpapi"), dpapi(key.encode("utf-8")))
        return "Windows DPAPI current user"
    if keychain_available():
        keychain_store(key)
        return "macOS Keychain"
    raise Failure("unsupported_platform", "当前系统没有内置的安全凭证存储，未保存凭证。", 3)


def credential() -> str:
    key = os.environ.get("PASSNAT_SECRET_KEY", "").strip()
    if not key:
        key = load_stored_credential() or ""
    if not key or "\r" in key or "\n" in key:
        raise Failure("authentication_required", "请设置 PASSNAT_SECRET_KEY，或使用 passnat auth login --stdin。", 2)
    return key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不把用户的平台凭据带到重定向目标，即使远端暂时返回不同 API 入口也明确失败。
        raise Failure("redirect_refused", "认证请求出现重定向，已停止以保护凭证。")


class Client:
    def __init__(self, key: str | None = None):
        self.key = credential() if key is None else key
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path: str, method: str = "GET", data: dict | None = None, text: bool = False):
        if not path.startswith("/user/") or ".." in path or "\\" in path:
            raise Failure("invalid_endpoint", "只允许固定 PassNAT 用户接口。", 3)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {"Authorization": self.key, "Accept": "application/json", "User-Agent": "passnat-cli/0.1"}
        # 无请求体的 DELETE 使用查询参数，声明 JSON 会触发服务端空体解析。
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise Failure("http_error", f"PassNAT 返回 HTTP {error.code}。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure("network_error", "连接 PassNAT 失败；写请求可能已受理，请先对账，不要盲目重试。") from None
        if text:
            # 配置下载返回 text/plain，内含连接凭据，调用者不得将它打印到 stdout。
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            if isinstance(parsed, dict) and parsed.get("code") != 10000:
                self.reject(parsed)
            raise Failure("invalid_response", "配置接口没有返回 TOML。")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise Failure("invalid_response", "PassNAT 返回了非 JSON 响应。") from None
        if not isinstance(payload, dict) or payload.get("code") != 10000:
            self.reject(payload if isinstance(payload, dict) else {})
        return payload.get("data")

    def reject(self, payload: dict):
        message = str(payload.get("msg", "PassNAT 请求失败。")).replace(self.key, "[REDACTED]")[:200]
        code = payload.get("code")
        raise Failure("authentication_failed" if code in (30001, 30002, 30003) else "api_error", f"{message}（业务码 {code}）", 2 if code in (30001, 30002, 30003) else 1)

    def tunnel(self, tunnel_id: str) -> dict:
        data = self.request("/user/tunnel/detail?" + urllib.parse.urlencode({"tunnel_id": tunnel_id}))
        if not isinstance(data, dict) or data.get("tunnel_id") != tunnel_id:
            raise Failure("invalid_response", "隧道身份不匹配。")
        return data

    def config(self, tunnel_id: str) -> str:
        return self.request("/user/tunnel/config?" + urllib.parse.urlencode({"tunnel_id": tunnel_id}), text=True)


def port_number(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= 65535:
        raise Failure("invalid_port", "端口必须在 1–65535 内。", 3)
    return value


def marker(tunnel: str, name: str) -> str:
    return "pn:" + hashlib.sha256((tunnel + ":" + name).encode()).hexdigest()[:12]


def matches(row: dict, spec: dict) -> bool:
    fields = ("local_ip", "local_port", "protocol", "remote_port", "remark")
    if not all(row.get(field) == spec.get(field) for field in fields):
        return False
    return not spec.get("sub_domain") or str(row.get("domain", "")).startswith(spec["sub_domain"] + ".")


def add_port(client: Client, name: str, tunnel_id: str, spec: dict, apply: bool) -> dict:
    owned_path("port-" + name)
    state = load_state(name)
    previous_marker = state.get("spec", {}).get("remark") if state and state.get("tunnel_id") == tunnel_id else None
    spec = dict(spec, remark=previous_marker or marker(tunnel_id, name))
    port_number(spec["local_port"])
    port_number(spec["remote_port"])
    if spec["protocol"] == "udp":
        raise Failure("unsupported_protocol", "PassNAT CLI 当前不支持 UDP 映射，平台协议只有 http、https、tcp。", 3)
    if spec["protocol"] not in ("tcp", "http", "https"):
        raise Failure("invalid_protocol", "当前支持 http、https 或 tcp 端口。", 3)
    # 本地归属冲突只依赖本地登记，先于远端查询返回；同时保留“平台明确拒绝且无 port_id”时换端口重新预演的能力。
    if state and state.get("status") == "rejected" and not state.get("port_id"):
        state = None
    if state and (state["tunnel_id"] != tunnel_id or state["spec"] != spec):
        raise Failure("state_conflict", "本地同名端口属于其他配置，请使用新名称。", 3)
    tunnel = client.tunnel(tunnel_id)
    rows = tunnel.get("proxy_info", [])
    if spec["protocol"] == "tcp":
        node = tunnel["node_info"]
        if not node["start_port"] <= spec["remote_port"] <= node["end_port"]:
            raise Failure("invalid_port", "远程端口不在当前节点允许范围内。", 3)
    if any(row.get("remark") == spec["remark"] and not matches(row, spec) for row in rows):
        raise Failure("ownership_conflict", "同名预览端口的参数已变化，拒绝覆盖。", 3)
    found = [row for row in rows if matches(row, spec)]
    if found:
        if len(found) != 1 or not state or found[0]["id"] in state.get("before_ids", []):
            raise Failure("ownership_conflict", "匹配的远程映射不属于本工具，拒绝接管。", 3)
        if apply:
            state.update(port_id=found[0]["id"], proxy_name=found[0]["proxy_name"], status="active")
            save_state(name, state)
        return {"reused": True, "port": found[0]}
    if state and state.get("status") in ("creating", "uncertain", "active"):
        raise Failure("reconciliation_required", "先前创建结果不确定或映射已被外部修改，请检查平台；不会自动再次创建。")
    payload = {"tunnel_id": tunnel_id, "port_relation": [spec]}
    if not apply:
        return {"dry_run": True, "method": "POST", "endpoint": "/user/tunnel/port/add", "request": payload}
    state = {"tunnel_id": tunnel_id, "spec": spec, "before_ids": [row["id"] for row in rows], "status": "creating"}
    save_state(name, state)
    try:
        client.request("/user/tunnel/port/add", "POST", payload)
    except Failure as error:
        state["status"] = "rejected" if error.code == "api_error" else "uncertain"
        save_state(name, state)
        if error.code == "api_error":
            raise
        # 超时不等于失败，先按唯一备注和完整参数查回结果，避免生成重复端口。
        try:
            matches_after = [row for row in client.tunnel(tunnel_id)["proxy_info"] if matches(row, spec) and row["id"] not in state["before_ids"]]
        except Failure:
            raise error
        if len(matches_after) != 1:
            raise error
    found = [row for row in client.tunnel(tunnel_id)["proxy_info"] if matches(row, spec) and row["id"] not in state["before_ids"]]
    if len(found) != 1:
        state["status"] = "uncertain"
        save_state(name, state)
        raise Failure("reconciliation_required", "平台已响应，尚无法唯一确认新端口，已保留对账记录。")
    state.update(port_id=found[0]["id"], proxy_name=found[0]["proxy_name"], status="active")
    save_state(name, state)
    return {"created": True, "port": found[0]}


def _reuse_created(client: Client, name: str, node_id: int, port_name: str, spec: dict, record: dict) -> dict:
    # 复用前先核对本地记录，再核对远端身份和初始映射；任一处不一致都不接管。
    tunnel_id = record["tunnel_id"]
    if record.get("name") != name or record.get("node_id") != node_id or record.get("port_name") != port_name:
        raise Failure("state_conflict", "本地创建记录属于其他隧道名称、节点或初始端口名，拒绝复用。", 3)
    recorded = record.get("spec") or {}
    if not recorded.get("remark"):
        raise Failure("reconciliation_required", f"已登记隧道 {tunnel_id} 缺少初始映射参数记录，无法核对；记录已保留，请先在平台核对。")
    if recorded != dict(spec, remark=recorded["remark"]):
        raise Failure("state_conflict", "本次创建参数与已登记记录不一致，拒绝复用。", 3)
    try:
        item = client.tunnel(tunnel_id)
    except Failure as error:
        if error.code in ("authentication_failed", "network_error", "http_error"):
            raise
        # 平台目前没有已确认的“隧道不存在”业务码；api_error / invalid_response 只说明无法核对，
        # 不构成删除证据，因此保留记录并交给人工对账，不自动清理。
        raise Failure("reconciliation_required", f"无法核对已登记隧道 {tunnel_id}（{error}）；这不证明远端已删除，本地记录已保留。请用同一账号执行 `passnat tunnels get --tunnel {tunnel_id}` 核对，只有平台明确证明该 ID 不存在后才按技能文档精确清理。")
    if item.get("tunnel_id") != tunnel_id or item.get("name") != name:
        raise Failure("ownership_conflict", "已登记的新隧道身份不匹配（ID 或名称被外部修改），拒绝接管。", 3)
    rows = item.get("proxy_info") or []
    if any(row.get("remark") == recorded["remark"] and not matches(row, recorded) for row in rows):
        raise Failure("ownership_conflict", "已登记隧道的初始映射参数已被外部修改，拒绝复用。", 3)
    found = [row for row in rows if matches(row, recorded)]
    if len(found) != 1:
        raise Failure("reconciliation_required", "已登记隧道中无法唯一确认初始映射；记录已保留，请先在平台核对，不会自动创建或删除。")
    port_state = load_state(port_name)
    if port_state and (port_state.get("tunnel_id") != tunnel_id or port_state.get("spec") != recorded or port_state.get("port_id") not in (None, found[0]["id"])):
        raise Failure("state_conflict", "本地初始端口记录与远端映射不一致，拒绝复用。", 3)
    # 远端隧道详情只提供 node_info（端口范围/地址），没有 node_id；节点只能按本地记录核对。
    return {"reused": True, "tunnel_id": tunnel_id, "name": name, "node_id": record["node_id"], "initial_port": found[0]}


def create_tunnel(client: Client, name: str, node_id: int, port_name: str, spec: dict, apply: bool) -> dict:
    if not name or len(name) > 12 or node_id <= 0:
        raise Failure("invalid_tunnel", "隧道名称需为 1–12 字符，并指定有效节点。", 3)
    owned_path("create-" + name)
    owned_path("port-" + port_name)
    record = load_state(name, "create-")
    if record and record.get("status") in ("removed", "rejected"):
        record = None
    # 本地预检先于任何远端查询：端口登记名是本地归属键，与监听端口号无关，
    # 所以共享流程里多个隧道复用同一个 local_port 不会被误判为冲突。
    port_state = load_state(port_name)
    owned_port = bool(record and record.get("tunnel_id") and record.get("port_name") == port_name and port_state and port_state.get("tunnel_id") == record["tunnel_id"])
    if port_state and not owned_port:
        raise Failure("state_conflict", f"初始端口登记名 {port_name} 已被其他隧道或配置占用；请在远端查询前更换 --port-name。若旧隧道已在平台删除，先用 `passnat tunnels get --tunnel <旧ID>` 核对，再按技能文档精确清理本地记录；不会自动删除。", 3)
    if record and not record.get("tunnel_id"):
        # 上次创建没有拿到可核对的 ID，先对账，不重发写请求。
        raise Failure("reconciliation_required", "上次创建隧道结果不确定，记录已保留，未再次创建；请先核对平台。")
    if record:
        return _reuse_created(client, name, node_id, port_name, spec, record)
    before = client.request("/user/tunnel/list?page=1&size=100")
    if any(item.get("name") == name for item in before):
        raise Failure("tunnel_exists", "已存在同名隧道且不属于本工具，请使用新名称。", 3)
    spec = dict(spec, remark=marker("new-" + name, port_name))
    payload = {"tunnel_name": name, "node_id": node_id, "port_relation": [spec]}
    if not apply:
        return {"dry_run": True, "method": "POST", "endpoint": "/user/tunnel/add", "request": payload}
    record = {"name": name, "node_id": node_id, "before_ids": [item["tunnel_id"] for item in before], "spec": spec, "port_name": port_name, "status": "creating"}
    save_state(name, record, "create-")
    error = None
    try:
        client.request("/user/tunnel/add", "POST", payload)
    except Failure as cause:
        error = cause
    after = client.request("/user/tunnel/list?page=1&size=100")
    created = [item for item in after if item["tunnel_id"] not in record["before_ids"] and item.get("name") == name]
    if len(created) != 1:
        record["status"] = "rejected" if error and error.code == "api_error" else "uncertain"
        save_state(name, record, "create-")
        raise error or Failure("reconciliation_required", "新隧道创建结果无法唯一确认，已保留对账记录。")
    item = created[0]
    ports = [row for row in item["proxy_info"] if matches(row, spec)]
    if len(ports) != 1:
        raise Failure("reconciliation_required", "新隧道初始端口参数与请求不一致。")
    row = ports[0]
    save_state(port_name, {"tunnel_id": item["tunnel_id"], "spec": spec, "before_ids": [], "status": "active", "port_id": row["id"], "proxy_name": row["proxy_name"]})
    record.update(status="active", tunnel_id=item["tunnel_id"])
    save_state(name, record, "create-")
    return {"created": True, "tunnel_id": item["tunnel_id"], "name": name, "node_id": node_id, "initial_port": row}


def delete_port(client: Client, name: str, apply: bool) -> dict:
    state = load_state(name)
    if not state or not state.get("port_id"):
        raise Failure("not_owned", "没有本工具创建的端口记录，拒绝删除。", 3)
    rows = client.tunnel(state["tunnel_id"])["proxy_info"]
    found = next((row for row in rows if row["id"] == state["port_id"]), None)
    if found and (not matches(found, state["spec"]) or found.get("proxy_name") != state["proxy_name"]):
        raise Failure("ownership_conflict", "映射已被其他操作修改，拒绝删除。", 3)
    if not apply:
        return {"dry_run": True, "port_id": state["port_id"], "already_removed": found is None}
    if found:
        client.request("/user/tunnel/port/del", "DELETE", {"id": state["port_id"]})
        if any(row["id"] == state["port_id"] for row in client.tunnel(state["tunnel_id"])["proxy_info"]):
            raise Failure("reconciliation_required", "删除结果尚未确认，请稍后查询。")
    state["status"] = "removed"
    save_state(name, state)
    return {"removed": True, "port_id": state["port_id"]}


def filtered_config(raw: str, names: set[str]) -> str:
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        raise Failure("invalid_config", "平台返回的配置不是有效 TOML。") from None
    if parsed.get("includes") or parsed.get("visitors") or parsed.get("start"):
        raise Failure("unsafe_config", "配置含额外 include、visitor 或启动选择，拒绝自动运行。", 3)
    blocks = re.split(r"(?m)^\s*\[\[proxies\]\]\s*$", raw)
    selected = []
    for block in blocks[1:]:
        proxy = tomllib.loads("[[proxies]]\n" + block)["proxies"][0]
        if proxy.get("name") in names:
            if proxy.get("plugin") or proxy.get("type") not in ("http", "https", "tcp"):
                raise Failure("unsafe_proxy", "仅允许直接 HTTP/HTTPS/TCP 代理，不执行配置中的插件。", 3)
            if proxy.get("localIP") not in ("127.0.0.1", "localhost", "::1"):
                raise Failure("unsafe_proxy", "预览代理必须指向本机回环地址。", 3)
            selected.append("[[proxies]]\n" + block.strip() + "\n")
    result = blocks[0].rstrip() + "\n\n" + "\n".join(selected)
    actual = {row["name"] for row in tomllib.loads(result).get("proxies", [])}
    if actual != names:
        raise Failure("proxy_not_found", "平台配置尚未包含全部指定代理。")
    return result


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(word in key.lower() for word in ("password", "secret", "token", "metadatas", "key", "http_pass")) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
