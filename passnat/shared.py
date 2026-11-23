from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from .core import Client, Failure, ensure_home, filtered_config, load_state, matches, mutation_lock, owned_path, port_number, save_state, write_private, add_port
from .preview import find_caddy, find_frpc


def state_for(name):
    state = load_state(name, 'shared-')
    if not state:
        raise Failure('not_configured', '请先执行 shared init --apply。', 3)
    return state


def owned_records(tunnel):
    records = {}
    for path in ensure_home().glob('port-*.json'):
        item = json.loads(path.read_text('utf-8'))
        if item.get('tunnel_id') == tunnel['tunnel_id'] and item.get('status') == 'active':
            records[item.get('port_id')] = item
    for row in tunnel.get('proxy_info', []):
        item = records.get(row['id'])
        if not item or not matches(row, item['spec']) or row.get('proxy_name') != item.get('proxy_name'):
            raise Failure('ownership_conflict', '共享隧道存在未登记或已变化的映射，已停止。', 3)
    return records


def init(client, name, tunnel_id, tls_port, admin_port, challenge_port, apply):
    owned_path('shared-' + name)
    ports = (tls_port, admin_port, admin_port + 1, challenge_port)
    for value in ports:
        port_number(value)
    if len(set(ports)) != len(ports):
        raise Failure('port_conflict', 'TLS、挑战与两个管理端口必须互不相同。', 3)
    tunnel = client.tunnel(tunnel_id)
    creation = load_state(tunnel['name'], 'create-')
    if not creation or creation.get('tunnel_id') != tunnel_id or creation.get('status') != 'active':
        raise Failure('not_owned', '仅使用本工具创建并登记的共享隧道。', 3)
    owned_records(tunnel)
    values = dict(name=name, tunnel_id=tunnel_id, tunnel_name=tunnel['name'], tls_port=tls_port, admin_port=admin_port, challenge_port=challenge_port)
    previous = load_state(name, 'shared-')
    if previous and any(previous.get(key) != value for key, value in values.items()):
        raise Failure('state_conflict', '共享网关已有其他参数，拒绝覆盖。', 3)
    if not apply:
        return dict(dry_run=True, **values)
    state = dict(previous or {}, **values)
    state.setdefault('sites', {})
    state.setdefault('frpc_admin_password', secrets.token_urlsafe(32))
    state['frpc'] = find_frpc()
    state['caddy'] = find_caddy()
    save_state(name, state, 'shared-')
    return dict(configured=True, name=name, site_count=len(state['sites']))


def add_site(client, name, site_name, subdomain, domain_id, upstream_port, root, files, apply):
    state = state_for(name)
    port_name = f'site-{name}-{site_name}'
    owned_path('port-' + port_name)
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?', subdomain):
        raise Failure('invalid_domain', '子域名格式无效。', 3)
    domains = client.request('/user/domain/list')
    domain = next((item['domain'] for item in domains if item['id'] == domain_id and item.get('status') == 1), None)
    if not domain or not re.fullmatch(r'[a-zA-Z0-9.-]+', domain):
        raise Failure('domain_unavailable', '域名不可用。', 3)
    if bool(upstream_port) == bool(root):
        raise Failure('invalid_site', '必须选择反向代理或白名单静态站点。', 3)
    site = dict(domain=subdomain + '.' + domain, port_name=port_name)
    if upstream_port:
        port_number(upstream_port)
        if upstream_port in (state['tls_port'], state['admin_port'], state['admin_port'] + 1, state['challenge_port']):
            raise Failure('port_conflict', '不能代理到网关自身或管理接口。', 3)
        site['upstream_port'] = upstream_port
    else:
        directory = Path(root)
        if not directory.is_absolute() or not directory.is_dir() or not files:
            raise Failure('invalid_root', '需要绝对目录及文件白名单。', 3)
        for filename in files:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*[.]html', filename) or not (directory / filename).is_file() or (directory / filename).is_symlink():
                raise Failure('invalid_file', '仅允许明确存在的普通 HTML 文件。', 3)
        if 'index.html' not in files:
            raise Failure('missing_index', '需要白名单内的 index.html。', 3)
        site.update(root=str(directory.resolve()), files=sorted(set(files)))
    previous = state['sites'].get(site_name)
    if previous and {key: value for key, value in previous.items() if key != 'status'} != site:
        raise Failure('site_conflict', '同名站点参数不同，拒绝覆盖。', 3)
    if any(key != site_name and value['domain'] == site['domain'] for key, value in state['sites'].items()):
        raise Failure('domain_conflict', '域名已被其他站点登记。', 3)
    owned_records(client.tunnel(state['tunnel_id']))
    spec = dict(local_ip='127.0.0.1', local_port=state['tls_port'], remote_port=443, protocol='https', sub_domain=subdomain, domain_id=domain_id)
    if not apply:
        return dict(dry_run=True, site=site, mapping=add_port(client, port_name, state['tunnel_id'], spec, False))
    # 先保存意图，回执不确定时依赖端口登记对账，不盲目重试。
    state['sites'][site_name] = dict(site, status='preparing')
    save_state(name, state, 'shared-')
    result = add_port(client, port_name, state['tunnel_id'], spec, True)
    state['sites'][site_name]['status'] = 'active'
    save_state(name, state, 'shared-')
    return dict(registered=True, url='https://' + site['domain'] + '/', port_id=result['port']['id'])


