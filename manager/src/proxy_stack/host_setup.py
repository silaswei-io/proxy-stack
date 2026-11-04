"""Host-side stock systemd Caddy HTTP-01 adapter. Standalone, stdlib-only.

Exported into a private transaction directory; never imported by runtime services.
Only the recognized service's Caddyfile is replaced, never its binary or unit.
"""
import argparse
import glob
import hashlib
import json
import os
import platform
import pwd
import re
import signal
import shlex
import stat
import subprocess
import sys
import time
import tempfile
import urllib.request
from pathlib import Path


class Refused(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600, uid=None, gid=None):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.proxy-stack-' + os.urandom(8).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            if uid is not None:
                os.fchown(out.fileno(), uid, gid)
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def save(path, value):
    atomic(path, json.dumps(value, sort_keys=True).encode())


def tokens(text):
    """Position-preserving lexer: braces in comments/quoted strings aren't blocks."""
    result, i = [], 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        start = i
        if text[i] in ('"', '`'):
            quote, i = text[i], i + 1
            value = ''
            while i < len(text) and text[i] != quote:
                if text[i] == '\\' and quote == '"':
                    i += 1
                if i >= len(text):
                    raise Refused('Caddyfile 引号不完整')
                value += text[i]
                i += 1
            if i == len(text):
                raise Refused('Caddyfile 引号不完整')
            i += 1
        else:
            while i < len(text) and not text[i].isspace() and text[i] != '#':
                i += 1
            value = text[start:i]
        if value.startswith('<<') or '{$' in value:
            raise Refused('不支持含 heredoc/环境替换的宿主配置')
        result.append((value, start, i))
    depth = 0
    for value, _, _ in result:
        depth += (value == '{') - (value == '}')
        if depth < 0:
            raise Refused('Caddyfile 大括号不匹配')
    if depth:
        raise Refused('Caddyfile 大括号不匹配')
    return result


def challenge_site(domain, port, identity):
    host = '[' + domain + ']' if ':' in domain else domain
    # HTTP only; no new TLS automation/default_sni/logging changes on host Caddy.
    return (f'\n# BEGIN proxy-stack-http01 {identity}\nhttp://{host}:80 {{\n'
            f'    @proxy_stack_acme path /.well-known/acme-challenge/*\n'
            f'    handle @proxy_stack_acme {{\n        reverse_proxy 127.0.0.1:{port}\n    }}\n'
            f'    handle {{\n        respond "Not found" 404\n    }}\n'
            f'}}\n# END proxy-stack-http01 {identity}\n')


def merge(text, site, identity):
    tokens(text)  # Native adapt below also validates imported/expanded syntax.
    begin, end = '# BEGIN proxy-stack-http01 ' + identity, '# END proxy-stack-http01 ' + identity
    if begin in text or end in text:
        if text.count(begin) != 1 or text.count(end) != 1:
            raise Refused('已有托管 HTTP-01 区块不明确')
        start = text.index(begin)
        stop = text.index(end, start) + len(end)
        if text[start:stop].strip() != site.strip():
            raise Refused('已有托管 HTTP-01 配置不同；保留原文件')
        return text
    # No global block is introduced/changed: high-port TLS stays inside the stack.
    return text + site


def stock_modules(output):
    counts, modules = {}, []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.fullmatch(r'(Standard|Non-standard|Unknown) modules: (\d+)', line)
        if match:
            counts[match[1]] = int(match[2])
            continue
        parts = line.split()
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', parts[0]):
            return False
        if len(parts) > 1 and (len(parts) != 3 or not re.fullmatch(r'github.com/caddyserver/caddy/v2(?:/.*)?', parts[-1])):
            return False
        modules.append(parts)
    if not modules or len({line[0] for line in modules}) != len(modules):
        return False
    if counts.get('Non-standard', 0) or counts.get('Unknown', 0) or counts.get('Standard', len(modules)) != len(modules):
        return False
    if any(len(line) == 1 for line in modules):
        # Debian's stock 2.6 build omits build metadata even with these flags.
        return counts == {'Standard': len(modules), 'Non-standard': 0, 'Unknown': 0} and all(len(line) == 1 for line in modules)
    return True


