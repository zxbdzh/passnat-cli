from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import Client, Failure, SCHEMA_VERSION, add_port, create_tunnel, delete_port, dpapi, filtered_config, mutation_lock, owned_path, redact, write_private


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("invalid_arguments", message, 3)


def parser() -> argparse.ArgumentParser:
    root = Parser(prog="passnat", description="独立 PassNAT CLI；默认输出 JSON，写操作默认预演。")
    root.add_argument("--version", action="version", version=__version__)
    groups = root.add_subparsers(dest="resource", required=True)
    auth = groups.add_parser("auth", help="验证凭证或使用 Windows DPAPI 保存凭证").add_subparsers(dest="action", required=True)
    login = auth.add_parser("login")
    login.add_argument("--stdin", action="store_true", required=True)
    auth.add_parser("check")
    account = groups.add_parser("account", help="查询当前账号").add_subparsers(dest="action", required=True)
    account.add_parser("info")
    tunnels = groups.add_parser("tunnels", help="查询已有隧道").add_subparsers(dest="action", required=True)
    tunnels.add_parser("list")
    get = tunnels.add_parser("get")
    get.add_argument("--tunnel", required=True)
    create = tunnels.add_parser("create", help="创建独立隧道及首个 HTTP 映射，避免抢占已有客户端")
    create.add_argument("name")
    create.add_argument("--node-id", required=True, type=int)
    create.add_argument("--port-name", required=True)
    create.add_argument("--local-port", required=True, type=int)
    create.add_argument("--subdomain", required=True)
    create.add_argument("--domain-id", required=True, type=int)
    create.add_argument("--protocol", choices=["http", "https"], default="https")
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
    add.add_argument("--protocol", choices=["http", "https", "tcp"], required=True)
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
    up = preview.add_parser("up", help="配置两个映射、frpc 与 Caddy；默认只预演")
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
    shared = groups.add_parser("shared", help="统一隧道、Caddy TLS 与站点白名单").add_subparsers(dest="action", required=True)
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
    groups.add_parser("schema", help="查询稳定输出与退出码约定")
    return root


def dispatch(args):
    resource, action = args.resource, getattr(args, "action", None)
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
        return {"schema_version": SCHEMA_VERSION, "exit_codes": {"0": "success", "1": "runtime_or_api", "2": "authentication", "3": "validation"}, "credentials": "PASSNAT_SECRET_KEY or Windows DPAPI", "writes": "--apply required; no POST retry", "resources": ["auth", "account", "tunnels", "domains", "ports", "config", "tools", "preview", "shared"]}
    if resource == "tools":
        from .preview import install_caddy
        return install_caddy()
    if resource == "auth" and action == "login":
        key = sys.stdin.read().strip()
        if not key or "\n" in key or "\r" in key:
            raise Failure("invalid_credential", "标准输入必须仅包含一把 SecretKey。", 3)
        Client(key).request("/user/userInfo")
        write_private(owned_path("credential", ".dpapi"), dpapi(key.encode("utf-8")))
        return {"authenticated": True, "storage": "Windows DPAPI current user"}
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
            spec = {"local_ip": "127.0.0.1", "local_port": args.local_port, "protocol": args.protocol, "remote_port": 443 if args.protocol == "https" else 80, "sub_domain": args.subdomain, "domain_id": args.domain_id}
            return create_tunnel(client, args.name, args.node_id, args.port_name, spec, args.apply)
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
    except Exception as error:
        # 不把 HTTP 请求、凭据、私有配置或子进程原始异常展开到控制台。
        print(json.dumps({"ok": False, "error": {"code": "internal_error", "message": "操作失败，未输出可能含凭据的异常详情。", "error_type": type(error).__name__, "retryable": False}}, ensure_ascii=False))
        return 1
