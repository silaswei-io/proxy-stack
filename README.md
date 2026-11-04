# proxy-stack v1.0.0

一个订阅中心管理多台机器。每位朋友各有稳定订阅 URL 和独立 UUID；同一个 Mihomo/Clash Meta 或 Quantumult X 订阅包含所有已就绪节点。国内、局域网、Apple/iCloud 直连，其余走 `VPS` 节点组。没有就绪节点时，默认代理流量被拒绝，避免意外直连。

## 安装

支持 **Linux、本机 rootful Docker Engine、Compose ≥ 2.24.4、Bash 和 GNU coreutils（timeout/sha256sum）**。不支持 Docker Desktop 的端口预检、远程 Docker 或 rootless Docker。独立安装不需要宿主 Python/git；自动集成宿主 Caddy 需要 Linux root、systemd、Python 3 和 `ss`。

```sh
docker run --pull=always --rm ghcr.io/silaswei-io/proxy-stack:v1.0.0 installer | bash
```

向导逐行选择「中心 + 本机节点」（默认）、「仅订阅中心」或「加入已有中心」。地址默认探测到的公网 IP，名称默认 hostname，均可回车接受；公共 HTTPS 端口默认选取 **20000–59999 的空闲随机端口**，也可自行输入。`--port 52789` 指定端口（1–65535，80 留给 HTTP-01）。安装前及激活前都会重新检查占用；订阅与节点地址包含所选端口，重复运行保留已选端口。

栈内固定 Caddy **2.11.4** 在所选端口终结 TLS，仅使用 Let’s Encrypt HTTP-01。公网 80 空闲时直接发布栈内 80；若被受支持的 stock systemd Caddy ≥ 2.6 占用，工具自动验证完整候选配置，添加**仅此地址 ACME challenge 路径**到已检查的随机回环端口，再转入栈内 80。宿主 443、Caddy 二进制、unit 及原有路由/import 原文保持不变，不需要升级宿主 Caddy。公共 IP 使用 `shortlived` profile 和栈内 `default_sni`。

自动适配要求宿主 root 所有的普通配置/程序、可确认的监听 PID/启动参数、stock 模块、标准本机 admin/reload，且运行配置与磁盘一致。自定义模块、未知监听者、目标地址已有站点/catch-all、动态/不安全配置等会在预检失败，**不写入安装目录、不修改宿主服务**。域名的全部 A/AAAA 必须能由本机 IPv4/IPv6 出口证明；额外多地址/NAT 无法证明时会明确拒绝。工具不更改 DNS、防火墙或云平台设置；公网需允许 **80 和所选 HTTPS 端口**，域名 CAA 需允许签发。

先看完整计划，再安装；`--check`（同 `--plan`）可拉取/验证镜像、创建私有临时计划和短暂端口探测容器，但不写入安装目录、启动栈或重载宿主：

```sh
docker run --rm ghcr.io/silaswei-io/proxy-stack:v1.0.0 installer | bash -s -- \
  --check --domain YOUR_PUBLIC_IP --port 52789 --directory /opt/proxy-stack

# 无交互：--manage-host-caddy 仅授权计划中的宿主 HTTP-01 路由事务。
docker run --rm ghcr.io/silaswei-io/proxy-stack:v1.0.0 installer | bash -s -- \
  --non-interactive --domain YOUR_PUBLIC_IP --port 52789 \
  --manage-host-caddy --directory /opt/proxy-stack
```

交互模式在宿主路由或受管重配前只确认一次计划；无交互自动集成须显式 `--manage-host-caddy`。可选 `--name 'My VPS'`、`--role hub-only`；原始 IPv6 不加方括号。镜像拉取、候选验证和 HTTPS 等待均有上限并显示简短进度，常规 Docker 输出写入提示的私有日志（0600）。加入码、UUID 和私密订阅 URL 不写日志。

安装使用持久事务记录：修改前备份配置和身份，原子替换宿主配置并 native 验证/重载；服务启动后最多 **120 秒**验证公共 HTTPS。中心需返回自身订阅的确切内容与正确 TLS；选了本机节点还需当前 generation 的有效运行回执，节点模式还检查自身公共 WSS 和近期本机运行/同步。失败恢复宿主原配置、受管原模板/状态和原本运行的服务；新安装只清理自己的 Compose 资源，并把私有状态留在事务备份。没有 HTTPS 或有效节点回执时不会显示可用订阅链接。

