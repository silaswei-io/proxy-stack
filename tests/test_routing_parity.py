"""Compare rendered subscriptions with an independent, sanitized Seoul fixture."""
import copy
import fnmatch
import ipaddress
import json
import re
import unittest
from dataclasses import asdict
from pathlib import Path

from proxy_stack import render
from proxy_stack.model import Settings
from proxy_stack.routes import RULE_NAMES, asset_paths
from proxy_stack.topology import initialize

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/seoul-routing.json').read_text())
TOKEN = 'a' * 48
BASE = 'sub/' + TOKEN + '/'
UUID = '00000000-0000-4000-8000-000000000001'


def subscription(settings=None, endpoints=()):
    settings = settings or Settings(FIXTURE['host'], 'Sanitized node')
    state = {'settings': asdict(settings), 'ws_path': '/ws/' + 'b' * 48,
             'users': {'owner': {'token': TOKEN, 'uuid': UUID}}}
    initialize(state)
    state['nodes']['local'].update(ready=True, acked=1)
    for index, endpoint in enumerate(endpoints):
        state['nodes'][str(index)] = {'settings': asdict(endpoint), 'ws_path': '/ws/' + 'c' * 48,
                                     'enabled': True, 'ready': False, 'desired': 1, 'acked': 0}
    cache = {'quality': 'upstream', 'lists': copy.deepcopy(FIXTURE['sources'])}
    files = render.public_files(settings, state, cache)
    return files, json.loads(files[BASE + 'clash.yaml'])


def expand_mihomo(rows, providers):
    """Read actual classical payloads, carrying RULE-SET options into IP rules."""
    expanded = []
    for row in rows:
        kind, value, *rest = row.split(',')
        if kind != 'RULE-SET':
            expanded.append(row)
            continue
        for entry in providers[value]:
            rule_kind, match, *options = entry.split(',')
            expanded.append(','.join([rule_kind, match, rest[0], *options, *rest[1:]]))
    return expanded


def fixture_payloads():
    """Independent conversion of sanitized upstream samples for the oracle."""
    result = {}
    for name, values in FIXTURE['sources'].items():
        result[name] = []
        for value in values:
            if '/' in value:
                kind = 'IP-CIDR6' if ':' in value else 'IP-CIDR'
            elif value.startswith('+.'):
                kind, value = 'DOMAIN-SUFFIX', value[2:]
            elif '*' in value:
                kind, value = 'DOMAIN-REGEX', '^' + re.escape(value).replace(r'\*', '.*') + '$'
            else:
                kind = 'DOMAIN'
            result[name].append(kind + ',' + value)
    return result


def route(rows, target, resolved=None):
    """Small independent interpreter, including deferred DNS for Mihomo IP rules."""
    for row in rows:
        kind, value, *rest = [s.strip() for s in row.split(',')]
        if kind in ('MATCH', 'final'):
            return value
        policy, *options = rest
        if kind in ('IP-CIDR', 'IP-CIDR6', 'ip-cidr', 'ip6-cidr'):
            try:
                address = ipaddress.ip_address(target)
            except ValueError:
                if not resolved or 'no-resolve' in options:
                    continue
                address = ipaddress.ip_address(resolved)
            if address in ipaddress.ip_network(value):
                return policy
        elif kind in ('DOMAIN', 'host') and target == value:
            return policy
        elif kind in ('DOMAIN-SUFFIX', 'host-suffix') and (target == value or target.endswith('.' + value)):
            return policy
        elif kind == 'DOMAIN-REGEX' and re.fullmatch(value, target):
            return policy
        elif kind == 'host-wildcard' and fnmatch.fnmatchcase(target, value):
            return policy
    raise AssertionError('missing final rule')


