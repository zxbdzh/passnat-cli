import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from passnat.core import Failure, load_state, save_state
from passnat.shared import add_site, caddy_config, init, migrate, owned_records, probe, run, sync


class SharedClient:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.connected = False

    def tunnel(self, tunnel_id):
        return dict(name='dev', tunnel_id=tunnel_id, status=1 if self.connected else 2, proxy_info=copy.deepcopy(self.rows))

    def request(self, path, method='GET', data=None):
        if path == '/user/domain/list':
            return [dict(id=3, domain='example.com', status=1)]
        self.calls.append((path, method, data))
        if method == 'POST':
            spec=copy.deepcopy(data['port_relation'][0])
            self.rows.append(dict(spec, id=100+len(self.rows), proxy_name='proxy-'+str(len(self.rows)), domain=spec['sub_domain']+'.example.com'))
        return {}


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'PASSNAT_HOME': self.temp.name, 'PASSNAT_CADDY': ''})
        self.env.start()
        # 共享网关本身与平台无关；这里固定为非 macOS，环境变量/受管目录的解析由 preview 测试覆盖。
        self.platform = patch('passnat.preview._caddy_platform', return_value='windows')
        self.platform.start()
        self.client = SharedClient()
        save_state('dev', dict(tunnel_id='test', status='active'), 'create-')
        binary = Path(self.temp.name)/'bin/caddy.exe'
        binary.parent.mkdir()
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        with patch('passnat.shared.find_frpc', return_value='frpc.exe'):
            init(self.client, 'dev', 'test', 18446, 54216, 18283, True)

    def tearDown(self):
        self.platform.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_init_honors_passnat_caddy_env(self):
        custom = Path(self.temp.name)/'custom'/'caddy'
        custom.parent.mkdir()
        custom.write_text('#!/bin/sh\nexit 0\n')
        custom.chmod(0o755)
        with patch.dict(os.environ, {'PASSNAT_CADDY': str(custom)}), patch('passnat.shared.find_frpc', return_value='frpc.exe'):
            init(self.client, 'env', 'test', 18446, 54216, 18283, True)
        self.assertEqual(load_state('env', 'shared-')['caddy'], str(custom.resolve()))
        # 默认状态也不应再被硬编码路径覆盖。
        self.assertEqual(load_state('dev', 'shared-')['caddy'], str((Path(self.temp.name)/'bin/caddy.exe').resolve()))

    def test_init_reports_missing_caddy_with_hints(self):
        with tempfile.TemporaryDirectory() as empty, \
             patch.dict(os.environ, {'PASSNAT_HOME': empty, 'PASSNAT_CADDY': ''}), \
             patch('passnat.preview.shutil.which', return_value=None), \
             patch('passnat.shared.find_frpc', return_value='frpc.exe'):
            save_state('dev', dict(tunnel_id='test', status='active'), 'create-')
            with self.assertRaises(Failure) as found:
                init(self.client, 'missing', 'test', 18446, 54216, 18283, True)
            self.assertEqual(found.exception.code, 'caddy_missing')
            self.assertIn('PASSNAT_CADDY', str(found.exception))
            self.assertIsNone(load_state('missing', 'shared-'))

    def test_macos_init_uses_managed_caddy_and_ignores_env(self):
        managed = Path(self.temp.name)/'bin'/'caddy'
        managed.write_text('#!/bin/sh\nexit 0\n')
        managed.chmod(0o755)
        custom = Path(self.temp.name)/'custom'/'caddy'
        custom.parent.mkdir()
        custom.write_text('#!/bin/sh\nexit 0\n')
        custom.chmod(0o755)
        with patch('passnat.preview._caddy_platform', return_value='macos'), \
             patch.dict(os.environ, {'PASSNAT_CADDY': str(custom)}), \
             patch('passnat.shared.find_frpc', return_value='frpc.exe'):
            init(self.client, 'mac', 'test', 18446, 54216, 18283, True)
        self.assertEqual(load_state('mac', 'shared-')['caddy'], str(managed.resolve()))

    def test_macos_init_requires_managed_caddy(self):
        custom = Path(self.temp.name)/'custom'/'caddy'
        custom.parent.mkdir()
        custom.write_text('#!/bin/sh\nexit 0\n')
        custom.chmod(0o755)
        with tempfile.TemporaryDirectory() as empty, \
             patch.dict(os.environ, {'PASSNAT_HOME': empty, 'PASSNAT_CADDY': str(custom)}), \
             patch('passnat.preview._caddy_platform', return_value='macos'), \
             patch('passnat.shared.find_frpc', return_value='frpc.exe'):
            save_state('dev', dict(tunnel_id='test', status='active'), 'create-')
            with self.assertRaises(Failure) as found:
                init(self.client, 'mac-missing', 'test', 18446, 54216, 18283, True)
            self.assertEqual(found.exception.code, 'caddy_missing')
            self.assertIn('install-caddy', str(found.exception))
            self.assertNotIn('PASSNAT_CADDY', str(found.exception))
            self.assertIsNone(load_state('mac-missing', 'shared-'))

    def test_dry_run_no_remote_or_site_writes(self):
        result=add_site(self.client,'dev','app','app',3,8000,None,[],False)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.client.calls)
        self.assertFalse(load_state('dev','shared-')['sites'])

    def test_sites_share_tls_port_and_reuse_owned_mappings(self):
        for name in ('one','two','one'):
            add_site(self.client,'dev',name,name,3,8000,None,[],True)
        self.assertEqual(len(self.client.rows),2)
        self.assertEqual({row['local_port'] for row in self.client.rows},{18446})
        self.assertEqual(len(load_state('dev','shared-')['sites']),2)
        self.assertIn('reverse_proxy http://127.0.0.1:8000', caddy_config(load_state('dev','shared-')))

    def test_unknown_mapping_not_adopted(self):
        self.client.rows.append(dict(id=7,proxy_name='foreign'))
        with self.assertRaises(Failure) as error:
            add_site(self.client,'dev','app','app',3,8000,None,[],True)
        self.assertEqual(error.exception.code,'ownership_conflict')
        self.assertFalse(self.client.calls)

    def test_changed_owned_mapping_rejected(self):
        add_site(self.client,'dev','app','app',3,8000,None,[],True)
        self.client.rows[0]['local_port']=22
        with self.assertRaises(Failure):
            owned_records(self.client.tunnel('test'))

    def test_static_site_only_allowlisted_html(self):
        root=Path(self.temp.name)/'public'
        root.mkdir()
        (root/'index.html').write_text('<h1>synthetic</h1>')
        (root/'private.json').write_text('{}')
        add_site(self.client,'dev','docs','docs',3,None,str(root),['index.html'],True)
        text=caddy_config(load_state('dev','shared-'))
        self.assertIn('method GET HEAD',text)
        self.assertIn('path / /index.html',text)
        self.assertIn('respond 404',text)
        self.assertNotIn('private.json',text)
        self.assertNotIn('browse',text)

    def test_static_path_escape_rejected(self):
        with self.assertRaises(Failure):
            add_site(self.client,'dev','docs','docs',3,None,self.temp.name,['../index.html'],True)
        self.assertFalse(self.client.calls)

    def test_admin_port_cannot_be_upstream(self):
        with self.assertRaises(Failure):
            add_site(self.client,'dev','app','app',3,54216,None,[],True)

    def test_changed_site_configuration_rejected(self):
        add_site(self.client,'dev','app','app',3,8000,None,[],True)
        with self.assertRaises(Failure):
            add_site(self.client,'dev','app','app',3,9000,None,[],True)

    def test_local_address_not_public_verification(self):
        with self.assertRaises(Failure):
            probe('127.0.0.1','app.example.com')

    def test_second_live_frpc_blocked(self):
        Path(self.temp.name,'shared-dev.Caddyfile').touch()
        Path(self.temp.name,'shared-dev.frpc.toml').write_text('')
        self.client.connected=True
        with patch('passnat.shared.Client',return_value=self.client), patch('subprocess.Popen') as process:
            with self.assertRaises(Failure) as error:
                run('dev','frpc')
            self.assertEqual(error.exception.code,'tunnel_in_use')
            process.assert_not_called()

    def test_sync_dry_run_no_configuration_writes(self):
        add_site(self.client,'dev','app','app',3,8000,None,[],True)
        with patch('passnat.shared.checked') as command:
            result=sync(self.client,'dev',False,True)
            self.assertTrue(result['dry_run'])
            command.assert_not_called()
        self.assertFalse(Path(self.temp.name,'shared-dev.Caddyfile').exists())

    def test_migration_rejects_foreign_source_without_delete(self):
        with self.assertRaises(Failure) as error:
            migrate(self.client,'dev','foreign','app',8000,True)
        self.assertEqual(error.exception.code,'not_owned')
        self.assertFalse(self.client.calls)

    def test_migration_rejects_multiple_source_mappings(self):
        save_state('old',dict(tunnel_id='old-id',status='active'),'create-')
        original=self.client.tunnel
        self.client.tunnel=lambda target: dict(name='old',tunnel_id=target,proxy_info=[dict(id=1),dict(id=2)]) if target=='old-id' else original(target)
        with self.assertRaises(Failure) as error:
            migrate(self.client,'dev','old','app',8000,True)
        self.assertEqual(error.exception.code,'ownership_conflict')
        self.assertFalse(self.client.calls)


if __name__ == '__main__':
    unittest.main()
