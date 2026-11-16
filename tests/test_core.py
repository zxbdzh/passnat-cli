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

from passnat.core import Client, Failure, NoRedirect, add_port, delete_port, filtered_config, load_state, marker, mutation_lock, owned_path, redact, save_state, write_private
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


if __name__ == "__main__":
    unittest.main()