仅中心模式可以在 HTTPS 已验证、零节点时完成安装，收尾明确「等待就绪节点回执」；在中心菜单生成限时加入码即可继续：

```sh
/opt/proxy-stack/proxyctl node code
```

加入码有效 **10 分钟**、只用一次。在其他服务器运行相同向导，选「加入已有中心」并粘贴隐藏输入；无交互使用 `--role node --join-code-file /secure/join-code`。秘密只经 stdin 传入，不能放进命令参数。新节点安装失败会撤销其独立登记；中心暂时不可达时保留这份私有凭据及事务锁，恢复后重试撤销，不遗留一个被误报成功的节点。

相同 v1.0.0 的受管安装可直接重跑：保持 UUID/token/WS 路径，显式 `--domain`/`--name`/`--port` 会预检并事务重配、刷新随包模板和再生成运行配置。已有的 pending `MODE=existing-caddy` 默认重试为栈内 HTTPS，未指定新端口时选择空闲随机端口。角色不自动切换；中心已有远程节点或节点已有登记时，不能自动改变其公共控制/节点地址，预检会保留原状态并拒绝。正常无变更重跑不重建原容器；失败不停止原本运行的容器。

普通重跑会发现未完成事务，先安全恢复；也可只恢复，不继续安装：

```sh
docker run --rm ghcr.io/silaswei-io/proxy-stack:v1.0.0 installer | bash -s -- \
  --directory /opt/proxy-stack --recover
```

活动安装锁不会被抢占；`--check` 发现未完成事务仅报告恢复命令。若检测到外部并发改动或暂时无法撤销登记，保留锁、备份和私有日志，恢复可再次执行。事务记录目录应保留到确认完成；不会删除原用户私有状态。

`--prepare-only` 初始化但不启动栈、不探测公共就绪、不输出私密链接；它不能重配正在运行的安装。专家可显式 `--mode existing-caddy --prepare-only` 生成 `data/runtime/host-caddy.snippet`，保留片段 TLS/global 要求；这是手动集成的高级入口，正常安装使用自动 HTTP-01 流程。

```sh
/opt/proxy-stack/proxyctl finish       # 单次最多约 4 秒；待完成返回 1，就绪返回 0
/opt/proxy-stack/proxyctl finish --wait 120
/opt/proxy-stack/proxyctl menu         # 重试完成检查；我的订阅查看 Quantumult X 等链接
```

`finish` 仅在公共 HTTPS 返回本中心内容、至少一个已确认当前配置且租约有效的节点时显示一个 Mihomo 主 URL；仅中心需要远程就绪节点。检查始终验证 TLS，不跟随重定向、不使用环境代理，网络等待不持有状态锁。节点检查自身 HTTPS/WSS 和运行/同步，不检查订阅。它不代表真实客户端导入或代理出站验收，也不能保证云防火墙、NAT、公共 ACME 签发和持续续期。

## 管理

```sh
./proxyctl menu                     # 中文菜单；终端中无参数也可打开
./proxyctl finish                   # 检查完成条件；成功才显示一个主订阅 URL
./proxyctl links --labelled         # 显式披露 owner 四个私密 URL，不检查就绪
./proxyctl user add alice           # 独立 UUID/token，下发所有节点
./proxyctl links --user alice
./proxyctl user revoke alice        # 立即移除订阅，等待各节点应用回执
./proxyctl node list                # ID、desired、acknowledged、active、同步状态
./proxyctl node code                # 显式显示一次性加入码
./proxyctl node remove NODE_ID      # 立即使节点控制凭据失效；本机 ID 为 local
./proxyctl node join                # 节点读入加入码；隐藏终端输入，也接受 stdin
./proxyctl detach                   # 节点停止参与中心；中心不可达时本地仍会解除
./proxyctl status
./proxyctl verify                   # 中心真实 HTTPS 资源哈希、MIME、GET/HEAD 检查
./proxyctl update-rules
./proxyctl upgrade                  # 拉取 v1.0.0，重建原本运行的服务；保留身份与模板，迁移旧官方镜像默认
./proxyctl up
./proxyctl down                     # 保留数据与证书，不接受删除参数
```

