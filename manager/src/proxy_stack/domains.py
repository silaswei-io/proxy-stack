"""Host-side subscription alias lifecycle; stdlib-only exported zip application.

The operation lock is held only for state reads/publication. DNS, ACME and Docker
never block node leases. A separate domain lock serializes lifecycle/recovery.
"""
import argparse
from contextlib import contextmanager
import hashlib
import io
import errno
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from . import discovery, host_setup, render
from .model import DOMAIN, Settings, subscription_endpoint
from .storage import Store, atomic_file

OVERLAY = 'compose.subscription.yaml'
ALIAS = 'runtime/caddy/subscription/alias.Caddyfile'
IMPORT = '\nimport /etc/caddy/subscription/*.Caddyfile\n'


def helper_zip():
    from importlib.resources import files
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in files('proxy_stack').iterdir():
            if path.name.endswith('.py') or path.name == 'bootstrap.json':
                archive.writestr('proxy_stack/' + path.name, path.read_bytes())
        archive.writestr('__main__.py', 'from proxy_stack.domains import main\nraise SystemExit(main())\n')
    return output.getvalue()


def hostname(value):
    value = value.lower()
    if not DOMAIN.fullmatch(value) or value.endswith(('.local', '.localhost', '.internal', '.invalid')):
        raise ValueError('仅支持公共 ASCII DNS 主机名；不要包含协议、端口、路径或 IP')
    return value


def show(store):
    with store.locked(updater=True):
        state = store.state()
        if store.settings(state).role != 'hub':
            raise ValueError('订阅域名只能在中心管理')
        return {'preferred': subscription_endpoint(state).origin, 'original': store.settings(state).origin,
                'domain': state.get('subscription'),
                'recovery_pending': (store.root / 'domain-transaction/journal.json').exists()}


def safe(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path,) + tuple(path.parents)):
        raise ValueError('拒绝符号链接路径')
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('配置必须为独立普通文件')
    return path


def read_optional(path):
    safe(path)
    return path.read_text() if path.exists() else None


def compose_line(text):
    matches = re.findall(r'^COMPOSE_FILE=(.*)$', text, re.M)
    if len(matches) > 1:
        raise ValueError('COMPOSE_FILE 配置重复')
    if not matches:
        return None
    value = matches[0].strip("'\"")
    if not re.fullmatch(r'[a-zA-Z0-9_.:/-]+', value):
        raise ValueError('COMPOSE_FILE 含不支持的语法')
    return value


def with_compose(text, value):
    lines = text.splitlines(keepends=True)
    result = ''.join(line for line in lines if not line.startswith('COMPOSE_FILE='))
    if value is not None:
        if result and not result.endswith('\n'):
            result += '\n'
        result += 'COMPOSE_FILE=' + value + '\n'
    return result


def host_candidate(host, stage, settings, endpoint, identity, removing=False):
    service = host.service()  # Reuses stock systemd/module/process/import checks.
    config = Path(service['config'])
    original = config.read_text()
    site = render.subscription_site(endpoint, settings, identity)
    begin, end = '# BEGIN ' + identity, '# END ' + identity
    baseline = host.adapt(service['binary'], config, original)
    if removing:
        if original.count(begin) != 1 or original.count(end) != 1:
            raise ValueError('宿主托管站点标记缺失或重复；保留原配置')
        start = original.index(begin)
        stop = original.index(end, start) + len(end)
        if original[start:stop] + '\n' != site:
            raise ValueError('宿主托管站点已被修改；请人工审查')
        if start and original[start - 1] == '\n':
            start -= 1  # Remove only the separator introduced by our append.
        candidate = original[:start] + original[stop:].removeprefix('\n')
    else:
        # Unknown exact aliases, wildcard/catch-all routes and global access logs
        # require manual review. Never claim ownership merely because DNS matches.
        for server in baseline.get('apps', {}).get('http', {}).get('servers', {}).values():
            if server.get('logs'):
                raise ValueError('宿主配置启用了访问日志；请先审查订阅私密路径的日志隔离')
            for route in server.get('routes', []):
                names = host_setup.hosts(route.get('match', []))
                if not names or any(n == endpoint.domain or n.startswith('*.') and endpoint.domain.endswith(n[1:]) for n in names):
                    raise ValueError('域名已有宿主站点或 catch-all；未接管。请按文档迁移已有别名')
        listeners = host.run(['ss', '-H', '-ltnp', 'sport = :' + str(endpoint.port)])
        if not listeners.strip() or any(set(re.findall(r'pid=(\d+)', line)) != {service['pid']} for line in listeners.splitlines()):
            raise ValueError('HTTPS 端口不属于受支持的宿主 Caddy')
        candidate = original + '\n' + site
    return host_setup.plan_text(host, stage, candidate, identity, service=service, removing=removing,
                                alias_subject=endpoint.domain)


