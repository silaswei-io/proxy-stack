"""Validated deployment inputs and the common client routing model."""
import ipaddress
import os
import re
from dataclasses import dataclass

USER = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
TOKEN = re.compile(r"[0-9a-f]{48}\Z", re.ASCII)
SOURCES = ("private", "apple", "icloud", "proxy", "direct", "cncidr", "lancidr")
POLICY = "VPS"


def username(value):
    if not USER.fullmatch(value):
        raise ValueError("username must match [a-z][a-z0-9_-]{0,31}")
    return value


def public_ip(value):
    if "%" in value:
        raise ValueError("IP zone IDs are unsupported")
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or getattr(address, "ipv4_mapped", None)):
        raise ValueError("IP must be global unicast")
    return address


def default_node_name(hostname):
    """Sanitize only derived names. Never read the tool container's hostname."""
    name = "".join(c if c.isalnum() or c in " ._-()" else "_" for c in hostname[:1024]).strip()[:80].strip()
    if not name or not any(c.isalnum() for c in name):
        name = "Server"
    if name.lower() in ("proxy", "direct", "reject", POLICY.lower()):
        name = "Server-" + name
    return name


@dataclass(frozen=True)
class Settings:
    domain: str
    node_name: str
    port: int = 443
    mode: str = "standalone"
    email: str = ""
    interval: int = 86400
    role: str = "hub"
    local_node: bool = True
    lease_seconds: int = 300
    http01_only: bool = False

    @classmethod
    def from_env(cls):
        domain = os.environ.get("DOMAIN", "").lower()
        name = os.environ.get("NODE_NAME", "") or default_node_name(os.environ.get("HOST_NODE_NAME", "") or domain)
        port = int(os.environ.get("PUBLIC_PORT", "443"))
        mode = os.environ.get("MODE", "standalone")
        email = os.environ.get("CONTACT_EMAIL", "")
        interval = int(os.environ.get("UPDATE_INTERVAL", "86400"))
        if os.environ.get("LOCAL_NODE", "true").lower() not in ("true", "false"):
            raise ValueError("LOCAL_NODE must be true or false")
        return cls(domain, name, port, mode, email, interval,
                   os.environ.get("ROLE", "hub"), os.environ.get("LOCAL_NODE", "true").lower() == "true",
                   int(os.environ.get("LEASE_SECONDS", "300")),
                   os.environ.get("HTTP01_ONLY", "false").lower() == "true")

    def __post_init__(self):
        domain, name, port, mode, email, interval = self.domain, self.node_name, self.port, self.mode, self.email, self.interval
        if not DOMAIN.fullmatch(domain):
            try:
                address = public_ip(domain)
            except ValueError:
                raise ValueError("DOMAIN must be an ASCII DNS hostname or global public IP (no scheme/path/port/brackets/zone)") from None
            object.__setattr__(self, "domain", str(address))
        # QX comma-delimited tag syntax has no universally supported quoting.
        if not name or name != name.strip() or len(name) > 80 or any(not (c.isalnum() or c in " ._-()") for c in name):
            raise ValueError("NODE_NAME contains unsupported configuration characters")
        if name.lower() in ("proxy", "direct", "reject", POLICY.lower()):
            raise ValueError("NODE_NAME must not collide with a policy name")
        if type(port) is not int or type(interval) is not int or not 1 <= port <= 65535 or not 60 <= interval <= 604800:
            raise ValueError("invalid PUBLIC_PORT or UPDATE_INTERVAL (60..604800 seconds)")
        if type(self.http01_only) is not bool:
            raise ValueError("invalid HTTP01_ONLY")
        if self.role not in ("hub", "node") or type(self.local_node) is not bool or type(self.lease_seconds) is not int or not 10 <= self.lease_seconds <= 3600:
            raise ValueError("invalid role, local node flag or lease (10..3600 seconds)")
        if mode not in ("standalone", "existing-caddy"):
            raise ValueError("MODE must be standalone or existing-caddy")
        if email and not re.fullmatch(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+", email):
            raise ValueError("invalid CONTACT_EMAIL")

    @property
    def ip(self):
        try:
            return ipaddress.ip_address(self.domain)
        except ValueError:
            return None

    @property
    def host(self):
        """Authority/Host syntax; raw proxy server addresses remain unbracketed."""
        return "[" + self.domain + "]" if self.ip and self.ip.version == 6 else self.domain

    @property
    def origin(self):
        return "https://" + self.host + (":" + str(self.port) if self.port != 443 else "")


@dataclass(frozen=True)
class Rule:
    kind: str
    value: str
    policy: str

    def clash(self):
        kind = {"exact": "DOMAIN", "suffix": "DOMAIN-SUFFIX", "wildcard": "DOMAIN-REGEX", "ip4": "IP-CIDR", "ip6": "IP-CIDR6"}[self.kind]
        value = self.value
        if self.kind == "wildcard":
            value = "^" + re.escape(value).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return kind + "," + value

    def qx(self):
        kind = {"exact": "host", "suffix": "host-suffix", "wildcard": "host-wildcard", "ip4": "ip-cidr", "ip6": "ip6-cidr"}[self.kind]
        return f"{kind}, {self.value}, {self.policy}"

    def matches(self, target):
        if self.kind.startswith("ip"):
            try:
                return ipaddress.ip_address(target) in ipaddress.ip_network(self.value)
            except ValueError:
                return False
        target = target.lower().rstrip(".")
        if self.kind == "exact":
            return target == self.value
        if self.kind == "suffix":
            return target == self.value or target.endswith("." + self.value)
        pattern = re.escape(self.value).replace(r"\*", ".*").replace(r"\?", ".")
        return re.fullmatch(pattern, target) is not None


def parse_entry(value, policy):
    if not isinstance(value, str) or not value or len(value) > 300:
        raise ValueError("invalid rule entry")
    value = value.lower()
    if "/" in value:
        network = ipaddress.ip_network(value, strict=False)
        return Rule("ip4" if network.version == 4 else "ip6", str(network), policy)
    kind = "exact"
    if value.startswith("+."):
        kind, value = "suffix", value[2:]
    elif value.startswith("."):
        kind, value = "suffix", value[1:]
    elif "*" in value or "?" in value:
        kind = "wildcard"
    if not re.fullmatch(r"[a-z0-9_*?](?:[a-z0-9_.?*-]*[a-z0-9_*?])?", value, re.ASCII) or ".." in value:
        raise ValueError("unsupported domain rule")
    return Rule(kind, value, policy)


def groups(settings, lists, endpoints=()):
    """Order here is the sole routing authority for both clients."""
    result = [("force-proxy", [Rule("exact", x, POLICY) for x in ("api.ip.sb", "ipapi.co", "api.ipapi.is", "ipwho.is")])]
    address = settings.ip
    bootstrap = (Rule("ip4" if address.version == 4 else "ip6", str(address) + ("/32" if address.version == 4 else "/128"), "DIRECT")
                 if address else Rule("exact", settings.domain, "DIRECT"))
    bootstraps = [bootstrap]
    for endpoint in endpoints:
        address = endpoint.ip
        rule = (Rule("ip4" if address.version == 4 else "ip6", str(address) + ("/32" if address.version == 4 else "/128"), "DIRECT")
                if address else Rule("exact", endpoint.domain, "DIRECT"))
        if rule not in bootstraps:
            bootstraps.append(rule)
    result.append(("bootstrap", bootstraps + [ Rule("exact", "localhost", "DIRECT"), Rule("suffix", "local", "DIRECT"), Rule("suffix", "lan", "DIRECT")]))
    for name in ("private", "lancidr", "apple", "icloud", "proxy", "direct", "cncidr"):
        policy = POLICY if name == "proxy" else "DIRECT"
        entries = [parse_entry(x, policy) for x in lists[name]]
        if name == "direct":
            entries.insert(0, Rule("suffix", "cn", "DIRECT"))
        result.append((name, entries))
    return result
