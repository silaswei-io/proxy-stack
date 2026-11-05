"""Small, bounded completion probe. Private links only leave on verified success."""
import base64
import hashlib
import http.client
import os
import multiprocessing
import shlex
import ssl
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from .cli import NoRedirect
from .storage import Store
from .routes import asset_paths, labelled_paths


ATTEMPT_SECONDS = 4
MAX_ASSET = 2 * 1024 * 1024
PROGRESS_SECONDS = 10
REASONS = {
    'timeout': '连接或检查超时；检查所选 HTTPS 端口的云防火墙、NAT 和服务监听。',
    'connect': '连接失败；检查所选 HTTPS 端口的云防火墙、NAT、DNS 和服务监听。',
    'tls': 'TLS 验证失败；检查证书签发、有效期及 HTTP-01 的公网 80 路由。',
    'http': 'HTTP 响应不符合要求；检查 HTTPS 路由、重定向及响应头。',
    'content': 'HTTPS 内容不匹配；检查是否路由到本安装及当前订阅内容。',
    'state': '本机状态不可用或检查期间变化；检查本机状态和服务后重试。',
    'node': '节点运行同步待完成；检查节点服务、当前配置回执和租约。',
}


class ProbeFailure(Exception):
    pass


def failure_reason(error, phase):
    # Inspect types only: exception messages/URLs/paths must never cross the pipe.
    if isinstance(error, ProbeFailure):
        return error.args[0] if error.args[0] in REASONS else 'state'
    if phase == 'state':
        return 'state'
    if isinstance(error, urllib.error.HTTPError):
        return 'http'
    if isinstance(error, urllib.error.URLError):
        return failure_reason(error.reason, phase) if isinstance(error.reason, BaseException) else 'connect'
    if isinstance(error, ssl.SSLError):
        return 'tls'
    if isinstance(error, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(error, http.client.HTTPException):
        return 'http'
    if isinstance(error, OSError):
        return 'connect'
    return 'state'


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
        paths = asset_paths(state['users']['owner']['token'])
        path, name, _ = paths[0]
        manifest = store.read(store.current() / 'manifest.json')
        result.update(url=settings.origin + path, expected=manifest['files'][name],
                      rules=manifest['quality'],
                      links=[(label, settings.origin + p) for label, p in labelled_paths(state['users']['owner']['token'])])
        return result


def probe(root, pipe):
    phase = 'state'
    try:
        store = Store(root)
        before = snapshot(store)
        pipe.send({'phase': 'node' if before['role'] == 'node' else 'https',
                   'reason': 'timeout',
                   'role': before['role'], 'mode': before['mode'], 'nodes': before.get('nodes', 0), 'rules': before.get('rules'),
                   'node_ready': before.get('node_ready', False),
                   'local_required': before.get('local_required', False), 'local_ready': before.get('local_ready', False)})
        if before['role'] == 'node':
            phase = 'node'
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
                    raise ProbeFailure('http')
            finally:
                connection.close()
            phase = 'state'
            pipe.send({'phase': 'state', 'reason': 'state'})
            after = snapshot(store)
            matched = all(before[k] == after[k] for k in ('origin', 'ws_path'))
            pipe.send({'phase': 'node', 'role': 'node', 'https': matched, 'node_ready': after['node_ready'],
                       'ready': matched and after['node_ready'],
                       'reason': 'node' if matched else 'state'})
            return
        phase = 'https'
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        # No caller-supplied origin, insecure TLS switch, redirects or env proxies.
        with opener.open(urllib.request.Request(before['url']), timeout=2) as response:
            if (response.status != 200 or response.headers.get_content_type() != 'application/yaml'
                    or response.headers.get('Cache-Control') != 'no-store'):
                raise ProbeFailure('http')
            body = response.read(MAX_ASSET + 1)
            if len(body) > MAX_ASSET or hashlib.sha256(body).hexdigest() != before['expected']:
                raise ProbeFailure('content')
        phase = 'state'
        pipe.send({'phase': 'state', 'reason': 'state'})
        after = snapshot(store)
        # A concurrent publication/revocation must not endorse the old response.
        matched = after.get('url') == before['url'] and after.get('expected') == before['expected']
        result = {k: v for k, v in after.items() if k not in ('url', 'expected', 'links')}
        result.update(phase='https', https=matched, ready=matched and after['nodes'] > 0,
                      reason='node' if matched else 'state')
        if result['ready']:
            result['url'] = after['url']
            result['links'] = after['links']
        pipe.send(result)
    except Exception as error:
        # Never serialize raw exceptions: HTTP errors may contain private paths.
        pipe.send({'failed': True, 'reason': failure_reason(error, phase)})
    finally:
        pipe.close()


def check(root, wait=0, https_only=False, progress=None):
    """Hard wall-clock cap includes DNS, slow reads and blocked file locks.

    No lock survives an attempt. A zero wait runs one probe (at most 4 seconds);
    retry mode has a total budget of wait seconds, including all probes/sleeps.
    """
    started = time.monotonic()
    deadline = started + (wait or ATTEMPT_SECONDS)
    next_progress = started + PROGRESS_SECONDS
    result = {'ready': False, 'phase': 'state'}
    context = multiprocessing.get_context('fork')
    while True:
        reader, writer = context.Pipe(duplex=False)
        child = context.Process(target=probe, args=(root, writer), daemon=True)
        child.start()
        writer.close()
        end = min(deadline, time.monotonic() + ATTEMPT_SECONDS)
        result = {'ready': False, 'phase': 'state', 'reason': 'state'}
        try:
            while time.monotonic() < end:
                now = time.monotonic()
                if progress and now >= next_progress:
                    progress(int(now - started), result)
                    next_progress = now + PROGRESS_SECONDS
                poll_end = min(end, next_progress) if progress else end
                if not reader.poll(max(0, poll_end - time.monotonic())):
                    continue
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
        if progress and not success and time.monotonic() >= next_progress:
            progress(int(time.monotonic() - started), result)
            next_progress = time.monotonic() + PROGRESS_SECONDS
        if success or not wait or time.monotonic() >= deadline:
            return result
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return result


def completion_ready(result):
    if result.get('role') == 'node':
        return result['ready']
    return bool(result.get('https') and (not result.get('local_required') or result.get('local_ready')))


def finish(store, wait=0, command='./proxyctl', https_only=False, silent=False, progress=False):
    def report(elapsed, result):
        reason = result.get('reason', 'state')
        print(f'HTTPS 完成检查：已等待 {elapsed} 秒；{reason}：' + REASONS.get(reason, REASONS['state']), flush=True)
    if progress:
        result = check(str(store.root), wait, https_only, progress=report)
    else:
        result = check(str(store.root), wait, https_only)
    success = completion_ready(result) if https_only else result["ready"]
    if progress and not success:
        reason = result.get('reason', 'state')
        print('完成检查失败（' + reason + '）：' + REASONS.get(reason, REASONS['state']), flush=True)
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
            for label, url in result['links']:
                print(label + '（私密）：' + url)
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
