import io
import json
import os
import signal
import unittest
from unittest.mock import patch

from proxy_stack import render, rules
from proxy_stack.discovery import ENDPOINTS, detect_public_ip, check_address
from proxy_stack.model import Settings, default_node_name, groups
from proxy_stack.topology import initialize
from dataclasses import asdict


class DefaultsTests(unittest.TestCase):
    def test_fixed_bounded_probe_fallback_and_strict_public_validation(self):
        for bad in (b'10.0.0.1', b'127.0.0.1', b'169.254.1.1', b'100.64.0.1', b'224.0.0.1',
                    b'2001:db8::1', b'fe80::1%eth0', b'::1', b'::ffff:8.8.8.8', b'8.8.8.008',
                    b'2130706433', b'0x08080808', b'https://8.8.8.8', b'8.8.8.8:443', b'x'*65, b'\xff'):
            with self.subTest(bad=bad), patch('proxy_stack.discovery.urllib.request.build_opener') as build:
                build.return_value.open.side_effect = [io.BytesIO(bad), io.BytesIO(b'2606:4700:4700::1111\n')]
                self.assertEqual(detect_public_ip(), '2606:4700:4700::1111')
                self.assertEqual(build.return_value.open.call_args_list[0].args, (ENDPOINTS[0],))
                self.assertEqual(build.return_value.open.call_args_list[1].args, (ENDPOINTS[1],))
                self.assertEqual(build.return_value.open.call_args.kwargs, {'timeout': 4})
        with patch('proxy_stack.discovery.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = OSError('offline')
            with self.assertRaisesRegex(ValueError, '--domain'):
                detect_public_ip()
        # The overall deadline also interrupts DNS/slow trickled responses.
        with patch('proxy_stack.discovery.urllib.request.build_opener') as build:
            def timeout(*_, **__):
                signal.raise_signal(signal.SIGALRM)
            build.return_value.open.side_effect = timeout
            with self.assertRaisesRegex(ValueError, '--domain'):
                detect_public_ip()
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_domain_preflight_accepts_proven_dual_stack_and_explains_unproven_addresses(self):
        import socket
        records = [(socket.AF_INET, 1, 6, '', ('8.8.8.8', 0)),
                   (socket.AF_INET6, 1, 6, '', ('2606:4700:4700::1111', 0, 0, 0))]
        with patch('socket.getaddrinfo', return_value=records), patch('proxy_stack.discovery.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [io.BytesIO(b'8.8.8.8'), io.BytesIO(b'2606:4700:4700::1111')]
            check_address('proxy.example.com')
            self.assertEqual(build.return_value.open.call_count, 2)
            build.return_value.open.side_effect = [io.BytesIO(b'8.8.8.8'), OSError('no IPv6 proof')]
            with self.assertRaisesRegex(ValueError, '无法证明全部 A/AAAA'):
                check_address('proxy.example.com')
        with patch('socket.getaddrinfo', return_value=records[:1]), patch('proxy_stack.discovery.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [io.BytesIO(b'8.8.8.8'), OSError('v4 only')]
            check_address('proxy.example.com')
        with patch('socket.getaddrinfo', side_effect=OSError('dns')), self.assertRaisesRegex(ValueError, '超时'):
            check_address('proxy.example.com')

    def test_explicit_hosts_reject_ambiguous_private_and_injection(self):
        for value in ('8.8.8.08', '0177.0.0.1', '127.1', '2130706433', '0x08080808',
                      '10.0.0.1', '100.64.0.1', '192.0.2.1', '224.0.0.1', '255.255.255.255',
                      '::', '::1', 'fc00::1', '2001:db8::1', 'ff02::1', '::ffff:8.8.8.8',
                      '2606:4700::1%eth0', '[2606:4700::1]', '[2606:4700::1]:443',
                      '8.8.8.8:443', 'https://8.8.8.8', '8.8.8.8/path', '8.8.8.8\nimport x',
                      '$(touch /tmp/no)', 'x.example.com; echo bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(value, 'Host')

    def test_host_name_fallback_never_uses_container_hostname(self):
        for hostname in ('my-host_01', 'My host', '', 'VPS', 'direct', '\n\x00,[]$`', 'x'*200):
            name = default_node_name(hostname)
            Settings('8.8.8.8', name)
            self.assertLessEqual(len(name), 80)
        with patch.dict(os.environ, {'DOMAIN': 'proxy.example.com', 'HOST_NODE_NAME': 'my-host_01'}, clear=True):
            self.assertEqual(Settings.from_env().node_name, 'my-host_01')
        with patch.dict(os.environ, {'DOMAIN': 'proxy.example.com'}, clear=True):
            self.assertEqual(Settings.from_env().node_name, 'proxy.example.com')
        with patch.dict(os.environ, {'DOMAIN': '8.8.8.8', 'HOST_NODE_NAME': 'good', 'NODE_NAME': 'bad,name'}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_ip_clients_bootstrap_and_public_acme(self):
        state = {'ws_path': '/ws/' + 'b'*48, 'users': {'owner': {'token': 'a'*48, 'uuid': '00000000-0000-4000-8000-000000000001'}}}
        base = 'sub/' + 'a'*48 + '/'
        for address, host, kind, prefix in (('8.8.8.8', '8.8.8.8', 'IP-CIDR', '/32'),
                                             ('2606:4700:4700::1111', '[2606:4700:4700::1111]', 'IP-CIDR6', '/128')):
            with self.subTest(address=address):
                settings = Settings(address, 'Host', port=8443)
                self.assertEqual(settings.origin, 'https://' + host + ':8443')
                state["settings"] = asdict(settings)
                initialize(state)
                state["nodes"]["local"].update(ready=True, acked=1)
                files = render.public_files(settings, state, rules.bootstrap())
                node = json.loads(files[base + 'clash.yaml'])['proxies'][0]
                self.assertEqual(node['server'], address)
                self.assertEqual(node['ws-opts']['headers']['Host'], host)
                self.assertNotIn('servername', node)
                self.assertFalse(node['skip-cert-verify'])
                self.assertIn(kind + ',' + address + prefix + ',no-resolve', files[base + 'rules/bootstrap.yaml'])
                bootstrap = dict(groups(settings, rules.bootstrap()['lists']))['bootstrap'][0]
                self.assertTrue(bootstrap.matches(address))
                qx = files[base + 'qx/nodes.txt']
                self.assertIn('vless=' + host + ':8443', qx)
                self.assertIn('tls-verification=true', qx)
                self.assertNotIn('obfs-host=', qx)
                self.assertIn(address + prefix + ', direct', files[base + 'qx/rules.list'])
                for config in (render.caddy(settings, state), render.snippet(settings)):
                    self.assertIn(host + ':8443 {', config)
                    self.assertIn('default_sni ' + address, config)
                    self.assertIn('profile shortlived', config)
                    self.assertIn('https://acme-v02.api.letsencrypt.org/directory', config)
                    self.assertNotIn('tls internal', config)
                gateway = render.caddy(settings, state, external=True)
                self.assertIn(':8080 {', gateway)
                self.assertNotIn('issuer', gateway)
                self.assertNotIn('default_sni', gateway)
