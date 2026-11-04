"""Export only generic, packaged templates; no host state or deployment secrets."""
from importlib.resources import files


def script():
    templates = files("proxy_stack").joinpath("templates")
    body = templates.joinpath("installer.sh").read_text(encoding="utf-8")
    payload = []
    for name in ("compose.yaml", "compose.external.yaml", ".env.example", "proxyctl"):
        delimiter = "PROXY_STACK_TEMPLATE_" + name.replace(".", "_").upper()
        content = templates.joinpath(name).read_text(encoding="utf-8")
        payload.append("cat > " + name + " <<'" + delimiter + "'\n" + content + delimiter + "\n")
    helper = files("proxy_stack").joinpath("host_setup.py").read_text(encoding="utf-8")
    body = body.replace("# EXPORT_HOST_HELPER\n", "cat > host_setup.py <<'PROXY_STACK_HOST_HELPER'\n" + helper + "PROXY_STACK_HOST_HELPER\n")
    return body.replace("# EXPORT_TEMPLATES\n", "".join(payload))
