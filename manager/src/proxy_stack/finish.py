"""Small, bounded completion probe. Private links only leave on verified success."""
import base64
import hashlib
import http.client
import os
import multiprocessing
import shlex
import ssl
import time
import urllib.request
from pathlib import Path

from .cli import NoRedirect
from .storage import Store


ATTEMPT_SECONDS = 4
MAX_ASSET = 2 * 1024 * 1024


def snapshot(store):
    # Only local reads under the lock. The caller bounds even lock acquisition.
    with store.locked(updater=True):
        state = store.state()
        settings = store.settings(state)
        result = {'role': settings.role, 'mode': settings.mode}
        if settings.role == 'node':
            path = store.runtime / 'node-status.json'
            status = store.read(path) if path.exists() else {}
            age = time.time() - status.get('at', 0)
            result['origin'] = settings.origin
            result['host'] = settings.domain
            result['port'] = settings.port
            result['ws_path'] = state['ws_path']
            result['node_ready'] = bool(
                (store.model / 'peer.json').exists() and status.get('running') and
                not status.get('error') and 0 <= age <= 6 and
                status.get('lease_remaining', 0) > age and
                status.get('generation', 0) > 0 and status['generation'] == status.get('desired'))
            return result
        beats = store.read(store.model / 'heartbeats.json') if (store.model / 'heartbeats.json').exists() else {}
        now = time.time()
        result['nodes'] = sum(1 for key, node in state['nodes'].items()
                              if node['enabled'] and node['ready'] and node['acked'] == node['desired']
                              and 0 <= now - beats.get(key, 0) < settings.lease_seconds)
        local = state['nodes'].get('local', {})
        result['local_required'] = bool(settings.local_node and local.get('enabled'))
        result['local_ready'] = bool(local.get('ready') and local.get('acked') == local.get('desired')
                                     and 0 <= now - beats.get('local', 0) < settings.lease_seconds)
        name = 'sub/' + state['users']['owner']['token'] + '/clash.yaml'
        manifest = store.read(store.current() / 'manifest.json')
        result.update(url=settings.origin + '/' + name, expected=manifest['files'][name],
                      rules=manifest['quality'])
        return result


def probe(root, pipe):
    try:
        store = Store(root)
        before = snapshot(store)
        pipe.send({'phase': 'node' if before['role'] == 'node' else 'https',
                   'role': before['role'], 'mode': before['mode'], 'nodes': before.get('nodes', 0), 'rules': before.get('rules'),
                   'node_ready': before.get('node_ready', False),
                   'local_required': before.get('local_required', False), 'local_ready': before.get('local_ready', False)})
        if before['role'] == 'node':
            key = base64.b64encode(os.urandom(16)).decode()
            connection = http.client.HTTPSConnection(before['host'], before['port'], timeout=2,
                                                     context=ssl.create_default_context())
            try:
                connection.request('GET', before['ws_path'], headers={
                    'Upgrade': 'websocket', 'Connection': 'Upgrade',
                    'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': key})
                response = connection.getresponse()
                expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                if (response.status != 101 or response.getheader('Upgrade', '').lower() != 'websocket'
                        or response.getheader('Sec-WebSocket-Accept') != expected):
                    raise ValueError('wrong public websocket endpoint')
            finally:
                connection.close()
            after = snapshot(store)
            matched = all(before[k] == after[k] for k in ('origin', 'ws_path'))
            pipe.send({'role': 'node', 'https': matched, 'node_ready': after['node_ready'],
                       'ready': matched and after['node_ready']})
            return
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        # No caller-supplied origin, insecure TLS switch, redirects or env proxies.
        with opener.open(urllib.request.Request(before['url']), timeout=2) as response:
            if (response.status != 200 or response.headers.get_content_type() != 'application/yaml'
                    or response.headers.get('Cache-Control') != 'no-store'):
                raise ValueError('invalid response')
            body = response.read(MAX_ASSET + 1)
            if len(body) > MAX_ASSET or hashlib.sha256(body).hexdigest() != before['expected']:
                raise ValueError('wrong subscription')
        after = snapshot(store)
        # A concurrent publication/revocation must not endorse the old response.
        matched = after.get('url') == before['url'] and after.get('expected') == before['expected']
        result = {k: v for k, v in after.items() if k not in ('url', 'expected')}
        result.update(https=matched, ready=matched and after['nodes'] > 0)
        if result['ready']:
            result['url'] = after['url']
        pipe.send(result)
    except Exception:
        # Never serialize raw exceptions: HTTP errors may contain private paths.
        pipe.send({'failed': True})
    finally:
        pipe.close()


