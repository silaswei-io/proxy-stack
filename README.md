# proxy-stack 0.1.1

已安装 Docker 的服务器上运行：

```sh
docker run --rm ghcr.io/silaswei/proxy-stack:0.1.1 installer | bash
```

回答两个问题：**代理域名、节点名称**。工具会生成配置和独立凭据、启动服务，并显示带标签的 owner 私密订阅。默认目录为 root 的 `/opt/proxy-stack`，普通用户的 `~/proxy-stack`；不需要 git、宿主机 Python、手改 `.env` 或 Docker socket 挂载。安装结束显示绝对路径的中文管理菜单命令，例如：

```sh
/opt/proxy-stack/proxyctl menu
```

菜单可以查看自己的订阅、添加/撤销朋友、查看服务及规则状态、刷新规则。朋友标识使用英文/拼音；添加或撤销会重启 Xray、中断现有连接。重复安装只打开已有受管目录的菜单（无终端则显示已有链接），保留 `.env`、数据、UUID、token 和模板；拒绝非空未受管目录及符号链接路径。安装中断后可在原目录执行 `./proxyctl init`，再 `./proxyctl up`，不会生成替换已有凭据。

每次新安装在 `.env` 保存独立 Compose 项目名；受管目录的包装器使用自己的 `.env`，忽略当前 shell 中继承的部署/Compose 配置变量，避免影响其他项目。更改部署设置仍需遵循已有设置不可静默迁移的约束。

域名 A/AAAA 必须指向服务器，公网 80/443 和 CAA 必须满足证书签发要求。**Docker 启动成功不代表 TLS 或外部可达性已验证**；安装后按提示等待证书和完整规则，再用菜单查看状态、运行 `proxyctl verify`。初始规则是小型 `minimal-bootstrap`，`rules=upstream` 才表示完整规则已发布。安装器先取得链接再启动 updater，收尾不会等待首次完整规则刷新锁。

80/443 无法绑定时会询问是否使用已有反代模式，仅绑定 `127.0.0.1:18090`，并检查该端口；工具不会停止、覆盖现有服务或修改防火墙/DNS。需手动审查生成的 `data/runtime/host-caddy.snippet`，让主机反代为该域名提供 HTTPS 并指向此网关，完成前公共链接不可用；Nginx 等反代需自行提供等效配置。没有自动适配所有反代环境。

