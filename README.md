# proxy-stack v1.0.0

一个订阅中心管理多台机器。每位朋友各有稳定订阅 URL 和独立 UUID；同一个 Mihomo/Clash Meta 或 Quantumult X 订阅包含所有已就绪节点。国内、局域网、Apple/iCloud 直连，其余走 `VPS` 节点组。没有就绪节点时，默认代理流量被拒绝，避免意外直连。

## 安装

服务器只需 Docker Engine 和 Compose **≥ 2.24.4**，不需要宿主机 Python 或 git：

```sh
docker run --pull=always --rm ghcr.io/silaswei/proxy-stack:v1.0.0 installer | bash
```

向导提供三个选择：创建中心并同时运行节点、仅创建订阅中心、加入已有中心。地址默认探测到的公网 IP，可改为域名；节点名默认使用宿主机 hostname。IP 无需域名/DNS，但仍需可从公网访问。不能填写私网、回环或保留地址。

在第一台机器建立中心，等待 HTTPS 证书就绪，然后运行：

```sh
/opt/proxy-stack/proxyctl node code
```

加入码有效 **10 分钟**，只可使用一次；在第二台机器运行同一安装命令，选择“加入已有中心”并粘贴加入码。更多机器重复此过程。中心可以没有自己的代理节点。

独立模式使用公网 80/443。80/443 冲突时，可选择已有反代模式，只发布 `127.0.0.1:18090 → 8080`；需自行审查 `data/runtime/host-caddy.snippet` 并配置公共 HTTPS。工具不会修改已有主机反代、DNS、防火墙或其他服务。

域名必须正确配置 A/AAAA 和 CAA。公网 IP 使用 Caddy 2.11.4、Let’s Encrypt `shortlived` ACME profile 和 `default_sni`；约 160 小时的证书需要持续满足续期条件。**容器启动不等于公网可达或证书已签发**。本地测试不验证公共 ACME 签发与续期。

无终端也可初始化；省略地址和名称时使用相同默认值：

```sh
# 中心，同时提供本机节点。--role hub-only 表示仅订阅中心。
docker run --pull=always --rm ghcr.io/silaswei/proxy-stack:v1.0.0 installer | bash -s -- \
  --non-interactive --role hub --mode standalone --directory /opt/proxy-stack

# 加入节点：先把限时加入码保存到一个仅自己可读的文件。
# 秘密通过 stdin 传入容器，不放在命令参数或 .env。
docker run --pull=always --rm ghcr.io/silaswei/proxy-stack:v1.0.0 installer | bash -s -- \
  --non-interactive --role node --join-code-file /secure/join-code \
  --mode standalone --directory /opt/proxy-stack
```

可选 `--domain proxy.example.com --name 'My VPS'`；IPv6 使用原始地址，不加方括号。`--prepare-only` 初始化但不启动服务。端口检查仍会短暂预留预期绑定端口。新安装会拉取 v1.0.0；重复运行受管安装保留实际 `.env`、UUID、token、WS 路径和模板，不自动升级或重置。

加入失败后保留已初始化目录：在中心重新生成加入码，在节点执行 `./proxyctl node join`，然后 `./proxyctl up`。不需要删除数据。已加入的节点须先 `detach`，才能再次加入；中心可通过 `node remove ID` 清理不再使用的登记。

## 管理

```sh
./proxyctl menu                     # 中文菜单；终端中无参数也可打开
./proxyctl links --labelled         # 中心 owner 的四个私密资源 URL
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
./proxyctl upgrade                  # 拉取 v1.0.0，重建原本运行的服务；保留配置和模板
./proxyctl up
./proxyctl down                     # 保留数据与证书，不接受删除参数
```

节点不生成自己的订阅链接；所有订阅与规则链接均指向中心，节点连接地址使用各节点自己的 IP/域名和 WS 路径。owner 永久保留；朋友标识匹配 `[a-z][a-z0-9_-]{0,31}`。添加/撤销不会更改其他用户的 UUID/token/URL。用户变更由各节点自动应用，重启该节点的 Xray 会中断其已有连接。