订阅使用不带扩展名的私密地址：`https://host/<48hex-token>/clash`、`/<48hex-token>/quantumult-x`（完整配置）、`/<48hex-token>/quantumult-x/nodes`、`/<48hex-token>/quantumult-x/rules`。Mihomo 规则资源为 `/<48hex-token>/rules/<name>`。token 保持原有 48 位十六进制熵；旧 `/sub/<token>/clash.yaml`、`/sub/<token>/qx/...` 和规则地址继续可用。Caddy 仅对精确 GET/HEAD 做内部映射，不跳转、不接受查询参数；底层文件和 manifest 路径保持兼容。

官方项目与镜像命名空间为 `silaswei-io/proxy-stack`，公开版本仍为 `v1.0.0`。旧 `ghcr.io/silaswei/proxy-stack:v1.0.0` 的受管安装可重跑新安装器或运行新版 `proxyctl upgrade`，仅迁移官方镜像默认，保留 UUID、token、WS 路径和其他设置。自定义镜像和其他标签需手动迁移。已有安装要启用新订阅路由，请正常重跑新安装器（不用 `--prepare-only`）；`proxyctl upgrade` 只更新镜像默认与原本运行的服务，保留现有运行配置。安装器的模板更新仍使用原有事务备份/回滚；upgrade 在解析新镜像失败时恢复 `.env`，服务重建失败时可将 TOOL_IMAGE 恢复为旧官方地址后重试。

节点不生成自己的订阅链接；所有订阅与规则链接均指向中心，节点连接地址使用各节点自己的 IP/域名和 WS 路径。owner 永久保留；朋友标识匹配 `[a-z][a-z0-9_-]{0,31}`。添加/撤销不会更改其他用户的 UUID/token/URL。用户变更由各节点自动应用，重启该节点的 Xray 会中断其已有连接。

`node list` 的 `synced` 表示该节点已校验配置、成功启动 Xray 并回执当前 generation；`pending` 表示当前授权尚未确认；`offline` 表示超出租约心跳窗口；`removed` 表示已禁用。`acknowledged` 是最后确认版本，`active` 仅在当前版本就绪且租约有效时显示，否则为 0。不能把撤销后的 pending/offline 称作已应用。

节点默认每 2 秒轮询，中心租约默认 **300 秒**（`LEASE_SECONDS`，10–3600）。节点在失联时可暂用最后好配置，租约到期由独立单调时钟看门狗终止 Xray，即使控制请求阻塞也执行。重启节点进程必须重新获取有效配置，不恢复磁盘上的旧租约。移除节点立即拒绝后续控制请求，已离线节点最迟在剩余租约结束停止。关闭/移除中心也受同一限制；需自行删除不再使用的主机 HTTPS 路由。

订阅只发布确认当前授权的节点；新增用户、撤销用户、节点启动/恢复期间可能暂时没有节点，默认代理流量此时 `REJECT`/`reject`。显式国内/LAN/Apple/iCloud 规则继续直连。客户端已经缓存的旧订阅不会被远程强制刷新，但节点实际授权仍由独立 UUID 和租约限制。

## 架构和数据

- `manager` 是一次性 CLI；`hub` 提供内部控制 API；`xray` 使用工具镜像内的固定官方 Xray 二进制并监督子进程；`updater` 只处理规则。加入节点上的 hub/updater 进程休眠。
- Caddy 只开放四个精确 POST 控制路径 `/control/v1/{enroll,poll,ack,detach}` 和精确订阅资源路径，控制监听器无宿主机端口。仅订阅中心不开放 Xray tunnel。
- 控制客户端始终校验 HTTPS，禁用重定向和环境代理。加入码有 192 位随机秘密，服务端只存摘要；加入后每个节点使用独立秘密。普通 status/日志不输出 UUID、token 或加入码；只有显式 links/code、菜单订阅操作与通过完成检查后的收尾主动显示。
- 节点只取得所需 UUID、自己的 WS 路径、generation 和租约，不取得用户订阅 token、其他节点控制秘密或中心完整状态。
- 所有模型、发布和规则操作共享 `flock`；原子选择公共 revision。心跳单独保存，只在节点可见性发生变化时重新渲染，不在每次轮询重建大型规则。旧 revision 及时删除，撤销 token 不在旧公共树中残留。
- updater 只读模型，读写 cache/public/locks，完全不挂载 runtime。没有 docker.sock、privileged 或 host networking。Caddy 单独保留低端口绑定 capability，其余容器 `cap_drop: ALL`。