class Host:
    def run(self, argv, data=None, cwd=None, timeout=20):
        try:
            result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=cwd, timeout=timeout, check=False,
                                    env={k: v for k, v in os.environ.items()
                                         if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')})
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('宿主命令不可用或超时：' + Path(argv[0]).name) from None
        if result.returncode:
            # Never echo Caddy diagnostics/config lines or HTTP URL exceptions.
            raise Refused('宿主命令失败：' + Path(argv[0]).name + '（退出 ' + str(result.returncode) + '）')
        return result.stdout.decode()

    def safe(self, path, writable=False):
        path = Path(path)
        for parent in (path,) + tuple(path.parents):
            st = parent.lstat()
            if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
                raise Refused('宿主文件/父目录需 root 所有且不可被其他用户写入')
        if not path.is_file() or path.stat().st_nlink != 1:
            raise Refused('宿主配置/程序不是独立普通文件')
        if writable and (not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK)):
            raise Refused('宿主配置不可原子写入')
        if writable and os.statvfs(path.parent).f_flag & os.ST_RDONLY:
            raise Refused('宿主配置文件系统只读')
        # Preserve traditional permissions; ACL/xattr-controlled configs need review.
        if writable and os.listxattr(path):
            raise Refused('不支持含 ACL/xattr 的宿主配置')
        return path

    def process(self, pid):
        process = Path('/proc/' + pid)
        return (process.joinpath('cmdline').read_bytes().rstrip(b'\0').decode().split('\0'),
                process.joinpath('exe').resolve(), process.stat().st_uid)

    def service(self):
        if platform.system() != 'Linux' or os.geteuid() != 0:
            raise Refused('80 已占用；自动集成需要 Linux root、systemd 和宿主 Python 3')
        properties = ('ActiveState', 'SubState', 'MainPID', 'FragmentPath', 'ExecStart', 'ExecReload',
                      'User', 'Group', 'AmbientCapabilities', 'DropInPaths', 'Environment', 'EnvironmentFiles')
        raw = self.run(['systemctl', 'show', 'caddy.service', '--no-pager',
                        '--property=' + ','.join(properties)])
        info = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if (info.get('ActiveState') != 'active' or info.get('SubState') != 'running' or
                not info.get('MainPID', '').isdigit() or int(info['MainPID']) <= 1 or
                info.get('User') != 'caddy' or info.get('Group') not in ('', 'caddy') or
                info.get('DropInPaths') or info.get('EnvironmentFiles') or info.get('Environment')):
            raise Refused('80 占用者不是受支持的 stock caddy systemd 服务')
        pid = info['MainPID']
        args, executable, uid = self.process(pid)
        if len(args) not in (4, 5, 6, 7) or args[1:2] != ['run']:
            raise Refused('无法确认 Caddy 实际进程参数')
        options = args[2:]
        if '--environ' in options:
            options.remove('--environ')
        if len(options) == 4 and options[2:] == ['--adapter', 'caddyfile']:
            options = options[:2]
        if len(options) != 2 or options[0] != '--config' or not options[1].startswith('/'):
            raise Refused('不支持此 Caddy 启动方式')
        binary, config = self.safe(args[0]), self.safe(options[1], writable=True)
        if executable != binary:
            raise Refused('Caddy 运行程序与服务配置不同')
        user = pwd.getpwnam('caddy')
        if uid != user.pw_uid:
            raise Refused('Caddy 进程用户不匹配')
        for key in ('ExecStart', 'ExecReload'):
            spec = info.get(key, '')
            match = re.search(r'argv\[\]=(.*?) ;', spec)
            if not match or 'path=' + str(binary) + ' ' not in spec:
                raise Refused('不支持此 systemd 启动/重载命令')
            command = shlex.split(match[1])
            if key == 'ExecStart':
                if command != args:
                    raise Refused('systemd 启动参数与实际进程不匹配')
            else:
                expected = [str(binary), 'reload', '--config', str(config)]
                extras = command[len(expected):]
                if command[:len(expected)] != expected or extras not in ([], ['--force'], ['--adapter', 'caddyfile'], ['--adapter', 'caddyfile', '--force']):
                    raise Refused('不支持此 systemd 重载方式')
        if 'cap_net_bind_service' not in info.get('AmbientCapabilities', '').lower():
            raise Refused('Caddy 服务缺少标准绑定能力')
        unit = self.safe(info['FragmentPath'])
        # Listener PID, not merely the unit's name, establishes ownership of port 80.
        listeners = self.run(['ss', '-H', '-ltnp', 'sport = :80'])
        if not listeners.strip() or any(set(re.findall(r'pid=(\d+)', line)) != {pid}
                                       for line in listeners.splitlines()):
            raise Refused('80 存在未知监听者；未更改任何服务')
        modules = self.run([str(binary), 'list-modules', '--packages', '--versions'])
        if not stock_modules(modules):
            raise Refused('宿主 Caddy 含自定义/未知模块或无法确认 stock 模块')
        version = self.run([str(binary), 'version']).split()[0]
        match = re.match(r'v?2\.(\d+)\.', version)
        if not match or int(match[1]) < 6:
            raise Refused('自动 HTTP-01 集成需要 stock Caddy >= 2.6')
        return {'pid': pid, 'binary': str(binary), 'config': str(config), 'version': version,
                'unit': str(unit), 'unit_hash': digest(unit.read_bytes()),
                'binary_hash': digest(binary.read_bytes()), 'service': info}

    def files(self, config):
        found, patterns = {}, {}
        def visit(path):
            path = self.safe(path)
            if str(path) in found:
                return
            if len(found) >= 64:
                raise Refused('Caddy import 文件过多')
            text = path.read_text()
            found[str(path)] = digest(text.encode())
            parsed = tokens(text)
            snippets = {value[1:-1] for value, _, _ in parsed if value.startswith('(') and value.endswith(')')}
            for i, (value, _, end) in enumerate(parsed[:-1]):
                if value != 'import':
                    continue
                pattern, start, _ = parsed[i + 1]
                if '\n' in text[end:start] or pattern in snippets:
                    continue
                if '{' in pattern or '}' in pattern or pattern.startswith('http'):
                    raise Refused('不支持动态 Caddy import')
                target = str(path.parent / pattern) if not pattern.startswith('/') else pattern
                matches = sorted(glob.glob(target))
                # A bare snippet defined in another file is resolved by native adapt.
                if not matches and not any(c in pattern for c in '/*?.'):
                    continue
                patterns[target] = matches
                for filename in matches:
                    visit(Path(filename))
        visit(Path(config))
        return {'files': found, 'patterns': patterns}

    def adapt(self, binary, config, text):
        # Caddy 2.6 does not accept --config -. Keep the temporary file next to
        # the real config so Caddy resolves every relative import identically.
        parent = Path(config).parent
        fd, temporary = tempfile.mkstemp(prefix='.proxy-stack-plan-', suffix='.caddy', dir=parent)
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(text)
            raw = self.run([binary, 'adapt', '--adapter', 'caddyfile', '--config', temporary], cwd=str(parent))
            return json.loads(raw)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def validate(self, binary, config):
        self.run([binary, 'validate', '--config', str(config)])

    def active(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open('http://127.0.0.1:2019/config/', timeout=2) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if response.status != 200 or len(data) > 4 * 1024 * 1024:
                    raise ValueError()
                return json.loads(data)
        except Exception:
            raise Refused('无法验证宿主本机 admin 配置；未覆盖正在运行的配置') from None

    def reload(self):
        self.run(['systemctl', 'reload', 'caddy.service'], timeout=20)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.run(['systemctl', 'is-active', 'caddy.service'], timeout=2).strip() == 'active':
                return
            time.sleep(.2)
        raise Refused('Caddy 重载后健康检查失败')


def hosts(value):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'host' and isinstance(item, list):
                result.extend(item)
            else:
                result.extend(hosts(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(hosts(item))
    return result


def normalized_route(value):
    """Caddy renumbers handle groups after adding a site; preserve group equality."""
    groups = {}
    def visit(item):
        if isinstance(item, dict):
            return {key: groups.setdefault(child, len(groups)) if key == 'group' else visit(child)
                    for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item
    return visit(value)


def preserve_routes(before, after):
    old = before.get('apps', {}).get('http', {}).get('servers', {}).values()
    new = after.get('apps', {}).get('http', {}).get('servers', {}).values()
    for server in old:
        matches = [candidate for candidate in new if candidate.get('listen') == server.get('listen')]
        if len(matches) != 1:
            raise Refused('候选配置改变原有监听器')
        candidate = matches[0]
        for key, value in server.items():
            if key != 'routes' and candidate.get(key) != value:
                raise Refused('候选配置改变原有服务器设置')
        remaining = [normalized_route(route) for route in candidate.get('routes', [])]
        for route in server.get('routes', []):
            normalized = normalized_route(route)
            if normalized not in remaining:
                raise Refused('候选配置改变原有路由语义')
            remaining.remove(normalized)
    for app, value in before.get('apps', {}).items():
        if app != 'http' and after.get('apps', {}).get(app) != value:
            raise Refused('候选配置改变原有非 HTTP 应用')


def plan(host, stage, domain, port, identity):
    if (not isinstance(domain, str) or not re.fullmatch(r'[a-z0-9.:-]+', domain) or
            not isinstance(port, int) or not 1024 <= port <= 65535 or
            not isinstance(identity, str) or not re.fullmatch(r'proxy-stack-[a-zA-Z0-9_-]+', identity)):
        raise Refused('宿主计划输入无效')
    stage = Path(stage)
    stage.mkdir(mode=0o700, exist_ok=True)
    service = host.service()
    config = Path(service['config'])
    original = config.read_bytes()
    dependencies = host.files(config)
    site = challenge_site(domain, port, identity)
    candidate = merge(original.decode(), site, identity).encode()
    baseline = host.adapt(service['binary'], config, original.decode())
    admin = baseline.get('admin', {})
    if admin.get('disabled') or admin.get('listen', 'localhost:2019') not in ('localhost:2019', '127.0.0.1:2019'):
        raise Refused('自动集成需要标准本机 admin 端点以验证/重载配置')
    if host.active() != baseline:
        raise Refused('运行中的 Caddy 配置与磁盘不同；保留原服务')
    # Refuse target hosts and catch-alls that could consume an appended route.
    if candidate != original:
        server_configs = baseline.get('apps', {}).get('http', {}).get('servers', {}).values()
        for server in server_configs:
            if any(domain in hosts(route.get('match', [])) for route in server.get('routes', [])):
                raise Refused('目标地址已有宿主站点；无法安全自动追加 HTTP 路由')
            if any(str(address).rsplit(':', 1)[-1] == '80' for address in server.get('listen', [])):
                for route in server.get('routes', []):
                    names = hosts(route.get('match', []))
                    if not names or any(name == domain or name.startswith('*.') and domain.endswith(name[1:]) for name in names):
                        raise Refused('目标地址的 HTTP 路由已存在或有 catch-all；无法安全自动追加')
    adapted = host.adapt(service['binary'], config, candidate.decode())
    preserve_routes(baseline, adapted)
    atomic(stage / 'candidate.json', json.dumps(adapted).encode())
    host.validate(service['binary'], stage / 'candidate.json')
    metadata = config.stat()
    atomic(stage / 'original', original)
    atomic(stage / 'candidate', candidate)
    value = {'phase': 'planned', 'service': service, 'dependencies': dependencies,
             'original': digest(original), 'candidate': digest(candidate), 'identity': identity,
             'active_hash': digest(json.dumps(baseline, sort_keys=True).encode()),
             'uid': metadata.st_uid, 'gid': metadata.st_gid, 'mode': stat.S_IMODE(metadata.st_mode)}
    save(stage / 'journal.json', value)
    return value


def recheck(host, value):
    if digest(json.dumps(host.active(), sort_keys=True).encode()) != value['active_hash']:
        raise Refused('宿主运行配置在预检后已改变')
    if host.service() != value['service'] or host.files(value['service']['config']) != value['dependencies']:
        raise Refused('宿主配置/监听者在预检后已改变；未应用计划')


def apply(host, stage):
    stage = Path(stage)
    value = json.loads((stage / 'journal.json').read_text())
    recheck(host, value)
    host.validate(value['service']['binary'], stage / 'candidate.json')
    if value['candidate'] == value['original']:
        value['phase'] = 'unchanged'
        save(stage / 'journal.json', value)
        return
    # Persist intent BEFORE replacement, so crash recovery sees either file hash.
    value['phase'] = 'replacing'
    save(stage / 'journal.json', value)
    atomic(value['service']['config'], (stage / 'candidate').read_bytes(),
           value['mode'], value['uid'], value['gid'])
    value['phase'] = 'reloading'
    save(stage / 'journal.json', value)
    host.reload()
    if host.active() != json.loads((stage / 'candidate.json').read_text()):
        raise Refused('宿主重载后配置不匹配')
    value['phase'] = 'active'
    save(stage / 'journal.json', value)


def rollback(host, stage):
    stage = Path(stage)
    value = json.loads((stage / 'journal.json').read_text())
    if value['phase'] in ('planned', 'unchanged', 'rolled-back'):
        return
    info = host.run(['systemctl', 'show', 'caddy.service', '--no-pager',
                     '--property=FragmentPath,ExecStart,ExecReload,User,Group,ActiveState'])
    info = dict(line.split('=', 1) for line in info.splitlines() if '=' in line)
    for key in ('FragmentPath', 'ExecStart', 'ExecReload', 'User', 'Group'):
        if info.get(key) != value['service']['service'].get(key):
            raise Refused('回滚检测到外部 unit 变更；保留备份供恢复')
    current = dict(value['service'])
    current['unit_hash'] = digest(host.safe(current['unit']).read_bytes())
    current['binary_hash'] = digest(host.safe(current['binary']).read_bytes())
    for key in ('binary', 'config', 'unit_hash', 'binary_hash'):
        if current[key] != value['service'][key]:
            raise Refused('回滚检测到外部服务变更；保留备份供恢复')
    config = host.safe(value['service']['config'], writable=True)
    if digest(config.read_bytes()) not in (value['original'], value['candidate']):
        raise Refused('回滚检测到外部配置变更；保留备份供恢复')
    original = (stage / 'original').read_bytes()
    if digest(original) != value['original']:
        raise Refused('回滚备份校验失败')
    atomic(config, original, value['mode'], value['uid'], value['gid'])
    if info.get('ActiveState') == 'active':
        host.reload()
    else:
        host.run(['systemctl', 'start', 'caddy.service'], timeout=20)
        host.reload()
    if digest(json.dumps(host.active(), sort_keys=True).encode()) != value['active_hash']:
        raise Refused('宿主回滚后配置未确认')
    value['phase'] = 'rolled-back'
    save(stage / 'journal.json', value)


def main(argv=None, host=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('plan', 'apply', 'rollback', 'recheck'))
    parser.add_argument('--stage', required=True)
    parser.add_argument('--domain')
    parser.add_argument('--port', type=int)
    parser.add_argument('--identity')
    args = parser.parse_args(argv)
    def interrupted(*_):
        raise Refused('事务被中断；可运行保留的 recover.sh')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        host = host or Host()
        if args.action == 'plan':
            plan(host, args.stage, args.domain, args.port, args.identity)
            print('宿主 Caddy：仅追加目标地址的 HTTP-01 转发；保留程序、443 和现有路由。')
        elif args.action == 'recheck':
            recheck(host, json.loads((Path(args.stage) / 'journal.json').read_text()))
        else:
            globals()[args.action](host, args.stage)
        return 0
    except Exception as error:
        print('proxy-stack: ' + (str(error) if isinstance(error, Refused) else '宿主预检/事务失败：' + type(error).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