GHCR 包目前为 private，公开前需要有读取权限的 registry 登录。要让新服务器直接匿名运行上述命令，包所有者需在 [包设置](https://github.com/users/silaswei/packages/container/package/proxy-stack) 中将可见性改为 Public；源码仓库继续保持私有。工具不会管理 registry 登录或包权限。

无终端自动化必须显式提供输入，缺失输入会失败而非等待。可先只初始化（不启动服务，仍检查端口）：

```sh
docker run --rm ghcr.io/silaswei/proxy-stack:0.1.1 installer | bash -s -- \
  --domain proxy.example.com --name 'My VPS' --directory /opt/proxy-stack \
  --mode standalone --non-interactive --prepare-only
# 复用已有反代时用 --mode existing-caddy；默认 auto 遇到端口冲突需要交互选择。
```

个人 VPS 的 Docker Compose 代理：Xray VLESS + WebSocket + TLS，Caddy 自动证书，以及 Mihomo/Clash Meta、Quantumult X 订阅。国内、局域网和 Apple/iCloud 直连，其余默认代理。每位朋友有独立 UUID 和订阅 token，可单独撤销。

这是小范围的 v0.1.1 实现。**QX 原生 iOS 导入仍未验证**；输出依照官方 sample 的原生 VLESS/WSS 和配置区段语法，结构测试与 HTTP 200 均不等于真实客户端验收。历史上“第 4 行语法错误”的根因没有证实，不能把此前模板调整称为修复成功。参见 [客户端验收](docs/CLIENTS.md)。

## 环境与边界

- Linux VPS；目标平台 Debian 12/13、Ubuntu 22.04/24.04。发行版与双架构 VPS 尚需实机验收，不能把本地 Docker 测试当成这些平台已通过。
- Docker Engine 和 **Compose ≥ 2.24.4**（已有 Caddy override 使用 `!override`）。不支持旧版 `docker-compose`。
- 自己控制的 ASCII/punycode 域名，正确 A/AAAA；公网 TCP 80/443 可达，CAA 允许 ACME CA。独立模式默认占用 80/443；端口被占用时 Docker 会失败，工具不会停止现有服务。
- 不修改宿主机默认路由、系统代理、DNS、现有反代或 systemd 服务。代理流量从 VPS 普通网络出口发出。只支持 TCP；生成的客户端配置关闭 UDP 转发和 IPv6 DNS。
- WS 本身不是加密层，客户端到 Caddy 的 TLS 提供传输加密；Caddy 到 Xray 的 WS 只走项目 bridge 网络。Xray 不发布宿主机端口。
- 无面板、计费、Reality、DNS API、自动升级或凭据轮换命令。这些是未来功能，不提供空实现。
- 固定版本 Xray 会阻止私有地址的代理出站；局域网由客户端规则直连，不通过 VPS 访问服务器内部网络。此版本还会提示 WS 传输已弃用；v0.1.1 按兼容目标保留 WSS，未来迁移需单独验收。

## 高级用法：源码 Compose 安装与初始化

源码仓库 [silaswei/proxy-stack](https://github.com/silaswei/proxy-stack) 为私有仓库，克隆前需配置具有该仓库读取权限的 GitHub 身份。下面使用 HTTPS，需通过 Git 凭据管理器等方式认证；也可使用已授权的 SSH key 克隆。源码访问认证与 GHCR 镜像拉取认证是两回事。

默认使用预构建工具镜像 `ghcr.io/silaswei/proxy-stack:0.1.1`。安装前确认发布流程已生成此标签；若标签尚不可用，可使用下方本地构建方案。目标为公开 GHCR 包，公开后可匿名拉取；若包仍为 private/internal，则需先登录 GHCR，身份必须有该包的读取权限，使用 PAT (classic) 时需 `read:packages`（组织要求时还需 SSO 授权）。拥有源码仓库读取权限不代表 Docker 已登录 registry；私有源码仓库也不意味着关联包必然私有。本文不将计划中的发布视为已经成功。

```sh
git clone https://github.com/silaswei/proxy-stack.git
cd proxy-stack
cp .env.example .env
# 编辑 DOMAIN、NODE_NAME；不要往 .env 写 UUID、token 或密钥。
docker compose --profile tools pull
./proxyctl init
docker compose up -d
./proxyctl status
./proxyctl links
./proxyctl verify
```

也可直接运行 `docker compose run --rm --no-deps manager init`。manager 只在显式调用时运行，默认 `docker compose up -d` 启动 Xray、Caddy、updater。manager/updater 使用同一个工具镜像。

需要本地构建时，先完成上述克隆及 `.env` 配置，将 `.env` 中的工具镜像改为：

```dotenv
TOOL_IMAGE=proxy-stack-tool:0.1.1
```

然后用以下命令替代预构建工具镜像的拉取与启动步骤：

```sh
docker compose build manager updater
./proxyctl init
docker compose up -d
```

固定版本镜像为 `python:3.13-slim-bookworm`、`ghcr.io/xtls/xray-core:26.7.11`、`caddy:2.11.4`，默认值均已锁定实际核对并拉取的多架构 index digest（见 Dockerfile、Compose 和 `.env.example`）。工作流可构建 linux/amd64、linux/arm64，不能替代两架构运行测试。

首次 `init` 无需访问 GitHub：使用仓库中的 **minimal-bootstrap 小型人工精选规则**。其中中国 IP 只有一个示例网段，不是完整国内分流库。updater 启动后立即拉取完整规则，成功后按 `UPDATE_INTERVAL`（默认一天）刷新；失败每 5 分钟重试。`status` 中 `rules=upstream` 才表示完整的上游列表已发布。应在日常使用前检查此状态。

重复初始化保留全部 UUID、token 和 WS 路径。DOMAIN、端口、节点名称、模式等初始化设置不允许静默变更；不一致时明确失败，恢复原 `.env` 后重试。已有状态不得为了改域名直接删除，否则全部链接和凭据都会变。

`.env.example` 所有值均非秘密。自定义 `PUBLIC_PORT` 时，客户端链接和 Caddy 站点使用同一端口；ACME 的 HTTP-01 仍要求公网 80 能到达 Caddy。`HTTP_PORT` 主要供本地测试，改成其他公网端口不会改变 CA 的验证规则。

## 已有主机 Caddy

在初始化前设置：

```dotenv
MODE=existing-caddy
COMPOSE_FILE=compose.yaml:compose.external.yaml
```

然后执行上述拉取（或本地构建）、初始化及启动步骤。`COMPOSE_FILE` 由 Compose 读取，必须与 MODE 一起设置；单独设置 MODE 不会改变 Compose 端口。也可以在每个命令显式使用 `docker compose -f compose.yaml -f compose.external.yaml …`。

此布局把项目 Caddy 作为内部网关，**仅发布 `127.0.0.1:18090 → 8080`**，覆盖默认端口列表，完全不发布 80/443。Xray 仍仅在容器网络内。主机 Caddy 负责公共 TLS，只需将整个站点反代到 `127.0.0.1:18090`。生成的待审查片段在 `data/runtime/host-caddy.snippet`：

```caddyfile
proxy.example.com:443 {
    reverse_proxy 127.0.0.1:18090
}
```

工具不接触主机配置、不 reload。管理员应先审查、备份并执行主机 `caddy validate`，再按自己的运维流程 reload。复用已有同名站点时合并其现有路由，不能重复声明站点或覆盖其他业务。共享反代若启用 access/error 日志，应自行脱敏 `/sub/…`，避免把订阅凭据写进日志。

## 日常命令

```sh
./proxyctl links                    # owner 的 4 个私密资源链接
./proxyctl menu                     # 中文交互菜单；TTY 下无参数也可打开
./proxyctl user add alice           # 创建朋友并在运行时重启 Xray
./proxyctl links --user alice
./proxyctl user revoke alice        # 删除订阅与 UUID，并重启 Xray
./proxyctl update-rules             # 只更新规则和订阅，不改写/重启 Xray
./proxyctl status                   # 脱敏的发布状态、用户名称、刷新警告
./proxyctl verify                   # 真实 HTTPS GET/HEAD、MIME、哈希和拒绝路径检查
./proxyctl verify --base-url http://caddy:8080  # 已有 Caddy 模式，仅内部 HTTP 检查
./proxyctl up
./proxyctl down                     # 保留数据、证书；不接受 -v 等删除参数
```

owner 永久保留。用户名仅允许 `[a-z][a-z0-9_-]{0,31}`。用户变更将整组订阅、私有状态和 Xray 配置重新生成；manager 不挂载 docker.sock，只有 shell 包装器显式重启正在运行的 Xray。直接执行 manager 的 `user` 命令后，管理员必须执行 `docker compose restart xray`。重启前，旧内核仍可能接受已撤销 UUID 的新连接，既有隧道也可能继续；重启会中断所有用户的现有隧道。若重启失败，应立即重试并确认状态，不能把生成配置成功当作内核已应用。

订阅地址是持有即可使用的秘密。`init`、刷新日志、status 不显示 UUID/token；安装器、主动执行 `links` 和菜单的订阅选项会向操作者展示链接，请勿记录进普通日志、提交、截图公开或交给第三方二维码站。通过 HTTPS 提供的节点订阅必然包含对应用户 UUID。

## 发布、规则与数据

```text
data/model/state.json          私有用户模型，0700 目录 / 0600 文件
data/runtime/xray/config.json  只向 Xray 挂载的配置
data/runtime/caddy/            只向 Caddy 挂载的入口配置
data/runtime/host-caddy.snippet 待管理员审查片段
data/cache/                    最后一次规则集和刷新状态
data/locks/operation.lock      manager / updater 共享进程锁
data/public/releases/<id>/    成套发布文件及不可公开的哈希 manifest
data/public/current           原子切换的相对 symlink
```

私有目录及公共资源的宿主机父目录均限制访问；Xray 镜像以 UID 65532 运行，因此其子目录 0755、配置 0644，宿主机上的 `runtime` 父目录仍为 0700。updater 只读挂载模型，读写规则缓存、公共资源与锁，根本不挂载 runtime。Caddy 只读挂载公共资源与自己的配置，不挂载模型或 Xray 配置。没有 privileged、host network、docker.sock。

发布先生成新 revision、校验全部文件和 SHA-256，再原子切换 `current`，清理旧 revision（因此不会无限占用磁盘，也不会残留已撤销 token 文件）。只有固定的 token 资源路径允许 GET/HEAD，其他路径和目录列表返回 404；manifest 和私有文件不提供 HTTP 路由。不要手工往 public 写文件或 symlink。工具拒绝读取/发布时的 symlink 路径穿越；其可信前提仍是宿主机和写卷的进程没有被接管，文件服务器本身不是对抗恶意宿主机写入的隔离边界。

所有写操作使用 `flock` 串行执行。普通渲染/写入/切换失败会保留原发布并还原私有配置；不会对 DNS、ACME、Docker 和多个文件承诺跨系统原子事务。突然断电或进程被杀可能留下未选择的候选 revision 或写到一半的用户变更；恢复原 `.env`、重新 `init`（按 state 重新协调配置）、验证后重启 Xray。规则更新器永远不写私有模型。普通更新不需要重启 Caddy。跨多个 HTTP 请求可能读取到先后两个 revision，日常更新不改变策略名或节点 tag。

规则源固定为 [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) 的 `releases/latest/download/` 下 `private/apple/icloud/proxy/direct/cncidr/lancidr.txt`，不接受请求参数或环境变量提供任意源 URL。每份下载最多 16 MiB、30 秒网络超时，安全 YAML 解析禁止标签/别名；规则数量范围 1–500000。完整缓存后任何一个列表缩减超过 40% 都拒绝整批更新；首次小型 bootstrap 不参与相对缩减判断。失败留下最后一次好版本和独立刷新警告，healthcheck 以发布内容有效性为准，不会因上游故障把有效订阅判死。调度可响应 SIGTERM；正在进行的单次网络读取有超时，下一份下载前检查停止信号。

Mihomo 使用本服务器上的分组 classical rule-provider；QX 使用相同顺序的合并规则列表。所有列表都在每个用户的 token 路径下镜像，客户端无需直连 GitHub。顺序：四个 IP 查询域名代理 → 节点/订阅引导域名直连 → 私网/LAN → Apple/iCloud 直连 → 已知代理域名 → `.cn`/国内域名 → 国内 IP → 默认 VPS。精确域名、后缀、通配符、IPv4、IPv6 均经过统一中间模型转换；不支持的条目使整批失败。DNS 分别按客户端语法生成，未声称两端 DNS 行为完全等价。

项目代码使用 MIT；下载的上游规则及其衍生数据按上游 GPL-3.0 许可处理，不能把项目 MIT 当作规则数据的许可证。来源、归属和随仓库提供的许可证见 [THIRD_PARTY.md](docs/THIRD_PARTY.md)。仓库不附带完整上游规则转储。

## 测试与验收

```sh
docker build -t proxy-stack-tool:0.1.1 -f manager/Dockerfile .
docker run --rm --entrypoint python -v "$PWD/tests:/tests:ro" \
  proxy-stack-tool:0.1.1 -m unittest discover -s /tests -v
DOMAIN=proxy.example.com docker compose config --quiet
DOMAIN=proxy.example.com docker compose -f compose.yaml -f compose.external.yaml config --quiet
# Linux（rootful Docker）：
sudo python3 tests/integration/run.py
# macOS Docker Desktop 则使用：python3 tests/integration/run.py
```

离线单元测试不访问 GitHub、不需要真实秘密。集成测试使用实际 Docker 镜像、临时目录和独立 bridge 网络，自动清理，只绑定回环随机高端口；验证 Mihomo/Xray/Caddy 原生配置、订阅路由、可信测试 CA 的 TLS、真实 WS/VLESS TCP 出站，以及撤销后链接和 UUID 失效。初次可能需从公共 registry 拉取固定镜像，不使用生产域名或公网 ACME。Xray 默认阻止私有出站地址；集成测试只在另一份临时配置中精确放行模拟目标 IP 的 TCP 8000 端口，生成的生产配置不变。集成脚本需要宿主机 Python ≥ 3.9 和 Docker，不依赖宿主机 PyYAML。

Linux 集成测试要求通过 `sudo` 使用 rootful Docker：临时 bind mount 目录需由 root 创建，才能保持 manager 的 `cap_drop: ALL` 并执行目录权限操作；宿主机 Python 还需读取容器生成的 root 所有、0700 的模型目录。仅把 data 改成 0777 不能解决这两处权限问题。CI 与 release 的集成步骤均以 sudo 运行，以对应生产 Compose 自动创建的 root 所有挂载；不增加生产容器 capability、不放宽私有目录权限。macOS Docker Desktop 的文件共享行为不同，使用普通用户运行即可。

`verify` 检查真实 HTTP 响应、系统信任链、全部 owner 资源的哈希/MIME/GET/HEAD、未知 token 和目录拒绝。内部 `--base-url http://…` 明确不验证 TLS；verify 不验证 VLESS 出站或你的客户端所在网络。实际客户端还需按 [CLIENTS.md](docs/CLIENTS.md) 完成导入、规则刷新与出口检查。QX 无公开完整解析器，不能用这些测试替代 iOS。

CI 在 push/PR 测试；release 工作流仅在 `v0.1.1` tag 或手动运行时以 `GITHUB_TOKEN` 推送 GHCR 多架构镜像，标签为 `0.1.1` 与 `sha-<完整提交>`，带 source/version/revision 标签。发布由仓库所有者执行；工作流推送镜像不保证包已经公开，GHCR 可见性需由所有者在 GitHub 包设置页面单独确认或设置。实际可拉取性以发布结果及包权限为准。

## 备份与回退

对 `data/model`、`data/runtime`、`data/cache` 做访问受限的加密备份，必要时也备份 Caddy 命名卷证书。公共订阅同样包含秘密，不能上传公开存储。记录使用的镜像 tag/digest；回退代码/镜像前先停 updater、核对 schema=1，恢复同一时点的模型/配置/缓存，使用原 `.env` 重新初始化并验证，再显式启动服务。v0.1.1 不提供自动跨版本迁移或旧 revision 选择命令。

`down` 不删除数据或证书卷。已有 Caddy 的反代片段由管理员按原有配置所有权手动移除；工具不改共享入口或其他项目。完整删除属于人工运维操作，应先备份并确认路径和卷名。
