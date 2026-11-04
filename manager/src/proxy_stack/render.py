"""Pure renderers: never read state or perform network requests."""
import json

from .model import POLICY, Settings, groups


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
    result = global_options + f"""
{site} {{
{ip_tls(settings) if not external else ""}    @unsafe expression `{{http.request.orig_uri}}.contains("%") || {{http.request.orig_uri}}.contains("..") || {{http.request.orig_uri}}.contains("//") || {{http.request.orig_uri}}.contains("/./") || {{http.request.orig_uri}}.contains("?")`
    route {{
    handle @unsafe {{
        respond "Not found" 404
    }}
    @control {{
        method POST
        path /control/v1/enroll /control/v1/poll /control/v1/ack /control/v1/detach
    }}
    handle @control {{
        request_body {{
            max_size 64KB
        }}
        reverse_proxy hub:8090 {{
            transport http {{
                dial_timeout 3s
                response_header_timeout 10s
            }}
        }}
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

    if settings.role == "node":
        start = result.index("    @control {")
        end = result.index("    @tunnel path", start)
        result = result[:start] + result[end:]
    if settings.role == "hub" and (not settings.local_node or not state.get("nodes", {}).get("local", {}).get("enabled", True)):
        start = result.index("    @tunnel path")
        end = result.index("    @asset {", start)
        result = result[:start] + result[end:]
    return result


def ip_tls(settings):
    if not settings.ip and not settings.http01_only:
        return ""
    profile = "            profile shortlived\n" if settings.ip else ""
    challenge = "            disable_tlsalpn_challenge\n" if settings.http01_only else ""
    return """    tls {
        issuer acme {
            dir https://acme-v02.api.letsencrypt.org/directory
""" + profile + challenge + """        }
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


def ready_nodes(settings, state):
    return [(Settings(**node["settings"]), node["ws_path"]) for node in state["nodes"].values()
            if node.get("enabled") and node.get("ready") and node.get("acked") == node.get("desired")]


def public_files(settings, state, cache):
    endpoints = [Settings(**n["settings"]) for n in state.get("nodes", {}).values() if n.get("enabled")]
    available = ready_nodes(settings, state)
    routing = groups(settings, cache["lists"], endpoints)
    files = {}
    providers = {name: encoded({"payload": [r.clash() + (",no-resolve" if name == "bootstrap" and r.kind in ("ip4", "ip6") else "") for r in rules]}) for name, rules in routing}
    qx_rules = "\n".join(r.qx().replace(", DIRECT", ", direct") for _, rules in routing for r in rules) + "\n"
    for user in state["users"].values():
        base = "sub/" + user["token"] + "/"
        url = settings.origin + "/" + base
        nodes, qx_nodes = [], []
        for endpoint, path in available:
            node = {"name": endpoint.node_name, "type": "vless", "server": endpoint.domain,
                    "port": endpoint.port, "uuid": user["uuid"], "network": "ws", "tls": True,
                    "servername": endpoint.domain, "skip-cert-verify": False, "udp": False,
                    "ws-opts": {"path": path, "headers": {"Host": endpoint.host}}}
            if endpoint.ip:
                del node["servername"]
            nodes.append(node)
            qx_nodes.append(f"vless={endpoint.host}:{endpoint.port}, method=none, password={user['uuid']}, "
                            "obfs=wss, " + (f"obfs-host={endpoint.domain}, " if not endpoint.ip else "") +
                            f"obfs-uri={path}, tls-verification=true, fast-open=false, udp-relay=false, tag={endpoint.node_name}\n")
        names = [node["name"] for node in nodes]
        clash = {"mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "warning", "ipv6": False,
                 "dns": {"enable": True, "ipv6": False, "enhanced-mode": "fake-ip", "respect-rules": True,
                         "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                         "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"],
                         "nameserver": ["223.5.5.5", "119.29.29.29"],
                         "fake-ip-filter": list(dict.fromkeys([settings.domain] + [e.domain for e in endpoints] + ["*.lan", "*.local"]))},
                 "proxies": nodes, "proxy-groups": [{"name": POLICY, "type": "select", "proxies": names or ["REJECT"]}],
                 "rule-providers": {name: {"type": "http", "behavior": "classical", "format": "yaml", "interval": 86400,
                                             "url": url + "rules/" + name + ".yaml", "path": "./rules/" + name + ".yaml"} for name, _ in routing},
                 "rules": ["RULE-SET," + name + "," + rules[0].policy for name, rules in routing] + ["MATCH," + POLICY]}
        files[base + "clash.yaml"] = encoded(clash)
        for name, content in providers.items():
            files[base + "rules/" + name + ".yaml"] = content
        files[base + "qx/nodes.txt"] = "".join(qx_nodes)
        files[base + "qx/rules.list"] = qx_rules
        files[base + "qx/full.conf"] = f"""# proxy-stack v1.0.0; QX native iOS import remains UNVERIFIED.
# Rules: {cache['quality']}; URLs below are private credentials.
# No ready nodes means normally proxied traffic is REJECTED.
# Ready nodes: {len(nodes)}. If zero, wait for node startup/ack and check proxyctl node list.
[general]
server_check_url = http://www.apple.com/generate_204

[dns]
no-ipv6
server = 223.5.5.5
server = 119.29.29.29

[policy]
static = {POLICY}, {", ".join(names) if names else "reject"}

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
