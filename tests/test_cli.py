import io
import json
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import patch

from passnat.cli import dispatch, main
from passnat.core import Failure


class ExplodingStdin:
    def read(self, *args, **kwargs):
        raise AssertionError("unsupported auth login must not read stdin")


class OneShotStdin:
    # 登录只允许读一次标准输入；第二次读取说明流程有重复消费。
    def __init__(self, text):
        self.text, self.reads = text, 0

    def isatty(self):
        # 模拟管道输入：不是终端。
        return False

    def read(self, *args, **kwargs):
        self.reads += 1
        if self.reads > 1:
            raise AssertionError("stdin must be read exactly once")
        return self.text


class TtyStdin:
    # 模拟终端：登录必须走 getpass，绝不能调用 read 等待 EOF。
    def isatty(self):
        return True

    def read(self, *args, **kwargs):
        raise AssertionError("TTY login must not read stdin")


class AuthLoginTests(unittest.TestCase):
    def login_args(self):
        return Namespace(resource="auth", action="login")

    def test_unsupported_platform_fails_fast_without_reading_stdin(self):
        # 无内置安全存储的平台：必须在触碰 stdin 与网络前明确失败，避免静默等待 EOF。
        with patch("passnat.cli.credential_store_available", return_value=False), \
             patch("passnat.cli.sys.stdin", new=ExplodingStdin()), \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential") as store:
            with self.assertRaises(Failure) as found:
                dispatch(self.login_args())
        self.assertEqual(found.exception.code, "unsupported_platform")
        self.assertEqual(found.exception.exit_code, 3)
        self.assertIn("PASSNAT_SECRET_KEY", str(found.exception))
        client.assert_not_called()
        store.assert_not_called()

    def test_login_reads_stdin_once_authenticates_and_saves(self):
        stdin = OneShotStdin("SECRET-KEY\n")
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=stdin), \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential", return_value="macOS Keychain") as store:
            result = dispatch(self.login_args())
        self.assertEqual(stdin.reads, 1)
        self.assertEqual(result, {"authenticated": True, "storage": "macOS Keychain"})
        client.assert_called_once_with("SECRET-KEY")
        client.return_value.request.assert_called_once_with("/user/userInfo")
        store.assert_called_once_with("SECRET-KEY")

    def test_tty_login_uses_getpass_once_then_authenticates_and_saves(self):
        # macOS 终端直接运行：getpass 从控制终端无回显读取，不碰 stdin.read。
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=TtyStdin()), \
             patch("passnat.cli.getpass.getpass", return_value="SECRET-KEY\n") as prompt, \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential", return_value="macOS Keychain") as store:
            result = dispatch(self.login_args())
        self.assertEqual(result, {"authenticated": True, "storage": "macOS Keychain"})
        prompt.assert_called_once()
        self.assertIn("SecretKey", prompt.call_args.args[0])
        client.assert_called_once_with("SECRET-KEY")
        client.return_value.request.assert_called_once_with("/user/userInfo")
        store.assert_called_once_with("SECRET-KEY")

    def test_tty_empty_secret_is_rejected_before_network(self):
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=TtyStdin()), \
             patch("passnat.cli.getpass.getpass", return_value="  \n"), \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential") as store:
            with self.assertRaises(Failure) as found:
                dispatch(self.login_args())
        self.assertEqual(found.exception.code, "invalid_credential")
        client.assert_not_called()
        store.assert_not_called()

    def test_pipe_login_keeps_stdin_semantics_and_skips_getpass(self):
        stdin = OneShotStdin("SECRET-KEY\n")
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=stdin), \
             patch("passnat.cli.getpass.getpass") as prompt, \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential", return_value="macOS Keychain") as store:
            result = dispatch(self.login_args())
        self.assertEqual(stdin.reads, 1)
        prompt.assert_not_called()
        self.assertEqual(result, {"authenticated": True, "storage": "macOS Keychain"})
        client.assert_called_once_with("SECRET-KEY")
        store.assert_called_once_with("SECRET-KEY")

    def test_login_rejects_multiline_stdin_before_network(self):
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=io.StringIO("first\nsecond\n")), \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential") as store:
            with self.assertRaises(Failure) as found:
                dispatch(self.login_args())
        self.assertEqual(found.exception.code, "invalid_credential")
        client.assert_not_called()
        store.assert_not_called()

    def test_authentication_failure_saves_nothing_and_leaks_no_secret(self):
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=OneShotStdin("SECRET-KEY\n")), \
             patch("passnat.cli.Client") as client, \
             patch("passnat.cli.save_credential") as store:
            client.return_value.request.side_effect = Failure("authentication_failed", "认证失败（业务码 30001）", 2)
            with self.assertRaises(Failure) as found:
                dispatch(self.login_args())
        self.assertEqual(found.exception.code, "authentication_failed")
        self.assertNotIn("SECRET-KEY", str(found.exception))
        store.assert_not_called()

    def test_success_output_never_prints_the_secret(self):
        output = io.StringIO()
        with patch("passnat.cli.credential_store_available", return_value=True), \
             patch("passnat.cli.sys.stdin", new=OneShotStdin("SECRET-KEY\n")), \
             patch("passnat.cli.Client"), \
             patch("passnat.cli.save_credential", return_value="macOS Keychain"), \
             redirect_stdout(output):
            self.assertEqual(main(["auth", "login", "--stdin"]), 0)
        printed = json.loads(output.getvalue())
        self.assertEqual(printed["data"], {"authenticated": True, "storage": "macOS Keychain"})
        self.assertNotIn("SECRET-KEY", output.getvalue())