def config_paths(name):
    return owned_path('shared-' + name, '.Caddyfile'), owned_path('shared-' + name, '.frpc.toml')


def caddy_config(state):
    storage = (ensure_home() / ('shared-' + state['name'] + '-certs')).as_posix()
    # 路径使用 ensure_ascii=False，避免 Caddyfile 把非 ASCII 目录名当成字面量 \uXXXX。
    lines = ['{', f'    admin 127.0.0.1:{state["admin_port"]}', f'    http_port {state["challenge_port"]}', f'    https_port {state["tls_port"]}', '    auto_https disable_redirects', '    storage file_system {', f'        root {json.dumps(storage, ensure_ascii=False)}', '    }', '}', '']
    for site in state['sites'].values():
        if site.get('status') != 'active':
            continue
        lines += [f'https://{site["domain"]} {{', '    bind 127.0.0.1', '    tls {', '        issuer acme {', '            disable_http_challenge', '        }', '    }', '    header X-Robots-Tag "noindex, nofollow, noarchive"']
        if 'upstream_port' in site:
            lines += [f'    reverse_proxy http://127.0.0.1:{site["upstream_port"]} {{', '        flush_interval -1', '        header_down Set-Cookie "^(.*)$" "$1; Secure"', '    }']
        else:
            paths = ['/'] + ['/' + filename for filename in site['files']]
            lines += ['    @published {', '        method GET HEAD', '        path ' + ' '.join(paths), '    }', '    handle @published {', f'        root * {json.dumps(Path(site["root"]).as_posix(), ensure_ascii=False)}', '        file_server', '    }', '    handle {', '        respond 404', '    }']
        lines += ['}', '']
    return chr(10).join(lines)


def checked(command):
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode:
        raise Failure('runtime_command_failed', f'{Path(command[0]).name} {command[1]} 未成功，未输出私有详情。')


