import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from passnat.core import Failure, load_state, save_state
from passnat.preview import CADDY_INSTALL_RECORD, CADDY_MACOS_ARCHIVE_SHA256, CADDY_MACOS_ASSET, CADDY_RELEASE, CADDY_WINDOWS_ASSET, down, find_caddy, find_frpc, install_caddy, run, up

APP_FRPC = Path("PassNAT.app/Contents/MacOS/frpc")
SYSTEM_FRPC = Path("/Applications") / APP_FRPC
USER_FRPC_TEXT = "~/Applications/PassNAT.app/Contents/MacOS/frpc"


def existing_only(*allowed):
    """只让给定路径看起来存在，避免测试依赖宿主机真实文件。"""
    allowed = list(map(Path, allowed))

    def is_file(path):
        return any(path == item or path.resolve() == item.resolve() for item in allowed)

    return patch.object(Path, "is_file", is_file)


class FindFrpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    # --- 优先级：显式 > 环境变量 > PATH > 应用包 -------------------------

    def test_explicit_wins_over_env_path_and_app_bundle(self):
        explicit = self.executable("explicit/frpc")
        env_frpc = self.executable("env/frpc")
        on_path = self.executable("path/frpc")
        bundle = self.executable("home/Applications/PassNAT.app/Contents/MacOS/frpc")
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": str(env_frpc)}), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_frpc(str(explicit)), str(explicit.resolve()))
        self.assertTrue(bundle.is_file())

    def test_env_wins_over_path_and_app_bundle(self):
        env_frpc = self.executable("env/frpc")
        on_path = self.executable("path/frpc")
        self.executable("home/Applications/PassNAT.app/Contents/MacOS/frpc")
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": str(env_frpc)}), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_frpc(), str(env_frpc.resolve()))

    def test_path_wins_over_app_bundle(self):
        on_path = self.executable("path/frpc")
        self.executable("home/Applications/PassNAT.app/Contents/MacOS/frpc")
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_frpc(), str(on_path.resolve()))

    # --- macOS 应用包发现 ------------------------------------------------

    def test_macos_system_app_bundle_is_discovered(self):
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=None), \
             existing_only(SYSTEM_FRPC), \
             patch("passnat.preview.os.access", return_value=True):
            self.assertEqual(find_frpc(), str(SYSTEM_FRPC.resolve()))

    def test_macos_user_app_bundle_is_discovered(self):
        user_frpc = self.executable("home/Applications/PassNAT.app/Contents/MacOS/frpc")
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=None), \
             existing_only(user_frpc):
            self.assertEqual(find_frpc(), str(user_frpc.resolve()))

    def test_app_bundle_is_ignored_off_macos(self):
        with patch("passnat.preview.sys.platform", "linux"), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=None), \
             existing_only(SYSTEM_FRPC), \
             patch("passnat.preview.os.access", return_value=True):
            with self.assertRaises(Failure) as found:
                find_frpc()
        self.assertEqual(found.exception.code, "frpc_missing")
        self.assertNotIn("PassNAT.app", str(found.exception))

    # --- 失败路径 --------------------------------------------------------

    def test_no_usable_candidate_fails_with_macos_paths(self):
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=None), \
             patch.object(Path, "is_file", return_value=False), \
             patch("passnat.preview.os.access", return_value=False):
            with self.assertRaises(Failure) as found:
                find_frpc()
        self.assertEqual(found.exception.code, "frpc_missing")
        self.assertEqual(found.exception.exit_code, 3)
        message = str(found.exception)
        # 报错固定提示 macOS 正斜杠路径，避免在 Windows 上因反斜杠导致断言失真。
        self.assertIn(SYSTEM_FRPC.as_posix(), message)
        self.assertIn(USER_FRPC_TEXT, message)

    def test_non_executable_bundle_binary_is_rejected(self):
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=None), \
             existing_only(SYSTEM_FRPC), \
             patch("passnat.preview.os.access", return_value=False):
            with self.assertRaises(Failure) as found:
                find_frpc()
        self.assertEqual(found.exception.code, "frpc_missing")

    def test_explicit_missing_file_falls_back_to_bundle(self):
        bundle = self.executable("home/Applications/PassNAT.app/Contents/MacOS/frpc")
        with patch("passnat.preview.sys.platform", "darwin"), \
             patch.object(Path, "home", return_value=self.home), \
             patch.dict(os.environ, {"PASSNAT_FRPC": ""}), \
             patch("passnat.preview.shutil.which", return_value=None), \
             existing_only(bundle):
            self.assertEqual(find_frpc(str(self.root / "missing")), str(bundle.resolve()))


class FindCaddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def executable(self, relative, mode=0o755):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(mode)
        return path

    def write_macos_install_record(self, target):
        record = {
            "version": "2.11.9",
            "asset": CADDY_MACOS_ASSET,
            "archive_sha256": CADDY_MACOS_ARCHIVE_SHA256,
            "binary_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
        (target.parent / CADDY_INSTALL_RECORD).write_text(json.dumps(record), "utf-8")

    # --- macOS：只认受管 bin/caddy，不回退 Homebrew / PATH / 环境变量 ---------

    def test_macos_uses_managed_binary_even_when_env_and_path_exist(self):
        managed = self.executable("home/bin/caddy")
        self.write_macos_install_record(managed)
        env_caddy = self.executable("env/caddy")
        on_path = self.executable("path/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": str(env_caddy)}), \
             patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_caddy(), str(managed.resolve()))

    def test_macos_rejects_managed_binary_without_install_record(self):
        self.executable("home/bin/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="macos"):
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "unexpected_file")

    def test_macos_rejects_tampered_managed_binary(self):
        managed = self.executable("home/bin/caddy")
        self.write_macos_install_record(managed)
        managed.write_bytes(b"tampered")
        managed.chmod(0o755)
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="macos"):
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "unexpected_file")

    def test_macos_rejects_symlink_at_managed_binary(self):
        outside = self.executable("outside/caddy")
        managed = self.home / "bin" / "caddy"
        managed.parent.mkdir(parents=True, exist_ok=True)
        try:
            managed.symlink_to(outside)
            symlink_guard = nullcontext()
        except OSError:
            symlink_guard = patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == managed)
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="macos"), \
             symlink_guard:
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "unsafe_path")

    def test_macos_ignores_env_and_path_without_managed_binary(self):
        env_caddy = self.executable("env/caddy")
        on_path = self.executable("path/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": str(env_caddy)}), \
             patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "caddy_missing")
        self.assertEqual(found.exception.exit_code, 3)
        message = str(found.exception)
        self.assertIn("install-caddy", message)
        self.assertIn("Apple Silicon", message)
        self.assertIn("Homebrew", message)

    def test_macos_ignores_managed_windows_name(self):
        self.executable("home/bin/caddy.exe")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.shutil.which", return_value=None):
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "caddy_missing")

    def test_macos_rejects_non_executable_managed_binary(self):
        self.executable("home/bin/caddy", mode=0o644)
        # Windows 下 chmod(0o644) 不清除执行位，需要显式模拟 os.access 返回 False。
        no_exec = patch("passnat.preview.os.access", return_value=False) if os.name == "nt" else nullcontext()
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="macos"), \
             no_exec:
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "caddy_missing")

    # --- Windows / 其他平台：保留环境变量、受管目录与 PATH 顺序 ----------------

    def test_env_wins_over_managed_bin_and_path(self):
        env_caddy = self.executable("env/caddy")
        self.executable("home/bin/caddy.exe")
        on_path = self.executable("path/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": str(env_caddy)}), \
             patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_caddy(), str(env_caddy.resolve()))

    def test_managed_windows_name_wins_over_path(self):
        managed = self.executable("home/bin/caddy.exe")
        on_path = self.executable("path/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_caddy(), str(managed.resolve()))

    def test_managed_posix_name_is_supported(self):
        managed = self.executable("home/bin/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="unsupported"), \
             patch("passnat.preview.shutil.which", return_value=None):
            self.assertEqual(find_caddy(), str(managed.resolve()))

    def test_path_is_used_when_no_managed_binary(self):
        on_path = self.executable("path/caddy")
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview.shutil.which", return_value=str(on_path)):
            self.assertEqual(find_caddy(), str(on_path.resolve()))

    def test_non_executable_candidate_is_rejected(self):
        blocked = self.executable("home/bin/caddy", mode=0o644)
        no_exec = patch("passnat.preview.os.access", return_value=False) if os.name == "nt" else nullcontext()
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": str(blocked)}), \
             patch("passnat.preview._caddy_platform", return_value="unsupported"), \
             patch("passnat.preview.shutil.which", return_value=None), \
             no_exec:
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "caddy_missing")

    def test_missing_caddy_reports_resolution_hints(self):
        with patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""}), \
             patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview.shutil.which", return_value=None):
            with self.assertRaises(Failure) as found:
                find_caddy()
        self.assertEqual(found.exception.code, "caddy_missing")
        self.assertEqual(found.exception.exit_code, 3)
        message = str(found.exception)
        self.assertIn("PASSNAT_CADDY", message)
        self.assertIn("PATH", message)


