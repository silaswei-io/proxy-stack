import io
import json
import os
import signal
import unittest
from unittest.mock import patch

from proxy_stack import render, rules
from proxy_stack.discovery import ENDPOINTS, detect_public_ip
from proxy_stack.model import Settings, default_node_name, groups


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
