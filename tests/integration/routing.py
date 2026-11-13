#!/usr/bin/env python3
"""Offline Mihomo validation of Seoul parity and cached classical providers."""
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'manager/src'))
sys.path.insert(0, str(ROOT / 'tests'))

from proxy_stack.model import Settings
from proxy_stack.routes import RULE_NAMES
from test_routing_parity import BASE, subscription

MIHOMO = os.environ.get('TEST_MIHOMO_IMAGE', 'metacubex/mihomo:v1.19.15@sha256:da91cd45f1c96fe964982d7bcf3e7252d122022c65694365f3cd36c76e2d2ebd')


def main():
    with tempfile.TemporaryDirectory(prefix='proxy-stack-routing-') as temporary:
        root = Path(temporary).resolve()
        pending = [Settings('pending.example.org', 'Pending domain'), Settings('8.8.4.4', 'Pending IP'),
                   Settings('2606:4700::1001', 'Pending IPv6')]
        variants = []
        for host in ('hub.example.net', '9.9.9.9', '2606:4700::1111'):
            files, config = subscription(Settings(host, 'Sanitized node', port=8443), pending)
            variants.append((host, files, config))
        files, config = subscription()
        # Cached clients still reference all nine classical provider bodies.
        legacy = copy.deepcopy(config)
        prototype = next(iter(config['rule-providers'].values()))
        legacy['rule-providers'] = {name: {**prototype, 'url': prototype['url'].rsplit('/', 1)[0] + '/' + name,
                                          'path': './rules/' + name + '.yaml'} for name in RULE_NAMES}
        legacy['rules'] = ['RULE-SET,' + name + ',' + ('VPS' if name in ('force-proxy', 'proxy') else 'DIRECT')
                           for name in RULE_NAMES] + ['MATCH,VPS']
        variants.append(('cached-classical', files, legacy))
        empty = copy.deepcopy(config)
        empty['proxies'] = []
        empty['proxy-groups'][0]['proxies'] = ['REJECT']
        variants.append(('no-ready-nodes', files, empty))
        for index, (label, files, config) in enumerate(variants):
            directory = root / str(index)
            for path, body in files.items():
                if path.startswith(BASE + 'rules/'):
                    destination = directory / path.removeprefix(BASE)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(body)
            (directory / 'config.yaml').write_text(json.dumps(config))
            result = subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network', 'none',
                                     '-v', str(directory) + ':/root/.config/mihomo', MIHOMO,
                                     '-t', '-d', '/root/.config/mihomo', '-f', '/root/.config/mihomo/config.yaml'],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            if result.returncode:
                raise RuntimeError(label + ' Mihomo validation failed:\n' + result.stdout)
            # A successful -t must not hide malformed classical no-resolve rows.
            if 'error' in result.stdout.lower() or 'invalid' in result.stdout.lower():
                raise RuntimeError(label + ' Mihomo reported a parsing error:\n' + result.stdout)
            print('PASS: Mihomo native routing/DNS/classical validation: ' + label)


if __name__ == '__main__':
    main()