class HttpsMappingGuardTests(unittest.TestCase):
    # 直接 HTTPS 映射只适用于本地端口本身是 TLS；误用时必须在读凭证和发请求前拒绝。

    def ports_add(self, **overrides):
        values = dict(resource="ports", action="add", name="demo-https", tunnel="T1", protocol="https", local_tls=False, local_port=8000, remote_port=None, subdomain="demo", domain_id=7, apply=False)
        values.update(overrides)
        return Namespace(**values)

    def tunnels_create(self, **overrides):
        values = dict(resource="tunnels", action="create", name="demopreview", node_id=1, port_name="demo-tcp", local_port=443, subdomain="demo", domain_id=7, protocol="https", local_tls=False, caddy_tls_port=None, apply=False)
        values.update(overrides)
        return Namespace(**values)

    def test_ports_add_https_without_local_tls_is_rejected_before_credentials(self):
        with patch("passnat.cli.Client") as client, patch("passnat.cli.add_port") as add_port:
            with self.assertRaises(Failure) as found:
                dispatch(self.ports_add())
        self.assertEqual(found.exception.code, "https_requires_local_tls")
        self.assertEqual(found.exception.exit_code, 3)
        message = str(found.exception)
        self.assertIn("shared site", message)
        self.assertIn("preview up", message)
        self.assertIn("--local-tls", message)
        client.assert_not_called()
        add_port.assert_not_called()

    def test_tunnels_create_https_without_local_tls_is_rejected_before_credentials(self):
        with patch("passnat.cli.Client") as client, patch("passnat.cli.create_tunnel") as create_tunnel:
            with self.assertRaises(Failure) as found:
                dispatch(self.tunnels_create())
        self.assertEqual(found.exception.code, "https_requires_local_tls")
        self.assertEqual(found.exception.exit_code, 3)
        client.assert_not_called()
        create_tunnel.assert_not_called()

    def test_ports_add_https_with_explicit_local_tls_is_allowed(self):
        with patch("passnat.cli.Client"), patch("passnat.cli.add_port", return_value={"dry_run": True}) as add_port:
            dispatch(self.ports_add(local_tls=True))
        spec = add_port.call_args.args[3]
        self.assertEqual(spec["protocol"], "https")
        self.assertEqual(spec["remote_port"], 443)
        self.assertEqual(spec["local_port"], 8000)

    def test_tunnels_create_https_with_explicit_local_tls_is_allowed(self):
        with patch("passnat.cli.Client"), patch("passnat.cli.create_tunnel", return_value={"dry_run": True}) as create_tunnel:
            dispatch(self.tunnels_create(local_tls=True))
        spec = create_tunnel.call_args.args[4]
        self.assertEqual(spec["protocol"], "https")
        self.assertEqual(spec["remote_port"], 443)

    def test_tunnels_create_explicit_http_upstream_still_works(self):
        # 普通 HTTP 仍可用，但必须显式选择 --protocol http。
        with patch("passnat.cli.Client"), patch("passnat.cli.create_tunnel", return_value={"dry_run": True}) as create_tunnel:
            dispatch(self.tunnels_create(protocol="http", local_port=8000))
        spec = create_tunnel.call_args.args[4]
        self.assertEqual(spec["protocol"], "http")
        self.assertEqual(spec["remote_port"], 80)

    def test_tunnels_create_without_protocol_is_rejected_before_credentials(self):
        # 缺省不再静默写 HTTP：遗漏协议时必须在读凭证和发请求前拒绝。
        with patch("passnat.cli.Client") as client, patch("passnat.cli.create_tunnel") as create_tunnel:
            with self.assertRaises(Failure) as found:
                dispatch(self.tunnels_create(protocol=None))
        self.assertEqual(found.exception.code, "protocol_required")
        self.assertEqual(found.exception.exit_code, 3)
        message = str(found.exception)
        self.assertIn("--protocol http", message)
        self.assertIn("--caddy-tls-port", message)
        client.assert_not_called()
        create_tunnel.assert_not_called()

    def test_parser_has_no_default_protocol_for_tunnels_create(self):
        from passnat.cli import parser
        args = parser().parse_args(["tunnels", "create", "demo", "--node-id", "1", "--port-name", "p", "--local-port", "8000", "--subdomain", "demo", "--domain-id", "7"])
        self.assertIsNone(args.protocol)
        self.assertIsNone(args.caddy_tls_port)
        self.assertFalse(args.local_tls)

    def test_tunnels_create_protocol_requires_local_port(self):
        with patch("passnat.cli.Client") as client, patch("passnat.cli.create_tunnel") as create_tunnel:
            with self.assertRaises(Failure) as found:
                dispatch(self.tunnels_create(protocol="http", local_port=None))
        self.assertEqual(found.exception.code, "invalid_arguments")
        self.assertIn("--caddy-tls-port", str(found.exception))
        client.assert_not_called()
        create_tunnel.assert_not_called()

    def test_caddy_tls_port_maps_https_443_without_http_fallback(self):
        # 受管 Caddy：local=该 TLS 端口，remote=443，protocol=https，字段全部固定。
        with patch("passnat.cli.Client"), patch("passnat.cli.create_tunnel", return_value={"dry_run": True}) as create_tunnel:
            dispatch(self.tunnels_create(caddy_tls_port=18446, local_port=None, protocol=None))
        spec = create_tunnel.call_args.args[4]
        self.assertEqual(spec, {"local_ip": "127.0.0.1", "local_port": 18446, "protocol": "https", "remote_port": 443, "sub_domain": "demo", "domain_id": 7})

    def test_caddy_tls_port_dry_run_reports_correct_mapping(self):
        output = io.StringIO()
        with patch("passnat.cli.Client"), patch("passnat.cli.create_tunnel", return_value={"dry_run": True}) as create_tunnel, redirect_stdout(output):
            code = main(["tunnels", "create", "demopreview", "--node-id", "1", "--port-name", "demo-tcp", "--caddy-tls-port", "443", "--subdomain", "demo", "--domain-id", "7"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        spec = create_tunnel.call_args.args[4]
        self.assertEqual(spec["local_port"], 443)
        self.assertEqual(spec["protocol"], "https")
        self.assertEqual(spec["remote_port"], 443)

    def test_caddy_tls_port_rejects_combined_mapping_options(self):
        cases = ({"local_port": 443}, {"protocol": "https"}, {"local_tls": True})
        for overrides in cases:
            values = {"caddy_tls_port": 18446, "local_port": None, "protocol": None}
            values.update(overrides)
            with self.subTest(overrides=overrides), patch("passnat.cli.Client") as client, patch("passnat.cli.create_tunnel") as create_tunnel:
                with self.assertRaises(Failure) as found:
                    dispatch(self.tunnels_create(**values))
                self.assertEqual(found.exception.code, "conflicting_mapping_options")
                self.assertEqual(found.exception.exit_code, 3)
                client.assert_not_called()
                create_tunnel.assert_not_called()

    def test_caddy_tls_port_must_be_a_valid_port(self):
        with patch("passnat.cli.Client") as client, patch("passnat.cli.create_tunnel") as create_tunnel:
            with self.assertRaises(Failure) as found:
                dispatch(self.tunnels_create(caddy_tls_port=0, local_port=None, protocol=None))
        self.assertEqual(found.exception.code, "invalid_port")
        client.assert_not_called()
        create_tunnel.assert_not_called()

    def test_tcp_mapping_is_not_blocked(self):
        with patch("passnat.cli.Client"), patch("passnat.cli.add_port", return_value={"dry_run": True}) as add_port:
            dispatch(self.ports_add(protocol="tcp", local_port=5432, remote_port=15432, subdomain=None, domain_id=None))
        self.assertEqual(add_port.call_args.args[3]["protocol"], "tcp")

    def test_ports_add_http_or_https_rejects_conflicting_remote_port_before_credentials(self):
        for protocol, remote_port in (("http", 8443), ("https", 8080)):
            with self.subTest(protocol=protocol, remote_port=remote_port), \
                 patch("passnat.cli.Client") as client, \
                 patch("passnat.cli.add_port") as add_port:
                with self.assertRaises(Failure) as found:
                    dispatch(self.ports_add(protocol=protocol, local_tls=True, remote_port=remote_port))
                self.assertEqual(found.exception.code, "conflicting_mapping_options")
                self.assertEqual(found.exception.exit_code, 3)
                client.assert_not_called()
                add_port.assert_not_called()


class PlanTests(unittest.TestCase):
    # 分流决策必须纯本地：不读凭证、不联网、不写状态，且不替用户猜设备。

    def plan(self, **overrides):
        values = dict(resource="plan", device="same", protocol="https", apps=1)
        values.update(overrides)
        return Namespace(**values)

    def test_same_device_multiple_https_reuses_shared_base(self):
        result = dispatch(self.plan(apps=3))
        self.assertEqual(result["mode"], "shared_https")
        self.assertTrue(result["reuse_shared"])
        self.assertFalse(result["credentials_read"])
        self.assertFalse(result["network_accessed"])
        self.assertFalse(result["state_written"])
        steps = " ".join(result["steps"])
        self.assertIn("shared init NAME --tunnel TUNNEL_ID --tls-port TLS_PORT --apply", steps)
        self.assertIn("shared site", steps)
        self.assertIn("shared sync NAME --apply", steps)
        self.assertNotIn("--reload", steps)
        self.assertLess(steps.index("shared sync NAME --apply"), steps.index("shared run NAME proxy"))
        self.assertIn("--reload --apply", " ".join(result["notes"]))

    def test_same_device_single_https_does_not_auto_reuse(self):
        result = dispatch(self.plan(apps=1))
        self.assertEqual(result["mode"], "single_https")
        self.assertFalse(result["reuse_shared"])
        steps = " ".join(result["steps"])
        self.assertIn("--port-name NAME-tcp --caddy-tls-port 443", steps)
        self.assertIn("preview up NAME", steps)
        self.assertIn("--local-port APP_HTTP_PORT --public-port 443", steps)
        self.assertNotIn("--local-port PORT --public-port PORT", steps)

    def test_http_and_tcp_never_enter_shared_caddy(self):
        for protocol in ("http", "tcp"):
            with self.subTest(protocol=protocol):
                result = dispatch(self.plan(protocol=protocol))
                self.assertEqual(result["mode"], "protocol_tunnel")
                self.assertFalse(result["reuse_shared"])
                steps = " ".join(result["steps"])
                self.assertNotIn("shared site", steps)
                self.assertNotIn("shared sync", steps)
                self.assertIn(protocol, steps)

    def test_different_devices_are_independent_even_for_https(self):
        result = dispatch(self.plan(device="other", apps=3))
        self.assertEqual(result["mode"], "independent")
        self.assertFalse(result["reuse_shared"])
        self.assertIn("独立隧道", result["reason"])
        self.assertNotIn("shared site", " ".join(result["steps"]))

    def test_udp_is_rejected_as_unsupported_everywhere(self):
        with patch("passnat.cli.Client") as client:
            with self.assertRaises(Failure) as found:
                dispatch(self.plan(protocol="udp"))
        self.assertEqual(found.exception.code, "unsupported_protocol")
        self.assertEqual(found.exception.exit_code, 3)
        client.assert_not_called()
        for args in (HttpsMappingGuardTests().ports_add(protocol="udp"), HttpsMappingGuardTests().tunnels_create(protocol="udp")):
            with self.subTest(resource=args.resource), patch("passnat.cli.Client") as client:
                with self.assertRaises(Failure) as found:
                    dispatch(args)
                self.assertEqual(found.exception.code, "unsupported_protocol")
                client.assert_not_called()

    def test_plan_never_touches_credentials_network_or_state(self):
        output = io.StringIO()
        with patch("passnat.cli.Client") as client, redirect_stdout(output):
            self.assertEqual(main(["plan", "--device", "same", "--protocol", "https", "--apps", "2"]), 0)
        client.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["data"]["mode"], "shared_https")

    def test_plan_requires_explicit_device(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["plan", "--protocol", "https"]), 3)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")

    def test_schema_documents_reuse_policy_and_plan(self):
        result = dispatch(Namespace(resource="schema"))
        self.assertIn("plan", result["resources"])
        self.assertIn("udp", result["unsupported_protocols"])
        self.assertIn("same device", result["reuse_policy"])

    def test_shared_help_states_https_only(self):
        from passnat.cli import parser
        self.assertIn("不要进入此流程", parser().format_help())

    def test_preview_up_help_explains_443_and_non_443_mappings(self):
        from passnat.cli import parser
        help_io = io.StringIO()
        # 通过 subparsers 输出 preview 子命令帮助，确认说明 443 单映射与非 443 双映射。
        with self.assertRaises(SystemExit), patch("sys.stdout", help_io):
            parser().parse_args(["preview", "--help"])
        self.assertIn("443 用单个 HTTPS 映射", help_io.getvalue())


