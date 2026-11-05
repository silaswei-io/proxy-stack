"""Exact public aliases for the unchanged private on-disk asset layout."""
import re


TOKEN_PATTERN = r"[0-9a-f]{48}"
RULE_NAMES = ('force-proxy', 'bootstrap', 'private', 'lancidr', 'apple', 'icloud', 'proxy', 'direct', 'cncidr')
LINKS = (
    ('clash', 'Mihomo / Clash Meta'),
    ('quantumult-x', 'Quantumult X 完整配置（配置文件导入）'),
    ('quantumult-x/nodes', 'Quantumult X 节点（server_remote）'),
    ('quantumult-x/rules', 'Quantumult X 受管规则（filter_remote；需完整配置的 VPS 策略）'),
    ('quantumult-x/rules-portable', 'Quantumult X 独立规则（filter_remote；内置 proxy/direct）'),
)
# canonical suffix, physical legacy suffix, MIME
ASSETS = (
    ('clash', 'clash.yaml', 'application/yaml'),
    ('quantumult-x', 'qx/full.conf', 'text/plain'),
    ('quantumult-x/nodes', 'qx/nodes.txt', 'text/plain'),
    ('quantumult-x/rules', 'qx/rules.list', 'text/plain'),
) + tuple(('rules/' + name, 'rules/' + name + '.yaml', 'application/yaml') for name in RULE_NAMES) + (
    ('quantumult-x/rules-portable', 'qx/rules-portable.list', 'text/plain'),
)


def asset_paths(token):
    """Return canonical path, physical manifest key and MIME for one identity."""
    if not re.fullmatch(TOKEN_PATTERN, token):
        raise ValueError('invalid subscription token')
    return [('/' + token + '/' + clean, 'sub/' + token + '/' + legacy, mime)
            for clean, legacy, mime in ASSETS]


def canonical_path(token, suffix):
    return next(path for path, name, _ in asset_paths(token) if name.endswith('/' + suffix))


def labelled_paths(token):
    paths = {path.removeprefix('/' + token + '/'): path for path, _, _ in asset_paths(token)}
    return [(label, paths[suffix]) for suffix, label in LINKS]


ASSET_PATTERN = r'^/sub/' + TOKEN_PATTERN + '/(' + '|'.join(re.escape(legacy) for _, legacy, _ in ASSETS) + ')$'
PUBLIC_PATTERN = (r'^/(' + TOKEN_PATTERN + '/(' + '|'.join(re.escape(clean) for clean, _, _ in ASSETS)
                  + ')|sub/' + TOKEN_PATTERN + '/(' + '|'.join(re.escape(legacy) for _, legacy, _ in ASSETS) + '))$')


def caddy_rewrites():
    # Each rewrite is exact and method-constrained by the enclosing asset route.
    return ''.join(f'        @canonical{i} path_regexp canonical{i} ^/({TOKEN_PATTERN})/{re.escape(clean)}$\n'
                   f'        rewrite @canonical{i} /sub/{{re.canonical{i}.1}}/{legacy}\n'
                   for i, (clean, legacy, _) in enumerate(ASSETS))
