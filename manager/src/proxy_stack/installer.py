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
    return body.replace("# EXPORT_TEMPLATES\n", "".join(payload))