class SeoulParityTests(unittest.TestCase):
    def test_full_config_matches_sanitized_baseline_order_and_dns(self):
        _, config = subscription()
        self.assertEqual([r.replace(',VPS', ',Proxy') for r in config['rules']], FIXTURE['rules'])
        self.assertEqual(config['dns'], FIXTURE['dns'])
        self.assertEqual(list(config['rule-providers']), ['private', 'lancidr', 'icloud', 'apple', 'proxy', 'direct', 'cncidr'])
        self.assertTrue(all(p['behavior'] == 'classical' for p in config['rule-providers'].values()))

    def test_all_rule_subscriptions_match_baseline_on_overlaps_and_ip_fallback(self):
        files, config = subscription()
        payloads = {name: json.loads(files[BASE + 'rules/' + name + '.yaml'])['payload'] for name in RULE_NAMES}
        oracle = expand_mihomo(FIXTURE['rules'], fixture_payloads())
        mihomo = expand_mihomo(config['rules'], payloads)
        managed = files[BASE + 'qx/rules.list'].splitlines() + ['final, VPS']
        portable = files[BASE + 'qx/rules-portable.list'].splitlines() + ['final, proxy']
        # Overlaps ensure force-proxy, own endpoints, private, iCloud/Apple,
        # proxy-before-cn/direct and the cncidr/final distinction are exercised.
        direct = ['hub.example.net', 'localhost', 'a.local', 'a.lan', 'a.private.example',
                  'a.overlap.example', 'a.private-wild.example', 'apple.com', 'icloud.com',
                  'apple-overlap.example', 'baidu.com', 'ordinary.cn', '169.254.1.1',
                  '198.19.1.1', '203.0.113.7', '127.0.0.1', '10.1.1.1', '172.20.0.1',
                  '192.168.1.1', '100.100.0.1', '::1', 'fd00::1', 'fe80::1']
        proxy = ['api.ip.sb', 'ipapi.co', 'api.ipapi.is', 'ipwho.is', 'google.com',
                 'shared.example', 'forced.cn', 'a.proxy-wild.example', 'unknown.example', '198.51.100.7']
        for target in direct + proxy:
            expected = 'DIRECT' if target in direct else 'Proxy'
            with self.subTest(target=target):
                self.assertEqual(route(oracle, target), expected)
                self.assertEqual(route(mihomo, target), 'DIRECT' if expected == 'DIRECT' else 'VPS')
                self.assertEqual(route(managed, target), 'direct' if expected == 'DIRECT' else 'VPS')
                self.assertEqual(route(portable, target), 'direct' if expected == 'DIRECT' else 'proxy')

    def test_mihomo_skips_lan_dns_resolution_but_resolves_cncidr(self):
        files, config = subscription()
        payloads = {name: json.loads(files[BASE + 'rules/' + name + '.yaml'])['payload'] for name in RULE_NAMES}
        actual = expand_mihomo(config['rules'], payloads)
        oracle = expand_mihomo(FIXTURE['rules'], fixture_payloads())
        for address, expected in [('10.1.1.1', 'Proxy'), ('fd00::1', 'Proxy'),
                                  ('169.254.1.1', 'Proxy'), ('198.19.1.1', 'Proxy'),
                                  ('203.0.113.7', 'DIRECT'), ('198.51.100.7', 'Proxy')]:
            with self.subTest(address=address):
                self.assertEqual(route(oracle, 'unlisted.example', address), expected)
                self.assertEqual(route(actual, 'unlisted.example', address), expected.replace('Proxy', 'VPS'))
        # All eight inline private networks survive even if the LAN source lacks them.
        self.assertEqual(sum(r.endswith(',DIRECT,no-resolve') for r in config['rules'] if r.startswith('IP-')), 8)

    def test_legacy_assets_stay_classical_and_retain_old_config_helpers(self):
        files, config = subscription()
        self.assertEqual(len(files), len(asset_paths(TOKEN)))
        for name in RULE_NAMES:
            payload = json.loads(files[BASE + 'rules/' + name + '.yaml'])['payload']
            self.assertTrue(payload)
            self.assertTrue(all(row.startswith(('DOMAIN,', 'DOMAIN-SUFFIX,', 'DOMAIN-REGEX,', 'IP-CIDR,', 'IP-CIDR6,')) for row in payload))
        lan = json.loads(files[BASE + 'rules/lancidr.yaml'])['payload']
        self.assertTrue(all(r.endswith(',no-resolve') for r in lan))
        self.assertIn('DOMAIN-SUFFIX,cn', json.loads(files[BASE + 'rules/direct.yaml'])['payload'])
        self.assertEqual(len(json.loads(files[BASE + 'rules/force-proxy.yaml'])['payload']), 4)
        bootstrap = json.loads(files[BASE + 'rules/bootstrap.yaml'])['payload']
        self.assertIn('IP-CIDR,10.0.0.0/8,no-resolve', bootstrap)
        self.assertNotIn('bootstrap', config['rule-providers'])
        self.assertNotIn('force-proxy', config['rule-providers'])

    def test_qx_full_config_has_inline_safety_if_remote_rules_unavailable(self):
        files, _ = subscription()
        config = files[BASE + 'qx/full.conf']
        local = config.split('[filter_local]\n')[1].split('\n[rewrite_remote]')[0].strip().splitlines()
        managed = files[BASE + 'qx/rules.list'].splitlines()
        self.assertEqual(local[:-1], managed[:16])
        self.assertEqual(local[-1], 'final, VPS')
        self.assertEqual(route(local, 'api.ip.sb'), 'VPS')
        self.assertEqual(route(local, 'hub.example.net'), 'direct')
        self.assertEqual(route(local, '10.1.1.1'), 'direct')
        self.assertEqual(route(local, 'unknown.example'), 'VPS')
        self.assertNotIn('no-resolve', config + '\n'.join(managed))

    def test_deployment_identity_and_pending_node_dns_bootstrap_are_dynamic(self):
        nodes = [Settings('node.example.org', 'Pending domain'), Settings('8.8.4.4', 'Pending IPv4'),
                 Settings('2606:4700:4700::1001', 'Pending IPv6')]
        for host in ['other-hub.example.org', '9.9.9.9', '2606:4700::1111']:
            with self.subTest(host=host):
                settings = Settings(host, 'Own node', port=8443)
                files, config = subscription(settings, nodes)
                self.assertEqual(config['proxies'][0]['server'], host)
                self.assertEqual(config['proxies'][0]['uuid'], UUID)
                self.assertEqual(config['proxy-groups'][0]['name'], 'VPS')
                self.assertEqual(config['proxies'][0]['ws-opts']['path'], '/ws/' + 'b' * 48)
                self.assertTrue(all(p['url'].startswith(settings.origin + '/' + TOKEN + '/rules/') for p in config['rule-providers'].values()))
                dns = config['dns']
                for endpoint in [settings, *nodes]:
                    self.assertIn(endpoint.domain, dns['fake-ip-filter'])
                    if endpoint.ip:
                        cidr = endpoint.domain + ('/32' if endpoint.ip.version == 4 else '/128')
                        self.assertTrue(any(',' + cidr + ',DIRECT,no-resolve' in r for r in config['rules']))
                    else:
                        self.assertIn('DOMAIN,' + endpoint.domain + ',DIRECT', config['rules'])
                        self.assertEqual(dns['nameserver-policy'][endpoint.domain], dns['nameserver'])
                        self.assertLess(list(dns['nameserver-policy']).index(endpoint.domain),
                                        list(dns['nameserver-policy']).index('rule-set:proxy'))
                self.assertNotIn(FIXTURE['host'], files[BASE + 'clash.yaml'])