`node list` 的 `synced` 表示该节点已校验配置、成功启动 Xray 并回执当前 generation；`pending` 表示当前授权尚未确认；`offline` 表示超出租约心跳窗口；`removed` 表示已禁用。`acknowledged` 是最后确认版本，`active` 仅在当前版本就绪且租约有效时显示，否则为 0。不能把撤销后的 pending/offline 称作已应用。

节点默认每 2 秒轮询，中心租约默认 **300 秒**（`LEASE_SECONDS`，10–3600）。节点在失联时可暂用最后好配置，租约到期由独立单调时钟看门狗终止 Xray，即使控制请求阻塞也执行。重启节点进程必须重新获取有效配置，不恢复磁盘上的旧租约。移除节点立即拒绝后续控制请求，已离线节点最迟在剩余租约结束停止。关闭/移除中心也受同一限制；需自行删除不再使用的主机 HTTPS 路由。

订阅只发布确认当前授权的节点；新增用户、撤销用户、节点启动/恢复期间可能暂时没有节点，默认代理流量此时 `REJECT`/`reject`。显式国内/LAN/Apple/iCloud 规则继续直连。客户端已经缓存的旧订阅不会被远程强制刷新，但节点实际授权仍由独立 UUID 和租约限制。

## 架构和数据

- `manager` 是一次性 CLI；`hub` 提供内部控制 API；`xray` 使用工具镜像内的固定官方 Xray 二进制并监督子进程；`updater` 只处理规则。加入节点上的 hub/updater 进程休眠。
- Caddy 只开放四个精确 POST 控制路径 `/control/v1/{enroll,poll,ack,detach}` 和精确订阅资源路径，控制监听器无宿主机端口。仅订阅中心不开放 Xray tunnel。
- 控制客户端始终校验 HTTPS，禁用重定向和环境代理。加入码有 192 位随机秘密，服务端只存摘要；加入后每个节点使用独立秘密。普通 status/日志不输出 UUID、token 或加入码；只有显式 links/code 与安装收尾主动显示。
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

私有目录 0700、模型与控制凭据文件 0600。订阅和 Xray 配置同样包含秘密；备份应加密并限制访问。不要公开 URL、UUID、token，也不要上传到第三方二维码站。已有反代的 access/error 日志应自行避免记录订阅路径或请求体。

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
# Linux rootful Docker；macOS Docker Desktop 去掉 sudo。
sudo python3 tests/integration/run.py
```

集成测试仅使用独立临时容器/网络、回环随机高端口与显式测试 CA。覆盖实际 Mihomo 经两台受监督 Xray 的 HTTPS/WSS/VLESS 出站、各节点独立用户同步与撤销、失联/阻塞期间租约停止、失败启动不回执、移除、稳定中心 URL；保留单节点和 IPv4/IPv6 SAN、无 SNI、错误证书拒绝、规则隔离测试。测试仅在独立临时配置中放行模拟 origin 的 IP:8000，不改变生产配置。Linux 需 sudo，以保持 root 所有的私有挂载和 `cap_drop ALL`；不能用放宽私有目录权限替代。

发布工作流在 main push 或 main 手动触发，测试后构建 amd64/arm64 immutable `sha-<revision>` 镜像；发布串行执行，确认仍为当前 main 才更新唯一用户入口 `ghcr.io/silaswei/proxy-stack:v1.0.0`。历史标签保留。

**QX 原生 iOS 导入、公共 ACME 签发/续期和真实 VPS/运营商网络尚未在本地环境验证。** 见 [客户端验收](docs/CLIENTS.md) 与 [验证记录](docs/VALIDATION.md)。代码 MIT；下载规则遵循上游 GPL-3.0，见 [第三方归属](docs/THIRD_PARTY.md)。