def sync(client, name, apply, reload=False):
    state = state_for(name)
    tunnel = client.tunnel(state['tunnel_id'])
    if tunnel['name'] != state['tunnel_name']:
        raise Failure('ownership_conflict', '隧道名称变化，拒绝同步。', 3)
    owned_records(tunnel)
    names = {row['proxy_name'] for row in tunnel.get('proxy_info', [])}
    if not names or not state['sites']:
        raise Failure('empty_gateway', '没有站点或代理。', 3)
    for site in state['sites'].values():
        record = load_state(site['port_name'])
        if site.get('status') != 'active' or not record or record.get('proxy_name') not in names:
            raise Failure('reconciliation_required', '存在未完成的站点或缺失映射。', 3)
    if not apply:
        return dict(dry_run=True, site_count=len(state['sites']), proxy_count=len(names), reload=reload)
    raw = filtered_config(client.config(state['tunnel_id']), names)
    if 'webServer' in tomllib.loads(raw):
        raise Failure('unsafe_config', '平台配置已含管理接口，拒绝覆盖。', 3)
    common, proxies = raw.split('[[proxies]]', 1)
    frpc = chr(10).join([common.rstrip(), '', '[webServer]', 'addr = "127.0.0.1"', f'port = {state["admin_port"] + 1}', 'user = "shared"', f'password = {json.dumps(state["frpc_admin_password"])}', '', '[[proxies]]' + proxies])
    caddy_text = caddy_config(state)
    caddy_path, frpc_path = config_paths(name)
    # 先对临时候选配置执行 validate 与 verify，全部通过后再备份 .previous 并替换正式文件，
    # 避免校验失败污染正式配置或重复失败时把最后有效备份覆盖成坏配置。
    tmp_files = []
    try:
        for suffix, content in (('.Caddyfile', caddy_text), ('.frpc.toml', frpc)):
            handle, tmp_name = tempfile.mkstemp(prefix='.validate-', suffix=suffix, dir=caddy_path.parent)
            tmp_path = Path(tmp_name)
            tmp_files.append(tmp_path)
            with os.fdopen(handle, 'wb') as stream:
                stream.write(content.encode('utf-8'))
        tmp_caddy, tmp_frpc = tmp_files
        checked([state['caddy'], 'validate', '--config', str(tmp_caddy), '--adapter', 'caddyfile'])
        checked([state['frpc'], 'verify', '-c', str(tmp_frpc)])
    finally:
        for tmp_path in tmp_files:
            # 临时校验文件清理失败不应覆盖前面 checked() 抛出的主要校验错误。
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    for path in (caddy_path, frpc_path):
        if path.exists():
            write_private(path.with_suffix(path.suffix + '.previous'), path.read_bytes())
    write_private(caddy_path, caddy_text)
    write_private(frpc_path, frpc)
    if reload:
        # 只访问回环管理接口，核对实例归属后才允许热重载。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        address = f'127.0.0.1:{state["admin_port"]}'
        with opener.open('http://' + address + '/config/storage/root', timeout=5) as response:
            actual = json.load(response)
        if Path(actual).resolve() != (ensure_home() / ('shared-' + name + '-certs')).resolve():
            raise Failure('process_conflict', 'Caddy 不属于此共享实例。', 3)
        checked([state['caddy'], 'reload', '--config', str(caddy_path), '--adapter', 'caddyfile', '--address', address])
        checked([state['frpc'], 'reload', '-c', str(frpc_path)])
    return dict(synced=True, reloaded=reload, site_count=len(state['sites']), proxy_count=len(names))


def run(name, component):
    state = state_for(name)
    with mutation_lock(f'shared-{name}-{component}'):
        caddy_path, frpc_path = config_paths(name)
        if not caddy_path.is_file() or not frpc_path.is_file():
            raise Failure('not_synced', '请先执行 shared sync --apply。', 3)
        config = tomllib.loads(frpc_path.read_text('utf-8'))
        if component == 'frpc':
            tunnel = Client().tunnel(state['tunnel_id'])
            owned_records(tunnel)
            if tunnel['status'] == 1:
                raise Failure('tunnel_in_use', '已有在线客户端，禁止启动第二个 frpc。', 3)
            if {item['name'] for item in config.get('proxies', [])} != {row['proxy_name'] for row in tunnel['proxy_info']}:
                raise Failure('not_synced', '代理集合变化，请先同步。', 3)
            command = [state['frpc'], '-c', str(frpc_path)]
        else:
            command = [state['caddy'], 'run', '--config', str(caddy_path), '--adapter', 'caddyfile']
        values = [state['frpc_admin_password']]
        for section in ('auth', 'metadatas'):
            values.extend(str(value) for value in config.get(section, {}).values() if isinstance(value, (str, int)))
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        print(json.dumps(dict(event='started', component=component, pid=child.pid)), file=sys.stderr, flush=True)
        try:
            for line in child.stdout:
                for value in values:
                    if value:
                        line = line.replace(value, '[REDACTED]')
                sys.stderr.write(line)
            code = child.wait()
            if code:
                raise Failure('runtime_exit', f'共享 {component} 退出码 {code}。')
            return dict(stopped=True, component=component)
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            return dict(stopped=True, component=component)


def probe(address, domain, path='/'):
    if not ipaddress.ip_address(address).is_global:
        raise Failure('invalid_public_address', '不能将内网连接作为公网验证。', 3)
    with socket.create_connection((address, 443), timeout=20) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=domain) as stream:
            request = chr(13) + chr(10)
            stream.sendall(request.join([f'GET {path} HTTP/1.1', f'Host: {domain}', 'Connection: close', '', '']).encode('ascii'))
            response = http.client.HTTPResponse(stream)
            response.begin()
            return dict(tls_verified=True, http_status=response.status, tls_version=stream.version())


