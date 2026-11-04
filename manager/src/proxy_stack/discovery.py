"""Bounded public egress discovery, only requested for omitted installer addresses."""
import signal
import urllib.request

from .model import public_ip

ENDPOINTS = ("https://api.ipify.org", "https://api64.ipify.org")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DiscoveryDeadline(Exception):
    pass


def detect_public_ip():
    # Disable inherited proxies and redirects: only these fixed HTTPS services
    # are contacted. A total deadline also bounds DNS and trickled responses.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def expired(*_):
        raise DiscoveryDeadline()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 12)
    try:
        for endpoint in ENDPOINTS:
            try:
                with opener.open(endpoint, timeout=4) as response:
                    value = response.read(65)
                if len(value) > 64:
                    continue
                return str(public_ip(value.decode("ascii").strip()))
            except (OSError, ValueError, UnicodeError):
                continue
    except DiscoveryDeadline:
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    raise ValueError("无法检测公网 IP；请用 --domain 显式指定域名或公网 IP（无需探测）。")


def check_address(domain):
    """Prove published DNS addresses using separate IPv4/IPv6 egress probes.

    A host can legitimately publish both families. Additional addresses behind
    NAT/multihoming may be valid, but require evidence this preflight lacks.
    Inbound firewall/NAT/ACME remains part of the later HTTPS completion gate.
    """
    import socket
    try:
        public_ip(domain)
        return
    except ValueError:
        pass
    def expired(*_):
        raise DiscoveryDeadline()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 14)
    try:
        addresses = {str(public_ip(item[4][0])) for item in
                     socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)}
        if not addresses:
            raise ValueError('域名没有公共 A/AAAA 地址；预检未通过')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        proven = set()
        for endpoint in ('https://api.ipify.org', 'https://api6.ipify.org'):
            try:
                with opener.open(endpoint, timeout=4) as response:
                    value = response.read(65)
                proven.add(str(public_ip(value.decode('ascii').strip())))
            except (OSError, ValueError, UnicodeError):
                continue
        if not addresses <= proven:
            raise ValueError('无法证明全部 A/AAAA 属于本机公网出口（可能为多地址/NAT或探测不可达）；未更改安装')
    except (OSError, DiscoveryDeadline):
        raise ValueError('域名/本机双栈地址证明失败或超时；未更改安装') from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