```text
data/model/state.json            中心用户/节点模型，或节点自身非秘密设置
data/model/peer.json             加入节点自己的私密中心凭据
data/model/heartbeats.json       中心节点最近轮询时间
data/runtime/xray/active.json    监督运行器实际应用的配置
data/runtime/node-status.json    运行器状态；没有用户秘密
data/runtime/caddy/              本机 Caddy 配置
data/cache/                     最后一次完整规则与刷新状态
data/locks/                     跨进程写入锁
data/public/current             原子指向已校验的发布目录
```

私有目录 0700、模型与控制凭据文件 0600。订阅和 Xray 配置同样包含秘密；备份应加密并限制访问。不要公开 URL、UUID、token，也不要上传到第三方二维码站。栈内 Caddy 排除请求 access/error 日志，避免泄漏订阅/WS 私密路径。专家手动反代入口也应避免记录这些路径或请求体。

规则源固定为 [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) 的 private/apple/icloud/proxy/direct/cncidr/lancidr。首次为 `minimal-bootstrap`，不代表完整国内分流；updater 成功后 `rules=upstream`。下载限时限量，拒绝危险 YAML 与异常缩水，失败保留最后好版本。两客户端共用规则语义：IP 查询域名代理 → 中心/各节点引导直连 → 私网/LAN → Apple/iCloud → 已知代理 → `.cn`/国内域名 → 国内 IP → 默认 VPS。节点和中心地址使用精确域名或 /32、/128 规则。

只提供 TCP VLESS/WSS/TLS，不启用 UDP 或 IPv6 DNS。固定 Xray 默认阻止私有地址代理出站。没有面板、Reality、计费或自动公网运维。本次是全新 v1.0.0，不提供旧版本状态/模板迁移。

## 源码与验证

高级源码使用：复制 `.env.example` 为 `.env`，设置 DOMAIN、ROLE、LOCAL_NODE，运行 `./proxyctl init` 与 `./proxyctl up`。源码安装需要仓库读取权限；部署只使用 GHCR 镜像则无需 git。新 v1.0.0 的重复 init 保留身份，部署设置不允许静默更改。

```sh
docker build -t proxy-stack-tool:v1.0.0 -f manager/Dockerfile .
docker run --rm --network none --entrypoint python -v "$PWD/tests:/tests:ro" \
  proxy-stack-tool:v1.0.0 -m unittest discover -s /tests -v
DOMAIN=proxy.example.com docker compose config --quiet
DOMAIN=proxy.example.com docker compose -f compose.yaml -f compose.external.yaml config --quiet
# 快速 native 预检：真实 Debian Caddy 2.6.2、固定栈 Caddy 和导出安装器 --check。
python3 tests/integration/preflight.py
# 完整集成（内部也会运行上述 native 预检）；Linux rootful Docker 需 sudo。
sudo python3 tests/integration/run.py
```

native 预检用私有配置和回环随机端口；macOS 上的测试适配器仅替换 Linux 宿主判定、缓存镜像和探测绑定，不证明 Docker Desktop 的端口占用能力。完整集成测试仅使用独立临时容器/网络、回环随机高端口与显式测试 CA。覆盖实际 Mihomo 经两台受监督 Xray 的 HTTPS/WSS/VLESS 出站、各节点独立用户同步与撤销、失联/阻塞期间租约停止、失败启动不回执、移除、稳定中心 URL；保留单节点和 IPv4/IPv6 SAN、无 SNI、错误证书拒绝、规则隔离测试。测试仅在独立临时配置中放行模拟 origin 的 IP:8000，不改变生产配置。Linux 需 sudo，以保持 root 所有的私有挂载和 `cap_drop ALL`；不能用放宽私有目录权限替代。

发布工作流在 main push 或 main 手动触发，测试后构建 amd64/arm64 immutable `sha-<revision>` 镜像；发布串行执行，确认仍为当前 main 才更新唯一用户入口 `ghcr.io/silaswei-io/proxy-stack:v1.0.0`。历史标签保留。

**Quantumult X 原生 iOS 导入、公共 ACME 签发/续期和真实 VPS/运营商网络尚未在本地环境验证。** 见 [客户端验收](docs/CLIENTS.md) 与 [验证记录](docs/VALIDATION.md)。代码 MIT；下载规则遵循上游 GPL-3.0，见 [第三方归属](docs/THIRD_PARTY.md)。