def check(root, wait=0, https_only=False):
    """Hard wall-clock cap includes DNS, slow reads and blocked file locks.

    No lock survives an attempt. A zero wait runs one probe (at most 4 seconds);
    retry mode has a total budget of wait seconds, including all probes/sleeps.
    """
    deadline = time.monotonic() + (wait or ATTEMPT_SECONDS)
    result = {'ready': False, 'phase': 'state'}
    context = multiprocessing.get_context('fork')
    while True:
        reader, writer = context.Pipe(duplex=False)
        child = context.Process(target=probe, args=(root, writer), daemon=True)
        child.start()
        writer.close()
        end = min(deadline, time.monotonic() + ATTEMPT_SECONDS)
        result = {'ready': False, 'phase': 'state'}
        try:
            while time.monotonic() < end:
                if not reader.poll(max(0, end - time.monotonic())):
                    break
                try:
                    message = reader.recv()
                except EOFError:
                    break
                result.update(message)
                if 'ready' in message or message.get('failed'):
                    break
        finally:
            if child.is_alive():
                child.terminate()
            child.join(timeout=0.2)
            if child.is_alive():
                child.kill()
                child.join(timeout=0.2)
            reader.close()
        success = completion_ready(result) if https_only else result['ready']
        if success or not wait or time.monotonic() >= deadline:
            return result
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return result


def completion_ready(result):
    if result.get('role') == 'node':
        return result['ready']
    return bool(result.get('https') and (not result.get('local_required') or result.get('local_ready')))


def finish(store, wait=0, command='./proxyctl', https_only=False, silent=False):
    result = check(str(store.root), wait, https_only)
    success = completion_ready(result) if https_only else result["ready"]
    if silent:
        return 0 if success else 1
    command_text = shlex.quote(command)
    if result.get('phase') == 'state':
        print('待完成：本机状态检查超时或不可用。')
        print('请检查服务和本机状态后重试。')
        print('本机状态：' + command_text + ' status')
    elif result.get('role') == 'node':
        print('节点就绪：公共 HTTPS/WSS、本机运行及同步已确认。' if result['ready'] else
              '待完成：HTTPS/WSS ' + ('已验证' if result.get('https') else '待验证') +
              '；本机运行/同步' + ('已确认。' if result.get('node_ready') else '待确认。'))
        print('统一订阅链接请从中心获取；中心 node list 可查看回执。')
        print('本机状态：' + command_text + ' status')
    else:
        nodes = result.get('nodes', 0)
        if result['ready']:
            print(f'订阅就绪：HTTPS 内容已校验；已确认节点 {nodes}。')
            print('Mihomo（私密）：' + result['url'])
            print('QX / 其他链接：菜单「我的订阅」。')
        else:
            https = '已验证' if result.get('https') else '待验证'
            node = f'已确认 {nodes}' if nodes else '等待就绪节点回执'
            print(f'待完成：HTTPS {https}；节点：{node}。')
            if result.get('mode') == 'existing-caddy' and not result.get('https'):
                snippet = str(Path(command).parent / 'data/runtime/host-caddy.snippet')
                print('已有反代：审查 ' + shlex.quote(snippet))
                print('手动合并 HTTPS 路由及片段中的 TLS/global 配置。')
            elif result.get('https'):
                print('在中心菜单添加节点，并查看节点同步状态。')
            else:
                print('请检查 HTTPS 路由/证书及节点同步状态。')
        print('规则：' + {'upstream': 'upstream。', 'minimal-bootstrap': 'minimal-bootstrap；等待后台刷新。'}.get(result.get('rules'), '待检查。'))
    print('重试：' + command_text + ' finish')
    print('菜单：' + command_text + ' menu')
    return 0 if success else 1