def make_tar(entries) -> bytes:
    """entries: ("file", name, payload) 或 ("symlink", name, target)。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for kind, name, value in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = value
                archive.addfile(info)
    return buffer.getvalue()


def make_zip(entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class InstallCaddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""})
        env.start()
        self.addCleanup(env.stop)

    def fetch_mock(self, payloads):
        calls = []

        def fetch(url, timeout=None):
            calls.append(url)
            return payloads[url]

        return fetch, calls

    # --- macOS Apple Silicon -------------------------------------------------

    def test_macos_installs_only_the_caddy_member(self):
        archive = make_tar([("file", "caddy", b"mac-binary"), ("file", "README.md", b"docs")])
        digest = hashlib.sha256(archive).hexdigest()
        fetch, calls = self.fetch_mock({CADDY_RELEASE + CADDY_MACOS_ASSET: archive})
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview.CADDY_MACOS_ARCHIVE_SHA256", digest), \
             patch("passnat.preview._fetch", side_effect=fetch):
            result = install_caddy()
        target = self.home / "bin" / "caddy"
        self.assertEqual(target.read_bytes(), b"mac-binary")
        self.assertTrue(os.access(target, os.X_OK))
        self.assertEqual(calls, [CADDY_RELEASE + CADDY_MACOS_ASSET])
        self.assertEqual(result["asset"], CADDY_MACOS_ASSET)
        self.assertTrue(result["checksum_verified"])
        self.assertEqual(result["version"], "2.11.9")
        self.assertFalse((self.home / "bin" / "README.md").exists())

    def test_macos_checksum_mismatch_writes_nothing(self):
        archive = make_tar([("file", "caddy", b"mac-binary")])
        fetch, _ = self.fetch_mock({CADDY_RELEASE + CADDY_MACOS_ASSET: archive})
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch", side_effect=fetch):
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "checksum_mismatch")
        self.assertFalse((self.home / "bin" / "caddy").exists())

    def test_macos_symlink_member_is_rejected(self):
        archive = make_tar([("symlink", "caddy", "/bin/sh")])
        digest = hashlib.sha256(archive).hexdigest()
        fetch, _ = self.fetch_mock({CADDY_RELEASE + CADDY_MACOS_ASSET: archive})
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview.CADDY_MACOS_ARCHIVE_SHA256", digest), \
             patch("passnat.preview._fetch", side_effect=fetch):
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unsafe_archive")
        self.assertFalse((self.home / "bin" / "caddy").exists())

    def test_macos_requires_apple_silicon(self):
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="x86_64"), \
             patch("passnat.preview._fetch") as fetch:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unsupported_platform")
        self.assertEqual(found.exception.exit_code, 3)
        fetch.assert_not_called()

    def test_unsupported_platform_is_rejected_before_download(self):
        with patch("passnat.preview._caddy_platform", return_value="unsupported"), \
             patch("passnat.preview._fetch") as fetch:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unsupported_platform")
        fetch.assert_not_called()

    def install_record(self, target, binary=b"mac-binary", **overrides):
        record = {
            "version": "2.11.9",
            "asset": CADDY_MACOS_ASSET,
            "archive_sha256": CADDY_MACOS_ARCHIVE_SHA256,
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
        }
        record.update(overrides)
        (target.parent / CADDY_INSTALL_RECORD).write_text(json.dumps(record), "utf-8")

    def test_macos_reuses_recorded_binary_without_download(self):
        target = self.home / "bin" / "caddy"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"mac-binary")
        target.chmod(0o600)
        self.install_record(target)
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch") as fetch:
            result = install_caddy()
        self.assertTrue(result["reused"])
        self.assertTrue(os.access(target, os.X_OK))
        fetch.assert_not_called()

    def test_macos_reinstall_reuses_binary_from_previous_install(self):
        # 回归：归档摘要不可能等于解压后二进制的摘要，重复安装必须靠安装记录复用。
        archive = make_tar([("file", "caddy", b"mac-binary")])
        archive_digest = hashlib.sha256(archive).hexdigest()
        fetch, calls = self.fetch_mock({CADDY_RELEASE + CADDY_MACOS_ASSET: archive})
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview.CADDY_MACOS_ARCHIVE_SHA256", archive_digest), \
             patch("passnat.preview._fetch", side_effect=fetch):
            first = install_caddy()
        self.assertFalse(first.get("reused"))
        self.assertEqual(len(calls), 1)
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview.CADDY_MACOS_ARCHIVE_SHA256", archive_digest), \
             patch("passnat.preview._fetch") as second_fetch:
            second = install_caddy()
        self.assertTrue(second["reused"])
        second_fetch.assert_not_called()

    def test_macos_reuse_without_record_is_refused(self):
        target = self.home / "bin" / "caddy"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"mac-binary")
        target.chmod(0o755)
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch") as fetch:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unexpected_file")
        self.assertEqual(target.read_bytes(), b"mac-binary")
        fetch.assert_not_called()

    def test_macos_reuse_with_tampered_binary_is_refused(self):
        target = self.home / "bin" / "caddy"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"tampered")
        target.chmod(0o755)
        self.install_record(target, binary=b"mac-binary")
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch") as fetch:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unexpected_file")
        self.assertEqual(target.read_bytes(), b"tampered")
        fetch.assert_not_called()

    def test_macos_reuse_with_record_for_other_asset_is_refused(self):
        target = self.home / "bin" / "caddy"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"mac-binary")
        target.chmod(0o755)
        self.install_record(target, asset="caddy_2.11.7_mac_arm64.tar.gz")
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch") as fetch:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unexpected_file")
        self.assertEqual(target.read_bytes(), b"mac-binary")
        fetch.assert_not_called()

    def test_macos_does_not_overwrite_unexpected_file(self):
        target = self.home / "bin" / "caddy"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrelated")
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch") as fetch:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unexpected_file")
        self.assertEqual(target.read_bytes(), b"unrelated")
        fetch.assert_not_called()

    def test_macos_rejects_symlink_at_managed_path(self):
        outside = self.root / "outside-caddy"
        outside.write_bytes(b"mac-binary")
        target = self.home / "bin" / "caddy"
        target.parent.mkdir(parents=True)
        try:
            target.symlink_to(outside)
            symlink_guard = nullcontext()
        except OSError:
            # Windows 非提权账号默认无 symlink 权限，改用定向 mock 覆盖符号链接拒绝分支。
            symlink_guard = patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == target)
        with patch("passnat.preview._caddy_platform", return_value="macos"), \
             patch("passnat.preview.platform.machine", return_value="arm64"), \
             patch("passnat.preview._fetch") as fetch, \
             symlink_guard:
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "unsafe_path")
        fetch.assert_not_called()

    # --- Windows amd64 -------------------------------------------------------

    def test_windows_installs_caddy_exe_after_checksum_verification(self):
        archive = make_zip({"caddy.exe": b"win-binary", "LICENSE": b"license"})
        digest = hashlib.sha256(archive).hexdigest()
        checksums = f"{digest}  {CADDY_WINDOWS_ASSET}\n".encode("utf-8")
        fetch, calls = self.fetch_mock({
            CADDY_RELEASE + "caddy_2.11.9_checksums.txt": checksums,
            CADDY_RELEASE + CADDY_WINDOWS_ASSET: archive,
        })
        with patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview._fetch", side_effect=fetch):
            result = install_caddy()
        target = self.home / "bin" / "caddy.exe"
        self.assertEqual(target.read_bytes(), b"win-binary")
        self.assertEqual(result["asset"], CADDY_WINDOWS_ASSET)
        self.assertEqual(len(calls), 2)
        self.assertIn("checksums.txt", calls[0])

    def test_windows_checksum_mismatch_writes_nothing(self):
        archive = make_zip({"caddy.exe": b"win-binary"})
        checksums = f"{'0' * 64}  {CADDY_WINDOWS_ASSET}\n".encode("utf-8")
        fetch, _ = self.fetch_mock({
            CADDY_RELEASE + "caddy_2.11.9_checksums.txt": checksums,
            CADDY_RELEASE + CADDY_WINDOWS_ASSET: archive,
        })
        with patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview._fetch", side_effect=fetch):
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "checksum_mismatch")
        self.assertFalse((self.home / "bin" / "caddy.exe").exists())

    def test_windows_zip_without_caddy_member_fails(self):
        archive = make_zip({"other.exe": b"binary"})
        digest = hashlib.sha256(archive).hexdigest()
        checksums = f"{digest}  {CADDY_WINDOWS_ASSET}\n".encode("utf-8")
        fetch, _ = self.fetch_mock({
            CADDY_RELEASE + "caddy_2.11.9_checksums.txt": checksums,
            CADDY_RELEASE + CADDY_WINDOWS_ASSET: archive,
        })
        with patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview._fetch", side_effect=fetch):
            with self.assertRaises(Failure) as found:
                install_caddy()
        self.assertEqual(found.exception.code, "download_failed")

    def test_windows_reuses_existing_executable_without_download(self):
        target = self.home / "bin" / "caddy.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        target.chmod(0o755)
        with patch("passnat.preview._caddy_platform", return_value="windows"), \
             patch("passnat.preview._fetch") as fetch:
            result = install_caddy()
        self.assertTrue(result["reused"])
        self.assertNotIn("version", result)
        self.assertEqual(target.read_bytes(), b"existing")
        fetch.assert_not_called()


class PreviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, {"PASSNAT_HOME": str(self.home), "PASSNAT_CADDY": ""})
        env.start()
        self.addCleanup(env.stop)

    def _make_client(self, fail_tcp=False):
        class ClientDouble:
            def __init__(self):
                self.rows = []
                self.calls = []
                self.deleted = False

            def tunnel(self, tunnel_id):
                return {
                    "tunnel_id": tunnel_id,
                    "name": "demo",
                    "status": 2,
                    "node_info": {"start_port": 10000, "end_port": 60000, "remote_ip": "8.8.8.8"},
                    "proxy_info": list(self.rows),
                }

            def config(self, tunnel_id):
                proxy_blocks = "\n".join(
                    f'[[proxies]]\nname = "{row["proxy_name"]}"\ntype = "{row["protocol"]}"\nlocalIP = "127.0.0.1"\nlocalPort = {row["local_port"]}\n'
                    for row in self.rows
                )
                return f'serverAddr = "8.8.8.8"\nserverPort = 7000\n[transport.tls]\nenable = true\n\n{proxy_blocks}'

            def request(self, path, method="GET", data=None, **kwargs):
                self.calls.append((path, method, data))
                if path == "/user/domain/list":
                    return [{"id": 3, "domain": "example.com", "status": 1}]
                if path == "/user/tunnel/port/add":
                    spec = dict(data["port_relation"][0])
                    if fail_tcp and spec["protocol"] == "tcp":
                        raise Failure("api_error", "tcp port rejected")
                    self.rows.append(
                        dict(
                            spec,
                            id=200 + len(self.rows),
                            proxy_name=f'p-{spec["protocol"]}',
                            domain=f'{spec.get("sub_domain", "demo")}.example.com',
                        )
                    )
                    return {}
                if path.startswith("/user/tunnel/del"):
                    self.deleted = True
                    return {}
                if path.startswith("/user/tunnel/list"):
                    return [] if self.deleted else [{"tunnel_id": "T1", "name": "demo"}]
                return {}

        return ClientDouble()

    def test_up_apply_443_configures_frpc_and_caddy_successfully(self):
        client = self._make_client()
        with patch("passnat.preview.find_frpc", return_value="frpc.exe"), \
             patch("passnat.preview.find_caddy", return_value="caddy.exe"), \
             patch("passnat.preview.subprocess.run", return_value=type("R", (), {"returncode": 0})()):
            result = up(client, "demo", "T1", 5000, 443, 18280, 54214, 3, "app", None, True)
        self.assertTrue(result["configured"])
        self.assertEqual(result["url"], "https://app.example.com/")
        self.assertEqual(load_state("demo", "preview-")["status"], "configured")
        self.assertTrue((self.home / "preview-demo.frpc.toml").is_file())
        self.assertTrue((self.home / "preview-demo.Caddyfile").is_file())

    def test_down_recovers_partial_preparing_preview_without_proxy_names(self):
        client = self._make_client(fail_tcp=True)
        save_state("demo", {"tunnel_id": "T1", "name": "demo", "status": "active"}, "create-")
        with patch("passnat.preview.find_frpc", return_value="frpc.exe"), \
             patch("passnat.preview.find_caddy", return_value="caddy.exe"):
            with self.assertRaises(Failure) as found:
                up(client, "demo", "T1", 5000, 18443, 18280, 54214, 3, "app", None, True)
        self.assertEqual(found.exception.code, "api_error")
        preview_state = load_state("demo", "preview-")
        self.assertEqual(preview_state["status"], "preparing")
        self.assertNotIn("proxy_names", preview_state)
        self.assertEqual(load_state("demo-http")["status"], "active")
        # 部分失败后无需 proxy_names，也必须允许正常 down 清理已建映射与专用隧道。
        with patch("passnat.preview._stop_preview_caddy"):
            plan = down(client, "demo", False)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["port_ids"], [200])
            removed = down(client, "demo", True)
        self.assertTrue(removed["removed"])
        self.assertTrue(client.deleted)
        self.assertEqual(load_state("demo", "preview-")["status"], "removed")
        self.assertEqual(load_state("demo-http")["status"], "removed")

    def test_run_raises_runtime_exit_on_nonzero_exit_code(self):
        save_state("demo", {"status": "configured", "tunnel_id": "T1", "proxy_names": ["p-https"], "caddy": "caddy", "frpc": "frpc"}, "preview-")
        (self.home / "preview-demo.Caddyfile").write_text("", "utf-8")
        fake_child = type("Child", (), {"pid": 999, "stdout": io.StringIO("boot failed\n"), "wait": lambda self: 7})()
        with patch("passnat.preview.subprocess.Popen", return_value=fake_child), \
             patch("sys.stderr", io.StringIO()):
            with self.assertRaises(Failure) as found:
                run("demo", "proxy")
        self.assertEqual(found.exception.code, "runtime_exit")
        self.assertIn("7", str(found.exception))

    def test_down_refuses_tunnel_deletion_when_caddy_stop_returns_http_500(self):
        client = self._make_client()
        save_state("demo", {"tunnel_id": "T1", "name": "demo", "status": "active"}, "create-")
        with patch("passnat.preview.find_frpc", return_value="frpc.exe"), \
             patch("passnat.preview.find_caddy", return_value="caddy.exe"), \
             patch("passnat.preview.subprocess.run", return_value=type("R", (), {"returncode": 0})()):
            up(client, "demo", "T1", 5000, 443, 18280, 54214, 3, "app", None, True)

        class FakeOpener:
            def open(self, req, timeout=5):
                url = req if isinstance(req, str) else req.full_url
                if url.endswith("/config/"):
                    return io.BytesIO(b'{"apps":{"http":{"servers":{"s":{"routes":[{"match":[{"host":["app.example.com"]}]}]}}}}}')
                raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

        with patch("passnat.preview._port_listening", return_value=False), \
             patch("passnat.preview.urllib.request.build_opener", return_value=FakeOpener()):
            with self.assertRaises(Failure) as found:
                down(client, "demo", True)
        self.assertEqual(found.exception.code, "runtime_command_failed")
        self.assertFalse(client.deleted)
        self.assertEqual(load_state("demo", "preview-")["status"], "configured")


if __name__ == "__main__":
    unittest.main()