def status(client, name):
    state = state_for(name)
    tunnel = client.tunnel(state['tunnel_id'])
    result = dict(name=name, connected=tunnel['status'] == 1, proxy_count=len(tunnel['proxy_info']), sites=[])
    for site_name, site in state['sites'].items():
        item = dict(name=site_name, url='https://' + site['domain'] + '/')
        try:
            item.update(probe(tunnel['node_info']['remote_ip'], site['domain']))
        except (OSError, TimeoutError, http.client.HTTPException):
            item.update(tls_verified=False, reachable=False)
        result['sites'].append(item)
    return result


def migrate(client, name, source_name, site_name, upstream_port, apply):
    state = state_for(name)
    source = load_state(source_name, 'create-')
    if not source or source.get('status') != 'active' or source.get('tunnel_id') == state['tunnel_id']:
        raise Failure('not_owned', '源隧道不是可迁移的自有独立隧道。', 3)
    live = client.tunnel(source['tunnel_id'])
    if live['name'] != source_name or len(live.get('proxy_info', [])) != 1:
        raise Failure('ownership_conflict', '源隧道必须名称不变且只含一条已登记映射。', 3)
    records = owned_records(live)
    row = live['proxy_info'][0]
    record = records[row['id']]
    spec = record['spec']
    if spec['protocol'] != 'https' or row['domain'] != spec.get('sub_domain', '') + '.odn.cc':
        raise Failure('unsupported_migration', '当前迁移仅支持已核验的 odn.cc HTTPS 映射。', 3)
    target = client.tunnel(state['tunnel_id'])
    owned_records(target)
    # 先验证目标参数和运行中的 TLS 底座，再释放不可重复绑定的原域名。
    add_site(client, name, site_name, spec['sub_domain'], spec['domain_id'], upstream_port, None, [], False)
    plan = dict(source=source_name, target=name, domain=row['domain'], source_port=row['local_port'], upstream_port=upstream_port, temporary_interruption=True)
    if not apply:
        return dict(dry_run=True, **plan)
    existing = next((site for site in state['sites'].values() if site.get('status') == 'active'), None)
    if target['status'] != 1 or not existing:
        raise Failure('target_not_ready', '共享网关尚未在线或没有可验证站点，保留旧隧道。', 3)
    health = probe(target['node_info']['remote_ip'], existing['domain'])
    if not 200 <= health['http_status'] < 500:
        raise Failure('target_not_ready', '共享 TLS 底座没有通过公网验证，保留旧隧道。', 3)
    # 保存原参数与归属证据，只有最后一条自有映射才回收整个旧隧道。
    snapshot = dict(plan=plan, creation=source, port=record, status='removing-source')
    save_state(site_name, snapshot, 'migration-')
    try:
        client.request('/user/tunnel/del?' + urllib.parse.urlencode({'tid': source['tunnel_id']}), 'DELETE')
    except Failure:
        remaining = client.request('/user/tunnel/list?page=1&size=100')
        if any(item['tunnel_id'] == source['tunnel_id'] for item in remaining):
            raise
    remaining = client.request('/user/tunnel/list?page=1&size=100')
    if any(item['tunnel_id'] == source['tunnel_id'] for item in remaining):
        raise Failure('reconciliation_required', '旧隧道回收结果未确认，停止后续写入。')
    source['status'] = 'removed'
    save_state(source_name, source, 'create-')
    for path in ensure_home().glob('port-*.json'):
        item = json.loads(path.read_text('utf-8'))
        if item.get('tunnel_id') == source['tunnel_id'] and item.get('port_id') == row['id']:
            item['status'] = 'removed'
            write_private(path, json.dumps(item, ensure_ascii=False, indent=2))
    snapshot['status'] = 'source-removed'
    save_state(site_name, snapshot, 'migration-')
    add_site(client, name, site_name, spec['sub_domain'], spec['domain_id'], upstream_port, None, [], True)
    sync(client, name, True, True)
    snapshot['status'] = 'target-configured'
    save_state(site_name, snapshot, 'migration-')
    return dict(migrated=True, **plan)
