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
