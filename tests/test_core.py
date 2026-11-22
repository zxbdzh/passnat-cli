import copy
import io
import json
import os
import tempfile
import tomllib
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from passnat.core import Client, Failure, NoRedirect, add_port, create_tunnel, credential, delete_port, filtered_config, keychain_load, keychain_store, load_state, load_stored_credential, marker, mutation_lock, owned_path, redact, save_credential, save_state, write_private
from passnat.preview import caddyfile, down, run, up


class FakeClient:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.next_error = None

    def tunnel(self, tunnel_id):
        return {"tunnel_id": tunnel_id, "name": "test", "status": 2, "node_info": {"start_port": 10000, "end_port": 60000, "remote_ip": "8.8.8.8"}, "proxy_info": copy.deepcopy(self.rows)}

    def request(self, path, method="GET", data=None, **kwargs):
        self.calls.append((path, method, data))
        if path == "/user/domain/list":
            return [{"id": 3, "domain": "example.com", "status": 1}]
        if method == "POST":
            spec = copy.deepcopy(data["port_relation"][0])
            row = dict(spec, id=101 + len(self.rows), proxy_name="preview-proxy", domain="test.example.com")
            self.rows.append(row)
            if self.next_error:
                raise self.next_error
        elif method == "DELETE":
            self.rows = [row for row in self.rows if row["id"] != data["id"]]
        return {}


class CreateClient:
    # 模拟 /user/tunnel/list、/user/tunnel/detail 与 /user/tunnel/add 的最小远端。
    def __init__(self, tunnels=None):
        self.tunnels = copy.deepcopy(tunnels or [])
        self.calls = []
        self.detail_error = None

    def tunnel(self, tunnel_id):
        self.calls.append(("detail", tunnel_id))
        if self.detail_error:
            raise self.detail_error
        for item in self.tunnels:
            if item["tunnel_id"] == tunnel_id:
                return copy.deepcopy(item)
        raise Failure("invalid_response", "隧道身份不匹配。")

    def request(self, path, method="GET", data=None, **kwargs):
        self.calls.append((path, method))
        if path.startswith("/user/tunnel/list"):
            return copy.deepcopy(self.tunnels)
        if path == "/user/tunnel/add":
            spec = copy.deepcopy(data["port_relation"][0])
            row = dict(spec, id=501, proxy_name="created-proxy", domain=(spec.get("sub_domain") or "tcp") + ".example.com")
            self.tunnels.append({"tunnel_id": "T-new", "name": data["tunnel_name"], "status": 2, "node_info": {"start_port": 10000, "end_port": 60000, "remote_ip": "8.8.8.8"}, "proxy_info": [row]})
            return {}
        raise AssertionError(f"unexpected request {method} {path}")


class PortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PASSNAT_HOME": self.temp.name})
        self.env.start()
        self.client = FakeClient()
        self.spec = {"local_ip": "127.0.0.1", "local_port": 3214, "protocol": "tcp", "remote_port": 43214}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_dry_run_never_posts_or_saves_creation(self):
        result = add_port(self.client, "sample", "new", self.spec, False)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.client.calls, [])
        self.assertIsNone(load_state("sample"))

    def test_duplicate_apply_reuses_same_owned_mapping(self):
        first = add_port(self.client, "sample", "new", self.spec, True)
        second = add_port(self.client, "sample", "new", self.spec, True)
        self.assertEqual(first["port"]["id"], second["port"]["id"])
        self.assertTrue(second["reused"])
        self.assertEqual(sum(method == "POST" for _, method, _ in self.client.calls), 1)

    def test_lost_response_reconciles_without_second_post(self):
        self.client.next_error = Failure("network_error", "lost")
        result = add_port(self.client, "sample", "new", self.spec, True)
        self.assertTrue(result["created"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(load_state("sample")["status"], "active")

    def test_preexisting_marker_is_never_adopted(self):
        self.client.rows = [dict(self.spec, id=4, proxy_name="foreign", remark=marker("new", "sample"))]
        with self.assertRaises(Failure) as found:
            add_port(self.client, "sample", "new", self.spec, True)
        self.assertEqual(found.exception.code, "ownership_conflict")

    def test_local_name_conflict_is_reported_before_remote_query(self):
        save_state("sample", {"tunnel_id": "other", "spec": dict(self.spec, remark="foreign"), "status": "active", "port_id": 5})
        for apply in (False, True):
            with self.subTest(apply=apply):
                client = FakeClient()
                with self.assertRaises(Failure) as found:
                    add_port(client, "sample", "new", dict(self.spec), apply)
                self.assertEqual(found.exception.code, "state_conflict")
                self.assertEqual(found.exception.exit_code, 3)
                self.assertEqual(client.calls, [])
                self.assertEqual(load_state("sample")["tunnel_id"], "other")

    def test_rejected_mapping_without_port_id_can_still_switch_local_port(self):
        save_state("sample", {"tunnel_id": "new", "spec": dict(self.spec, local_port=1111, remark=marker("new", "sample")), "status": "rejected", "before_ids": []})
        result = add_port(self.client, "sample", "new", self.spec, False)
        self.assertTrue(result["dry_run"])

    def test_uncertain_creation_does_not_retry(self):
        save_state("sample", {"tunnel_id": "new", "spec": dict(self.spec, remark=marker("new", "sample")), "status": "uncertain", "before_ids": []})
        with self.assertRaises(Failure) as found:
            add_port(self.client, "sample", "new", self.spec, True)
        self.assertEqual(found.exception.code, "reconciliation_required")
        self.assertFalse(self.client.calls)

    def test_delete_checks_remote_parameters(self):
        add_port(self.client, "sample", "new", self.spec, True)
        self.client.rows[0]["local_port"] = 22
        with self.assertRaises(Failure) as found:
            delete_port(self.client, "sample", True)
        self.assertEqual(found.exception.code, "ownership_conflict")
        self.assertFalse(any(method == "DELETE" for _, method, _ in self.client.calls))

    def test_delete_only_removes_owned_mapping(self):
        add_port(self.client, "sample", "new", self.spec, True)
        self.client.rows.append(dict(self.spec, id=9, proxy_name="unrelated", remark="keep"))
        delete_port(self.client, "sample", True)
        self.assertEqual([row["id"] for row in self.client.rows], [9])

    def test_invalid_path_is_rejected(self):
        with self.assertRaises(Failure):
            owned_path("../escape")
        with self.assertRaises(Failure):
            write_private(Path(self.temp.name).parent / "escape.toml", "secret")

    def test_mutations_cannot_run_concurrently(self):
        with mutation_lock():
            with self.assertRaises(Failure) as found:
                with mutation_lock():
                    self.fail("lock did not protect mutation")
            self.assertEqual(found.exception.code, "operation_in_progress")

    def test_preview_rejects_shared_tunnel_before_mutations(self):
        self.client.rows = [{"id": 1, "proxy_name": "company-service"}]
        with self.assertRaises(Failure) as found:
            up(self.client, "shared", "company", 3214, 443, 18280, 54214, 3, "test", None, True)
        self.assertEqual(found.exception.code, "shared_tunnel_refused")
        self.assertFalse(any(method == "POST" for _, method, _ in self.client.calls))

    def test_second_live_client_is_blocked(self):
        save_state("running", {"status": "configured", "tunnel_id": "test", "proxy_names": ["owned"]}, "preview-")
        with patch("passnat.preview.Client") as factory:
            factory.return_value.tunnel.return_value = {"status": 1, "proxy_info": [{"proxy_name": "owned"}]}
            with self.assertRaises(Failure) as found, patch("subprocess.Popen") as process:
                run("running", "frpc")
            self.assertEqual(found.exception.code, "tunnel_in_use")
            process.assert_not_called()

    def test_down_checks_full_registered_mapping_before_stopping(self):
        result = add_port(self.client, "owned-tcp", "test", self.spec, True)
        save_state("owned", {"status": "configured", "tunnel_id": "test", "proxy_names": [result["port"]["proxy_name"]]}, "preview-")
        save_state("test", {"status": "active", "tunnel_id": "test", "name": "test"}, "create-")
        original = copy.deepcopy(self.client.rows[0])
        for field, value in (("id", 999), ("local_port", 22), ("remote_port", 49999), ("protocol", "http"), ("local_ip", "192.0.2.1"), ("remark", "changed")):
            for apply in (False, True):
                with self.subTest(field=field, apply=apply):
                    self.client.rows = [dict(original, **{field: value})]
                    with patch("subprocess.run") as process:
                        with self.assertRaises(Failure) as found:
                            down(self.client, "owned", apply)
                        self.assertEqual(found.exception.code, "ownership_conflict")
                        process.assert_not_called()
        self.assertFalse(any(method == "DELETE" for _, method, _ in self.client.calls))

    def test_down_rejects_same_name_without_active_port_record(self):
        save_state("owned", {"status": "configured", "tunnel_id": "test", "proxy_names": ["owned"]}, "preview-")
        self.client.rows = [{"id": 2, "proxy_name": "owned"}]
        with self.assertRaises(Failure) as found:
            down(self.client, "owned", False)
        self.assertEqual(found.exception.code, "ownership_conflict")

    def test_down_accepts_unchanged_owned_mapping_in_dry_run(self):
        result = add_port(self.client, "owned-tcp", "test", self.spec, True)
        save_state("owned", {"status": "configured", "tunnel_id": "test", "proxy_names": [result["port"]["proxy_name"]]}, "preview-")
        save_state("test", {"status": "active", "tunnel_id": "test", "name": "test"}, "create-")
        with patch("subprocess.run") as process:
            plan = down(self.client, "owned", False)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["port_ids"], [result["port"]["id"]])
            process.assert_not_called()

    def test_preview_url_is_not_tied_to_a_specific_application(self):
        result = up(self.client, "sample", "test", 8000, 443, 18280, 54214, 3, "test", None, False)
        self.assertEqual(result["preview"]["url"], "https://test.example.com/")

    def test_down_refuses_foreign_proxy_before_stopping_anything(self):
        save_state("owned", {"status": "configured", "tunnel_id": "test", "proxy_names": ["owned"]}, "preview-")
        self.client.rows = [{"id": 2, "proxy_name": "other"}]
        with patch("subprocess.run") as process:
            with self.assertRaises(Failure) as found:
                down(self.client, "owned", True)
            self.assertEqual(found.exception.code, "shared_tunnel_refused")
            process.assert_not_called()


class CreateTunnelTests(unittest.TestCase):
    SPEC = {"local_ip": "127.0.0.1", "local_port": 8000, "protocol": "http", "remote_port": 80, "sub_domain": "demo", "domain_id": 7}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PASSNAT_HOME": self.temp.name})
        self.env.start()
        self.spec = dict(self.SPEC, remark=marker("new-demo", "demo-tcp"))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def registered(self):
        # 已成功创建并登记的隧道：create 记录、port 记录与远端详情三方一致。
        item = {"tunnel_id": "T1", "name": "demo", "status": 2, "node_info": {"start_port": 10000, "end_port": 60000, "remote_ip": "8.8.8.8"},
                "proxy_info": [dict(self.spec, id=11, proxy_name="created-proxy", domain="demo.example.com")]}
        record = {"name": "demo", "node_id": 1, "port_name": "demo-tcp", "spec": self.spec, "before_ids": [], "status": "active", "tunnel_id": "T1"}
        port_state = {"tunnel_id": "T1", "spec": self.spec, "before_ids": [], "status": "active", "port_id": 11, "proxy_name": "created-proxy"}
        save_state("demo", record, "create-")
        save_state("demo-tcp", port_state)
        return item, record

    def test_taken_port_name_conflicts_before_remote_query(self):
        save_state("demo-tcp", {"tunnel_id": "T-other", "spec": dict(self.spec, local_port=9999), "status": "active", "port_id": 3})
        for apply in (False, True):
            with self.subTest(apply=apply):
                client = CreateClient()
                with self.assertRaises(Failure) as found:
                    create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), apply)
                self.assertEqual(found.exception.code, "state_conflict")
                self.assertEqual(found.exception.exit_code, 3)
                self.assertIn("demo-tcp", str(found.exception))
                self.assertEqual(client.calls, [])
                self.assertIsNone(load_state("demo", "create-"))
                self.assertEqual(load_state("demo-tcp")["tunnel_id"], "T-other")

    def test_same_local_port_on_another_tunnel_is_not_a_conflict(self):
        save_state("other-tcp", {"tunnel_id": "T-other", "spec": dict(self.spec, remark="foreign"), "status": "active", "port_id": 3})
        client = CreateClient()
        result = create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), False)
        self.assertTrue(result["dry_run"])
        self.assertEqual([path for path, _ in client.calls], ["/user/tunnel/list?page=1&size=100"])

    def test_caddy_https_dry_run_payload_keeps_local_tls_port_and_public_443(self):
        # --caddy-tls-port 的完整预演：local=该 TLS 端口、protocol=https、remote=443，无 HTTP 回退。
        spec = {"local_ip": "127.0.0.1", "local_port": 443, "protocol": "https", "remote_port": 443, "sub_domain": "demo", "domain_id": 7}
        client = CreateClient()
        result = create_tunnel(client, "demo", 1, "caddy-tcp", spec, False)
        self.assertTrue(result["dry_run"])
        mapping = result["request"]["port_relation"][0]
        self.assertEqual(mapping["local_ip"], "127.0.0.1")
        self.assertEqual(mapping["local_port"], 443)
        self.assertEqual(mapping["protocol"], "https")
        self.assertEqual(mapping["remote_port"], 443)
        self.assertEqual(mapping["remark"], marker("new-demo", "caddy-tcp"))
        self.assertFalse(any(method == "POST" for _, method in client.calls))

    def test_reuses_registered_tunnel_when_all_parameters_match(self):
        item, record = self.registered()
        client = CreateClient([item])
        result = create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertTrue(result["reused"])
        self.assertEqual(result["tunnel_id"], "T1")
        self.assertEqual(result["initial_port"]["id"], 11)
        self.assertFalse(any(method == "POST" for _, method in client.calls))
        self.assertEqual(load_state("demo", "create-")["status"], "active")

    def test_reuse_rejects_changed_request_parameters_before_remote(self):
        item, record = self.registered()
        client = CreateClient([item])
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC, local_port=8001), True)
        self.assertEqual(found.exception.code, "state_conflict")
        self.assertEqual(client.calls, [])
        self.assertEqual(load_state("demo", "create-")["tunnel_id"], "T1")

    def test_reuse_rejects_changed_node_or_port_name(self):
        for kwargs in ({"node_id": 2}, {"port_name": "other-tcp"}):
            with self.subTest(**kwargs):
                item, record = self.registered()
                client = CreateClient([item])
                values = dict(name="demo", node_id=1, port_name="demo-tcp", spec=dict(self.SPEC), apply=True)
                values.update(kwargs)
                with self.assertRaises(Failure) as found:
                    create_tunnel(client, values["name"], values["node_id"], values["port_name"], values["spec"], values["apply"])
                self.assertEqual(found.exception.code, "state_conflict")
                self.assertEqual(client.calls, [])

    def test_reuse_rejects_remote_rename(self):
        item, record = self.registered()
        item["name"] = "renamed"
        client = CreateClient([item])
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertEqual(found.exception.code, "ownership_conflict")
        self.assertFalse(any(method == "POST" for _, method in client.calls))
        self.assertEqual(load_state("demo", "create-")["tunnel_id"], "T1")

    def test_reuse_rejects_changed_initial_mapping(self):
        item, record = self.registered()
        item["proxy_info"][0]["local_port"] = 9000
        client = CreateClient([item])
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertEqual(found.exception.code, "ownership_conflict")

    def test_reuse_requires_a_unique_initial_mapping(self):
        item, record = self.registered()
        item["proxy_info"] = []
        client = CreateClient([item])
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertEqual(found.exception.code, "reconciliation_required")
        self.assertEqual(load_state("demo", "create-")["status"], "active")

    def test_detail_api_error_is_reconciliation_not_deletion(self):
        item, record = self.registered()
        client = CreateClient([item])
        client.detail_error = Failure("api_error", "业务码 40000", 1)
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertEqual(found.exception.code, "reconciliation_required")
        message = str(found.exception)
        self.assertIn("T1", message)
        self.assertIn("passnat tunnels get --tunnel T1", message)
        self.assertIn("不证明远端已删除", message)
        self.assertFalse(any(method == "POST" for _, method in client.calls))
        self.assertEqual(load_state("demo", "create-")["status"], "active")

    def test_detail_network_error_is_preserved(self):
        item, record = self.registered()
        client = CreateClient([item])
        client.detail_error = Failure("network_error", "网络请求失败。", 1)
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertEqual(found.exception.code, "network_error")
        self.assertEqual(load_state("demo", "create-")["tunnel_id"], "T1")

    def test_uncertain_record_reconciles_before_any_remote_call(self):
        save_state("demo", {"name": "demo", "node_id": 1, "port_name": "demo-tcp", "spec": self.spec, "before_ids": [], "status": "uncertain"}, "create-")
        client = CreateClient()
        with self.assertRaises(Failure) as found:
            create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertEqual(found.exception.code, "reconciliation_required")
        self.assertEqual(client.calls, [])
        self.assertEqual(load_state("demo", "create-")["status"], "uncertain")

    def test_apply_creates_tunnel_and_registers_initial_port(self):
        client = CreateClient()
        result = create_tunnel(client, "demo", 1, "demo-tcp", dict(self.SPEC), True)
        self.assertTrue(result["created"])
        self.assertEqual(result["tunnel_id"], "T-new")
        self.assertEqual([method for _, method in client.calls].count("POST"), 1)
        self.assertEqual(load_state("demo-tcp")["port_id"], 501)
        self.assertEqual(load_state("demo-tcp")["spec"], self.spec)
        self.assertEqual(load_state("demo", "create-")["status"], "active")