class Deployment:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.store = Store(self.directory / 'data')
        self.stage = self.store.root / 'domain-transaction'
        self.host = host_setup.Host()

    def run(self, argv, timeout=30):
        try:
            result = subprocess.run(argv, cwd=self.directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout, env={k: v for k, v in os.environ.items()
                                    if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')})
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('域名操作命令不可用或超时：' + Path(argv[0]).name) from None
        if result.returncode:
            raise ValueError('域名操作命令失败：' + Path(argv[0]).name + '；原始诊断未输出以保护私密路径')
        return result.stdout.decode()

    def compose(self, *args, timeout=40):
        return self.run(['docker', 'compose', *args], timeout)

    def preflight(self, recovering=False):
        for name in ('.env', 'compose.yaml', 'compose.external.yaml', OVERLAY):
            safe(self.directory / name)
        endpoint = self.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
        if not endpoint.startswith('unix://') or os.environ.get('DOCKER_HOST', endpoint) != endpoint:
            raise ValueError('域名管理仅支持本机 Unix socket Docker')
        if any(os.environ.get(k) for k in ('COMPOSE_FILE', 'COMPOSE_ENV_FILES', 'COMPOSE_PATH_SEPARATOR', 'COMPOSE_DISABLE_ENV_FILE')):
            raise ValueError('请清除外部 Compose 文件/环境覆盖后操作')
        env_text = (self.directory / '.env').read_text()
        for name in (compose_line(env_text) or 'compose.yaml').split(':'):
            safe(self.directory / name)
        for name in self.store.runtime.joinpath('caddy/subscription').iterdir():
            if name.name != 'alias.Caddyfile':
                raise ValueError('存在未知订阅 Caddy import 文件；请先人工审查')
            safe(name)
        self.config = json.loads(self.compose('--profile', 'tools', 'config', '--format', 'json'))
        for service in ('manager', 'caddy'):
            if service not in self.config['services']:
                raise ValueError('缺少受支持的 Compose 服务')
        # Prove this manager and Caddy actually mount THIS state/public/config.
        expected = {'manager': ('model', 'public', 'cache', 'locks', 'runtime'), 'caddy': ('public', 'runtime/caddy')}
        for service, names in expected.items():
            volumes = self.config['services'][service].get('volumes', [])
            for name in names:
                target = '/etc/caddy' if name == 'runtime/caddy' else '/srv/public' if service == 'caddy' else '/data/' + name
                source = str(self.store.root / name)
                if not any(v.get('type') == 'bind' and v.get('source') == source and v.get('target') == target for v in volumes):
                    raise ValueError('Compose 挂载与当前安装不一致')
        if not recovering and not self.compose('ps', '--status', 'running', '-q', 'caddy').strip():
            raise ValueError('请先启动现有 Caddy 服务再管理订阅域名')
        with self.store.locked():
            state = self.store.state()
            if self.store.settings(state).role != 'hub':
                raise ValueError('订阅域名只能在中心管理')
        expected_env = {'DOMAIN': self.store.settings(state).domain, 'PUBLIC_PORT': str(self.store.settings(state).port),
                        'MODE': self.store.settings(state).mode, 'ROLE': 'hub'}
        actual_env = self.config['services']['manager'].get('environment', {})
        if any(str(actual_env.get(k, '')) != value for k, value in expected_env.items()):
            raise ValueError('Compose 环境与持久设置不一致；请先恢复原安装环境')
        return state

    def port_free(self, port):
        # Check host sockets AND Docker bindings; Desktop's host bind alone
        # does not reliably see Docker's port reservations.
        sockets = []
        try:
            for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
                try:
                    item = socket.socket(family, socket.SOCK_STREAM)
                    sockets.append(item)
                    if family == socket.AF_INET6:
                        item.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    item.bind((address, port))
                except OSError as error:
                    if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT):
                        continue
                    return False
        finally:
            for item in sockets:
                item.close()
        image = self.config['services']['caddy']['image']
        try:
            self.run(['docker', 'run', '--rm', '--network', 'bridge', '--read-only', '--cap-drop=ALL',
                      '-p', str(port) + ':8080', '--entrypoint', 'caddy', image, 'version'])
            return True
        except ValueError:
            return False

    def validate_container(self, changes):
        image = self.config['services']['caddy']['image']
        # Full original config + exact alias, on an isolated native Caddy.
        with tempfile.TemporaryDirectory(prefix='proxy-stack-domain-') as temporary:
            directory = Path(temporary)
            for filename in ('Caddyfile', 'external.Caddyfile', 'subscription/alias.Caddyfile'):
                path = self.store.runtime / 'caddy' / filename
                value = changes.get(str(path), {}).get('after', read_optional(path))
                if value is not None:
                    (directory / filename).parent.mkdir(parents=True, exist_ok=True)
                    (directory / filename).write_text(value)
            config = 'external.Caddyfile' if self.settings.mode == 'existing-caddy' else 'Caddyfile'
            self.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
                      '--cap-add=NET_BIND_SERVICE', '--tmpfs', '/data', '--tmpfs', '/config', '--tmpfs', '/tmp',
                      '-v', str(directory) + ':/etc/caddy:ro', image, 'caddy', 'validate',
                      '--config', '/etc/caddy/' + config, '--adapter', 'caddyfile'])

    def activate(self, backend, stage, recreate=False):
        if backend == 'host':
            host_setup.apply(self.host, stage / 'host')
        elif recreate:
            self.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'caddy', timeout=60)
        else:
            # admin is intentionally off, so restart only the Caddy container.
            self.compose('restart', 'caddy', timeout=60)

    def confirm(self, origin, wait=120):
        # Bound the entire TLS check, including DNS/slow readers/lock contention.
        # No network request holds the operation lock or leaks URL exceptions.
        import multiprocessing
        from .cli import verify
        context = multiprocessing.get_context('fork')
        deadline = time.monotonic() + wait
        def probe(pipe):
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                verify(self.store, origin)
                pipe.send(True)
            except Exception:
                pipe.send(False)
            finally:
                pipe.close()
        while time.monotonic() < deadline:
            reader, writer = context.Pipe(duplex=False)
            child = context.Process(target=probe, args=(writer,), daemon=True)
            child.start()
            writer.close()
            success = False
            try:
                if reader.poll(min(15, max(0, deadline - time.monotonic()))):
                    try:
                        success = reader.recv()
                    except EOFError:
                        pass
            finally:
                if child.is_alive():
                    child.terminate()
                child.join(timeout=.2)
                if child.is_alive():
                    child.kill()
                    child.join(timeout=.2)
                reader.close()
            if success:
                return
            print('等待 HTTPS 证书及订阅资源校验…', flush=True)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise ValueError('HTTPS/订阅内容验证失败；执行事务回滚')

    def save(self, value):
        host_setup.save(self.stage / 'journal.json', value)

    def apply_files(self, value, reverse=False, check_only=False):
        changes = list(value['files'].items())
        if reverse:
            changes.reverse()
        for name, change in changes:
            path = safe(Path(name))
            if path == self.directory / '.env':
                current = read_optional(path)
                expected = compose_line(change['after' if reverse else 'before'])
                target = compose_line(change['before' if reverse else 'after'])
                if compose_line(current) == target:
                    continue
                if compose_line(current) != expected:
                    raise ValueError('COMPOSE_FILE 被其他操作修改；保留事务，请审查后 domain recover')
                original = change['after' if reverse else 'before']
                desired = change['before' if reverse else 'after']
                if not check_only:
                    atomic_file(path, desired if current == original else with_compose(current, target))
                continue
            before, after = (change['after'], change['before']) if reverse else (change['before'], change['after'])
            current = read_optional(path)
            if current == after:
                continue
            if current != before:
                raise ValueError('配置被其他操作修改；保留事务，拒绝覆盖并发变更。请审查后 domain recover')
            if check_only:
                continue
            if after is None:
                path.unlink(missing_ok=True)
            else:
                atomic_file(path, after)

    def selection(self, expected, target):
        with self.store.locked():
            state = self.store.state()
            if state.get('subscription') == target:
                # Reconcile assets after interruption between model and switch.
                self.store.commit(state, self.store.read(self.store.cache / 'rules.json'), subscription=True)
                return
            if state.get('subscription') != expected:
                raise ValueError('订阅首选项被其他操作修改；拒绝覆盖')
            if target is None:
                state.pop('subscription', None)
            else:
                state['subscription'] = target
            self.store.commit(state, self.store.read(self.store.cache / 'rules.json'), subscription=True)

    def recover(self):
        safe(self.stage / 'journal.json')
        value = self.store.read(self.stage / 'journal.json')
        self.settings = self.store.settings(self.store.state())
        if value['phase'] == 'complete':
            self.finish_journal()
            return
        # Restore only our CAS-guarded files, then prove the restored Compose
        # identity/mounts even if the failed recreation left Caddy stopped.
        self.apply_files(value, reverse=True, check_only=True)
        self.apply_files(value, reverse=True)
        self.preflight(recovering=True)
        # Preserve newly enrolled nodes/friends/chains while restoring origin.
        self.selection(value['after'], value['before'])
        if (self.stage / 'host/journal.json').exists():
            host_setup.rollback(self.host, self.stage / 'host')
        if value['backend'] == 'container':
            self.activate('container', self.stage, True)
        self.finish_journal()

    def finish_journal(self):
        import shutil
        shutil.rmtree(self.stage)

    @contextmanager
    def installation_guard(self, action):
        # Share the installer's mkdir lock: reruns cannot stop services or
        # snapshot a half-completed domain operation. flock serializes recovery.
        lock = self.directory.with_name(self.directory.name + '.install-lock')
        marker = lock / 'domain-owner'
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            if action != 'recover' or read_optional(marker) != str(self.directory):
                raise ValueError('另一个安装/域名事务持有目录锁；先完成或恢复')
        else:
            atomic_file(marker, str(self.directory))
        try:
            yield
        finally:
            if not (self.stage / 'journal.json').exists():
                safe(marker).unlink(missing_ok=True)
                lock.rmdir()

    def operate(self, action, domain=None, port=443):
        if action not in ('add', 'remove', 'recover'):
            raise ValueError('无效域名操作')
        if action == 'add':
            domain = hostname(domain)
            endpoint = Settings(domain, 'Subscription', port=port)
            if port == 80:
                raise ValueError('80 保留用于 HTTP-01；请选择其他 HTTPS 端口')
        with self.store.locked(operation='domain.lock'), self.installation_guard(action):
            if action == 'recover':
                state = self.store.state()
                if self.store.settings(state).role != 'hub':
                    raise ValueError('订阅域名只能在中心管理')
            else:
                if (self.stage / 'journal.json').exists():
                    raise ValueError('发现未完成事务；请先运行 proxyctl domain recover')
                state = self.preflight()
            self.settings = self.store.settings(state)
            if action == 'recover':
                if (self.stage / 'journal.json').exists():
                    self.recover()
                return show(self.store)
            if (self.stage / 'journal.json').exists():
                raise ValueError('发现未完成事务；请先运行 proxyctl domain recover')
            previous = state.get('subscription')
            if action == 'add':
                if domain == self.settings.domain:
                    raise ValueError('订阅别名必须与原始地址不同')
                if previous:
                    if (previous['domain'], previous['port']) == (domain, port):
                        discovery.check_address(domain)
                        self.confirm(subscription_endpoint(state).origin)
                        return show(self.store)
                    raise ValueError('已有首选订阅域名；请先移除')
                discovery.check_address(domain)
                own_port = any(str(p.get('published')) == str(port) and p.get('target') == port
                               for p in self.config['services']['caddy'].get('ports', []))
                backend = 'container' if own_port or self.port_free(port) else 'host'
                identity = 'proxy-stack-sub-' + hashlib.sha256(str(self.directory).encode()).hexdigest()[:16]
                target = {'domain': domain, 'port': port, 'backend': backend, 'identity': identity}
            else:
                if not previous:
                    return show(self.store)
                endpoint = subscription_endpoint(state)
                port = endpoint.port
                backend, identity, target = previous['backend'], previous['identity'], None
            files = {}
            def change(path, after):
                files[str(path)] = {'before': read_optional(path), 'after': after}
            recreate = False
            if backend == 'container':
                alias = self.store.root / ALIAS
                site = render.subscription_site(endpoint)
                if action == 'add' and read_optional(alias) is not None:
                    raise ValueError('已有未知订阅 Caddy 文件；拒绝覆盖')
                if action == 'remove' and read_optional(alias) != site:
                    raise ValueError('托管订阅 Caddy 文件不匹配；保留原配置')
                change(alias, site if action == 'add' else None)
                env = self.directory / '.env'
                text = env.read_text()
                current = compose_line(text)
                overlay = self.directory / OVERLAY
                if action == 'add' and not own_port:
                    if read_optional(overlay) is not None:
                        raise ValueError('已有未知订阅 Compose 文件；拒绝覆盖')
                    change(overlay, json.dumps({'services': {'caddy': {'ports': [str(port) + ':' + str(port)]}}}) + '\n')
                    base = current or ('compose.yaml:compose.external.yaml' if self.settings.mode == 'existing-caddy' else 'compose.yaml')
                    change(env, with_compose(text, base + ':' + OVERLAY))
                    recreate = True
                elif action == 'remove' and read_optional(overlay) is not None:
                    expected = json.dumps({'services': {'caddy': {'ports': [str(port) + ':' + str(port)]}}}) + '\n'
                    if overlay.read_text() != expected or not current or current.split(':')[-1] != OVERLAY:
                        raise ValueError('托管 Compose 文件已改变；请人工审查')
                    change(overlay, None)
                    change(env, with_compose(text, ':'.join(current.split(':')[:-1])))
                    recreate = True
                for name, external in (('Caddyfile', False), ('external.Caddyfile', True)):
                    path = self.store.runtime / 'caddy' / name
                    text = path.read_text()
                    expected = render.caddy(self.settings, state, external)
                    if text not in (expected, expected.removesuffix(IMPORT)):
                        raise ValueError('原 Caddy 文件含未知修改；拒绝覆盖')
                    if text != expected:
                        change(path, expected)
                self.validate_container(files)
            # All DNS/native validation/conflict checks precede persistent writes.
            if self.stage.exists():
                # An interrupted preflight has no live mutations/journal.
                import shutil
                safe(self.stage / 'journal.json')
                shutil.rmtree(self.stage)
            self.stage.mkdir(mode=0o700)
            value = {'schema': 1, 'phase': 'prepared', 'before': previous, 'after': target,
                     'files': files, 'backend': backend, 'recreate': recreate}
            try:
                if backend == 'host':
                    host_candidate(self.host, self.stage / 'host', self.settings, endpoint, identity, action == 'remove')
                self.save(value)
                self.apply_files(value)
                self.activate(backend, self.stage, recreate)
                if action == 'add':
                    self.confirm(endpoint.origin)
                else:
                    self.confirm(self.settings.origin)
                self.selection(previous, target)
                self.confirm(endpoint.origin if target else self.settings.origin)
                value['phase'] = 'complete'
                self.save(value)
                self.finish_journal()
            except BaseException:
                if (self.stage / 'journal.json').exists():
                    try:
                        self.recover()
                        print('域名操作失败，本次变更已回滚；原地址保留。', file=sys.stderr)
                    except BaseException:
                        print('域名操作中断；事务已保留。审查并发配置后运行 proxyctl domain recover。', file=sys.stderr)
                raise
            return show(self.store)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='proxyctl domain')
    parser.add_argument('--directory', default='.')
    sub = parser.add_subparsers(dest='action', required=True)
    add = sub.add_parser('add')
    add.add_argument('domain')
    add.add_argument('--port', type=int, default=443)
    for name in ('show', 'list', 'remove', 'recover'):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    def interrupted(*_):
        raise KeyboardInterrupt()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        deployment = Deployment(args.directory)
        if args.action in ('show', 'list'):
            result = show(deployment.store)
        else:
            result = deployment.operate(args.action, getattr(args, 'domain', None), getattr(args, 'port', 443))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print('proxy-stack: ' + (str(error) if isinstance(error, (ValueError, host_setup.Refused)) else type(error).__name__), file=sys.stderr)
        return 1
