"""Pure renderers: never read state or perform network requests."""
import json

from .model import POLICY, groups


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def xray(state):
    return encoded({
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "0.0.0.0", "port": 10000, "protocol": "vless",
                      "settings": {"clients": [{"id": user["uuid"]} for user in state["users"].values()], "decryption": "none"},
                      "streamSettings": {"network": "ws", "wsSettings": {"path": state["ws_path"]}}}],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    })


ASSET_PATTERN = r"^/sub/[0-9a-f]{48}/(clash\.yaml|qx/(nodes\.txt|full\.conf|rules\.list)|rules/(force-proxy|bootstrap|private|lancidr|apple|icloud|proxy|direct|cncidr)\.yaml)$"


def caddy(settings, state, external=False):
    site = ":8080" if external else settings.host + ":" + str(settings.port)
    global_options = "{\n    admin off\n    log default {\n        exclude http.log.access http.log.error\n    }\n"
    if settings.email:
        global_options += "    email " + settings.email + "\n"
    if settings.ip and not external:
        global_options += "    default_sni " + settings.domain + "\n"
    global_options += "}\n"
    return global_options + f"""
{site} {{
{ip_tls(settings) if not external else ""}    @unsafe expression `{{http.request.orig_uri}}.contains("%") || {{http.request.orig_uri}}.contains("..") || {{http.request.orig_uri}}.contains("//") || {{http.request.orig_uri}}.contains("/./") || {{http.request.orig_uri}}.contains("?")`
    route {{
    handle @unsafe {{
        respond "Not found" 404
    }}
    @tunnel path {state['ws_path']}
    handle @tunnel {{
        reverse_proxy xray:10000
    }}
    @asset {{
        method GET HEAD
        path_regexp asset {ASSET_PATTERN}
    }}
    handle @asset {{
        root * /srv/public/current
        header Cache-Control "no-store"
        header X-Content-Type-Options "nosniff"
        header Referrer-Policy "no-referrer"
        @yaml path *.yaml
        header @yaml Content-Type "application/yaml; charset=utf-8"
        @text path *.txt *.conf *.list
        header @text Content-Type "text/plain; charset=utf-8"
        file_server
    }}
    handle {{
        respond "Not found" 404
    }}
    }}
}}
"""


def ip_tls(settings):
    if not settings.ip:
        return ""
    return """    tls {
        issuer acme {
            dir https://acme-v02.api.letsencrypt.org/directory
            profile shortlived
        }
    }
"""


def snippet(settings):
    options = ("# Merge default_sni into the ONE existing global options block (Caddy >= 2.11).\n"
               "{\n    default_sni " + settings.domain + "\n}\n" if settings.ip else "")
    return f"""# Review and merge into your host Caddy configuration; do not duplicate an existing site.
{options}{settings.host}:{settings.port} {{
{ip_tls(settings)}    reverse_proxy 127.0.0.1:18090
}}
"""


def public_files(settings, state, cache):
    routing = groups(settings, cache["lists"])
    files = {}
    providers = {name: encoded({"payload": [r.clash() + (",no-resolve" if name == "bootstrap" and r.kind in ("ip4", "ip6") else "") for r in rules]}) for name, rules in routing}
    qx_rules = "\n".join(r.qx().replace(", DIRECT", ", direct") for _, rules in routing for r in rules) + "\n"
    for user in state["users"].values():
        base = "sub/" + user["token"] + "/"
        url = settings.origin + "/" + base
        node = {"name": settings.node_name, "type": "vless", "server": settings.domain,
                "port": settings.port, "uuid": user["uuid"], "network": "ws", "tls": True,
                "servername": settings.domain, "skip-cert-verify": False, "udp": False,
                "ws-opts": {"path": state["ws_path"], "headers": {"Host": settings.host}}}
        if settings.ip:
            del node["servername"]
        clash = {"mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "warning", "ipv6": False,
                 "dns": {"enable": True, "ipv6": False, "enhanced-mode": "fake-ip", "respect-rules": True,
                         "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                         "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"],
                         "nameserver": ["223.5.5.5", "119.29.29.29"],
                         "fake-ip-filter": [settings.domain, "*.lan", "*.local"]},
                 "proxies": [node], "proxy-groups": [{"name": POLICY, "type": "select", "proxies": [settings.node_name]}],
                 "rule-providers": {name: {"type": "http", "behavior": "classical", "format": "yaml", "interval": 86400,
                                             "url": url + "rules/" + name + ".yaml", "path": "./rules/" + name + ".yaml"} for name, _ in routing},
                 "rules": ["RULE-SET," + name + "," + rules[0].policy for name, rules in routing] + ["MATCH," + POLICY]}
        files[base + "clash.yaml"] = encoded(clash)
        for name, content in providers.items():
            files[base + "rules/" + name + ".yaml"] = content
        files[base + "qx/nodes.txt"] = (
            f"vless={settings.host}:{settings.port}, method=none, password={user['uuid']}, "
            f"obfs=wss, " + (f"obfs-host={settings.domain}, " if not settings.ip else "") + f"obfs-uri={state['ws_path']}, "
            f"tls-verification=true, fast-open=false, udp-relay=false, tag={settings.node_name}\n")
        files[base + "qx/rules.list"] = qx_rules
        files[base + "qx/full.conf"] = f"""# proxy-stack 0.1.2; QX native iOS import remains UNVERIFIED.
# Rules: {cache['quality']}; URLs below are private credentials.
[general]
server_check_url = http://www.apple.com/generate_204

[dns]
no-ipv6
server = 223.5.5.5
server = 119.29.29.29

[policy]
static = {POLICY}, {settings.node_name}

[server_remote]
{url}qx/nodes.txt, tag=proxy-stack-node, update-interval=86400, enabled=true

[filter_remote]
{url}qx/rules.list, tag=proxy-stack-rules, update-interval=86400, enabled=true

[server_local]

[filter_local]
final, {POLICY}

[rewrite_remote]

[rewrite_local]

[task_local]

[mitm]
"""
    return files