class ConfigTests(unittest.TestCase):
    CONFIG = '''serverAddr = "8.8.8.8"
serverPort = 7000
[metadatas]
id = "private-tunnel-identity"
[transport.tls]
enable = true

[[proxies]]
name = "company"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22

[[proxies]]
name = "preview"
type = "https"
localIP = "127.0.0.1"
localPort = 443
customDomains = ["test.example.com"]
'''

    def test_export_preserves_metadata_but_excludes_company_proxy(self):
        result = tomllib.loads(filtered_config(self.CONFIG, {"preview"}))
        self.assertEqual(result["metadatas"]["id"], "private-tunnel-identity")
        self.assertEqual([row["name"] for row in result["proxies"]], ["preview"])

    def test_missing_proxy_does_not_return_whole_config(self):
        with self.assertRaises(Failure):
            filtered_config(self.CONFIG, {"missing"})

    def test_rejects_include_or_plugins(self):
        for raw in ["includes = [\"other.toml\"]\n" + self.CONFIG, self.CONFIG + '\n[proxies.plugin]\ntype = "https2http"\n']:
            with self.assertRaises(Failure):
                filtered_config(raw, {"preview"})

    def test_http_200_business_error_is_not_success(self):
        client = Client("test-key")
        client.opener.open = lambda *args, **kwargs: io.BytesIO(b'{"code":30001,"msg":"invalid test-key"}')
        with self.assertRaises(Failure) as found:
            client.request("/user/userInfo")
        self.assertEqual(found.exception.exit_code, 2)
        self.assertNotIn("test-key", str(found.exception))

    def test_config_download_is_plain_text(self):
        client = Client("test-key")
        client.opener.open = lambda *args, **kwargs: io.BytesIO(self.CONFIG.encode())
        self.assertEqual(client.config("test"), self.CONFIG)

    def test_redirects_are_never_followed_with_credentials(self):
        with self.assertRaises(Failure):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/api")

    def test_query_only_delete_has_no_json_content_type(self):
        client = Client("test-key")
        requests = []
        def respond(request, **kwargs):
            requests.append(request)
            return io.BytesIO(b'{"code":10000,"data":null}')
        client.opener.open = respond
        client.request("/user/tunnel/del?tid=synthetic", "DELETE")
        self.assertIsNone(requests[0].get_header("Content-type"))
        self.assertIsNone(requests[0].data)

    def test_stdout_redaction_includes_metadata(self):
        data = redact({"metadatas": {"id": "private"}, "access_token": "private", "http_pass": "private", "name": "preview"})
        self.assertNotIn("private", json.dumps(data))
        self.assertEqual(data["name"], "preview")

    def test_standard_https_uses_tls_alpn_and_no_body_buffering(self):
        config = caddyfile({"admin_port": 54214, "challenge_port": 18280, "public_port": 443, "domain": "test.example.com", "local_port": 3214})
        self.assertIn("disable_http_challenge", config)
        self.assertNotIn("disable_tlsalpn_challenge", config)
        self.assertIn("flush_interval -1", config)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PASSNAT_HOME": self.temp.name, "PASSNAT_SECRET_KEY": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_environment_key_wins_without_touching_storage(self):
        with patch.dict(os.environ, {"PASSNAT_SECRET_KEY": "ENV-KEY"}), \
             patch("passnat.core.load_stored_credential") as load:
            self.assertEqual(credential(), "ENV-KEY")
        load.assert_not_called()

    def test_missing_credential_asks_for_login(self):
        with patch("passnat.core.dpapi_available", return_value=False), \
             patch("passnat.core.keychain_available", return_value=False):
            with self.assertRaises(Failure) as found:
                credential()
        self.assertEqual(found.exception.code, "authentication_required")
        self.assertEqual(found.exception.exit_code, 2)

    def test_macos_login_saves_to_keychain_without_writing_a_credential_file(self):
        with patch("passnat.core.dpapi_available", return_value=False), \
             patch("passnat.core.keychain_available", return_value=True), \
             patch("passnat.core.keychain_store") as store:
            self.assertEqual(save_credential("SECRET-KEY"), "macOS Keychain")
        store.assert_called_once_with("SECRET-KEY")
        self.assertFalse((Path(self.temp.name) / "credential.dpapi").exists())

    def test_macos_auth_check_loads_from_keychain(self):
        with patch("passnat.core.dpapi_available", return_value=False), \
             patch("passnat.core.keychain_available", return_value=True), \
             patch("passnat.core.keychain_load", return_value="STORED-KEY") as load:
            self.assertEqual(credential(), "STORED-KEY")
        load.assert_called_once_with()

    def test_windows_login_still_uses_dpapi_file(self):
        with patch("passnat.core.dpapi_available", return_value=True), \
             patch("passnat.core.keychain_available", return_value=False), \
             patch("passnat.core.dpapi", return_value=b"cipher") as protect:
            self.assertEqual(save_credential("SECRET-KEY"), "Windows DPAPI current user")
        protect.assert_called_once_with(b"SECRET-KEY")
        self.assertEqual((Path(self.temp.name) / "credential.dpapi").read_bytes(), b"cipher")

    def test_windows_auth_check_loads_dpapi_file(self):
        (Path(self.temp.name) / "credential.dpapi").write_bytes(b"cipher")
        with patch("passnat.core.dpapi_available", return_value=True), \
             patch("passnat.core.keychain_available", return_value=False), \
             patch("passnat.core.dpapi", return_value=b"STORED-KEY") as unprotect:
            self.assertEqual(load_stored_credential(), "STORED-KEY")
        unprotect.assert_called_once_with(b"cipher", decrypt=True)

    def test_platform_without_secure_storage_cannot_save(self):
        with patch("passnat.core.dpapi_available", return_value=False), \
             patch("passnat.core.keychain_available", return_value=False):
            with self.assertRaises(Failure) as found:
                save_credential("SECRET-KEY")
        self.assertEqual(found.exception.code, "unsupported_platform")
        self.assertNotIn("SECRET-KEY", str(found.exception))