class ToolsInstallCaddyTests(unittest.TestCase):
    def test_install_caddy_dispatches_without_credentials(self):
        output = io.StringIO()
        result = {"installed": True, "path": "/managed/bin/caddy", "version": "2.11.9"}
        with patch("passnat.preview.install_caddy", return_value=result) as install, \
             patch("passnat.cli.Client") as client, redirect_stdout(output):
            code = main(["tools", "install-caddy"])
        self.assertEqual(code, 0)
        install.assert_called_once_with()
        client.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["data"], result)

    def test_install_caddy_failure_is_reported_with_exit_code(self):
        output = io.StringIO()
        with patch("passnat.preview.install_caddy", side_effect=Failure("unsupported_platform", "仅支持 Apple Silicon", 3)), redirect_stdout(output):
            code = main(["tools", "install-caddy"])
        self.assertEqual(code, 3)
        error = json.loads(output.getvalue())["error"]
        self.assertEqual(error["code"], "unsupported_platform")


class NetworkErrorOutputTests(unittest.TestCase):
    # 逃逸到 main 的网络异常必须折叠为稳定错误码，且不回显 URL、密钥或异常原文。

    def run_main(self, error):
        output = io.StringIO()
        with patch("passnat.cli.dispatch", side_effect=error), redirect_stdout(output):
            code = main(["schema"])
        return code, output.getvalue(), json.loads(output.getvalue())

    def test_url_error_returns_stable_network_error_without_leaking_url(self):
        secret_url = "https://user:SECRET@example.invalid/api?key=SECRET"
        code, raw, printed = self.run_main(urllib.error.URLError(secret_url))
        self.assertEqual(code, 1)
        self.assertFalse(printed["ok"])
        self.assertEqual(printed["error"]["code"], "network_error")
        self.assertNotIn("error_type", printed["error"])
        self.assertIn("对账", printed["error"]["message"])
        self.assertNotIn("example.invalid", raw)
        self.assertNotIn("SECRET", raw)

    def test_timeout_error_returns_stable_network_error(self):
        code, raw, printed = self.run_main(TimeoutError("timed out talking to https://example.invalid/?key=SECRET"))
        self.assertEqual(code, 1)
        self.assertEqual(printed["error"]["code"], "network_error")
        self.assertNotIn("error_type", printed["error"])
        self.assertNotIn("example.invalid", raw)
        self.assertNotIn("SECRET", raw)

    def test_http_error_is_reported_as_http_error_not_network_failure(self):
        error = urllib.error.HTTPError("https://example.invalid/?key=SECRET", 502, "Bad Gateway", {}, None)
        code, raw, printed = self.run_main(error)
        self.assertEqual(code, 1)
        self.assertEqual(printed["error"]["code"], "http_error")
        self.assertNotIn("error_type", printed["error"])
        self.assertNotIn("example.invalid", raw)
        self.assertNotIn("SECRET", raw)

    def test_unexpected_error_keeps_error_type_without_details(self):
        code, raw, printed = self.run_main(RuntimeError("SECRET detail"))
        self.assertEqual(code, 1)
        self.assertEqual(printed["error"]["code"], "internal_error")
        self.assertEqual(printed["error"]["error_type"], "RuntimeError")
        self.assertNotIn("SECRET", raw)


if __name__ == "__main__":
    unittest.main()
