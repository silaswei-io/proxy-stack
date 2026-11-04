# Third-party material

## Rule data

The updater downloads the release assets `private.txt`, `apple.txt`, `icloud.txt`,
`proxy.txt`, `direct.txt`, `cncidr.txt`, and `lancidr.txt` from
https://github.com/Loyalsoldier/clash-rules/releases/latest/download/ .

Copyright belongs to Loyalsoldier and the upstream contributors identified in
https://github.com/Loyalsoldier/clash-rules . The repository distributes its rule
data under GNU GPL version 3. The full upstream license is included as
[UPSTREAM-RULES-LICENSE](UPSTREAM-RULES-LICENSE). Source lists and their provenance
are available in the upstream repository. Preserve these notices and comply with
upstream licensing when distributing downloaded or converted rule data. This
project's MIT license applies to its own code, not to third-party rule datasets.

`manager/src/proxy_stack/bootstrap.json` is a small independently curated set of
public domain names/network facts for offline initialization, not a copy of the
full upstream release. It is explicitly labeled minimal-bootstrap at runtime.

## Quantumult X syntax reference

The small syntax excerpts in `tests/fixtures/qx-official-sample.conf` quote
crossutility's official sample at
https://raw.githubusercontent.com/crossutility/Quantumult-X/master/sample.conf
(consulted 2026-11-04). Attribution: crossutility / Quantumult X contributors.
They are documentation/reference snippets only, not a claim of native validation
and not a bundled client implementation.

## Runtime dependencies

- PyYAML 6.0.3: MIT, https://github.com/yaml/pyyaml
- Python: PSF License, https://www.python.org/psf/license/
- Caddy: Apache-2.0, https://github.com/caddyserver/caddy
- Xray-core: MPL-2.0, https://github.com/XTLS/Xray-core

Container images retain their own component notices and licenses.

The V1 tool image bundles the unmodified official Xray-core 26.7.11 binary.
Its MPL-2.0 license, exact source URLs and generic redistribution notice are
installed alongside this project’s MIT license in `/usr/share/licenses/proxy-stack/`.