class KeychainTests(unittest.TestCase):
    # 用低层函数 mock 隔离真实钥匙串，只验证存取语义与错误映射。
    def test_store_adds_new_item_and_releases_it(self):
        with patch("passnat.core._keychain_add", return_value=(0, "item")) as add, \
             patch("passnat.core._keychain_release") as release:
            keychain_store("SECRET-KEY")
        add.assert_called_once_with(b"SECRET-KEY")
        release.assert_called_once_with("item")

    def test_store_replaces_duplicate_item(self):
        with patch("passnat.core._keychain_add", return_value=(-25299, None)), \
             patch("passnat.core._keychain_find", return_value=(0, "item", b"old")), \
             patch("passnat.core._keychain_modify", return_value=0) as modify, \
             patch("passnat.core._keychain_release") as release:
            keychain_store("NEW-KEY")
        modify.assert_called_once_with("item", b"NEW-KEY")
        release.assert_called_once_with("item")

    def test_store_failure_is_reported_without_the_secret(self):
        with patch("passnat.core._keychain_add", return_value=(-25308, None)):
            with self.assertRaises(Failure) as found:
                keychain_store("SECRET-KEY")
        self.assertEqual(found.exception.code, "credential_error")
        self.assertIn("-25308", str(found.exception))
        self.assertNotIn("SECRET-KEY", str(found.exception))

    def test_load_returns_none_when_item_is_absent(self):
        with patch("passnat.core._keychain_find", return_value=(-25300, None, None)):
            self.assertIsNone(keychain_load())

    def test_load_returns_secret_and_releases_item(self):
        with patch("passnat.core._keychain_find", return_value=(0, "item", b"STORED-KEY")), \
             patch("passnat.core._keychain_release") as release:
            self.assertEqual(keychain_load(), "STORED-KEY")
        release.assert_called_once_with("item")

    def test_load_failure_is_reported_without_the_secret(self):
        with patch("passnat.core._keychain_find", return_value=(-25293, None, None)):
            with self.assertRaises(Failure) as found:
                keychain_load()
        self.assertEqual(found.exception.code, "credential_error")
        self.assertNotIn("STORED-KEY", str(found.exception))


if __name__ == "__main__":
    unittest.main()
