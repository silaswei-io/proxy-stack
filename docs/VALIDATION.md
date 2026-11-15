# v1.0.0 验证记录

## 2026-11-15 安装后独立订阅别名生命周期

本次功能只管理已安装 hub 的订阅别名，不重新实现安装器已有的域名/HTTPS。原控制入口、节点登记、用户身份和两跳线路保持独立；包含首选链接及嵌入资源 URL、中文菜单、容器/宿主 Caddy、事务回滚和中断恢复。已有手工别名采用明确的人工迁移流程，不自动接管未知站点，见 [订阅域名管理](SUBSCRIPTION_DOMAINS.md)。

最终代码在本地 macOS / Docker Desktop 29.2.1、Linux arm64 容器中完成以下验证：

- `docker build -t proxy-stack-tool:v1.0.0 -f manager/Dockerfile .` 成功。
- `docker run --rm --network none --entrypoint python -v "$PWD:/source:ro" -w /source -e PYTHONPATH=/source/manager/src proxy-stack-tool:v1.0.0 -m unittest discover -s tests -v`：**178 项全部通过，259.815 秒**。覆盖端口8443添加/删除及失败回滚、80拒绝、真实预检下停止 Caddy 后恢复、未知 include 拒绝、IPv4-only 端口探测、并发用户/节点/线路变更保留、wrapper/菜单，以及 `python -S` 独立 zip helper。
- `python3 tests/integration/run.py`：完整原生集成通过，包括 Mihomo、Xray、Caddy、精确资源 GET/HEAD/404、可信 TLS、实际 owner/朋友 VLESS/WSS 出站、多节点登记/撤销/租约、两跳线路与 updater 隔离。
- `python3 tests/integration/preflight.py`：最后一次安装器 include 校验变更后再次通过。覆盖固定 Caddy 2.11.4 和实际 Debian stock 2.6.2、宿主别名添加/移除/reload 失败回滚、原文件字节恢复、朋友及 QX 资源、外层/上游不可信 TLS 拒绝、证书重启保留和私密路径不入日志。
- 上述 preflight 包含真实 Docker Compose 生命周期：添加后只重建 Caddy、HTTPS 验证、重启/init 持久化、移除、失败回滚和停止后的恢复；导出的安装器重跑后，真实 `docker compose config` 仍保留原 HTTPS、HTTP-01 和新增别名443。
- `sh -n proxyctl`、`bash -n manager/src/proxy_stack/templates/installer.sh`、根 wrapper 与模板 `cmp`、Python `compileall` 及 `git diff --check` 均通过。

端口夹具故障已定位：Docker 29 的 `internal` 网络容器虽然保留 `HostConfig.PortBindings`，实际 inspect `NetworkSettings.Ports` 列表为空；网络内可信 HTTPS 成功不能证明已发布宿主端口。用一次性 internal/普通 bridge 容器对比确认后，Compose 夹具改用普通 bridge，明确断言实际发布到 `127.0.0.1` 的随机高端口，并从宿主以可信测试 CA 请求该端口，核对内容。仅测试容器内目标443；未绑定宿主80/443。

测试使用合成身份和临时 CA，不读取运行凭据；临时容器/网络/目录由 harness 清理。本次仅本地工作及提交，未 push、SSH、部署或修改真实服务。公网 ACME 实际签发/长期续期、防火墙外部可达性、Linux amd64 CI 和 iOS Quantumult X 原生导入未在本次验证。

## 2026-11-15 宿主 Caddy file_server 文件名误判修复

`Host.adapt` 仍在原配置旁写临时候选文件，仅将原生 Caddy 在 `file_server.hide` 中自动加入的精确临时路径还原为原配置路径；原配置已被用户显式隐藏时，按 Caddy 行为去重。其他 hide 项、其他 handler 和配置值均不替换，运行配置与磁盘、计划后复查、应用后验证及回滚的严格比较保留。

新增固定 digest 的 Caddy 2.11.4 原生回归，接入现有 integration preflight：隔离容器实际启动 Caddy，验证三个 file_server（含相对 import）、显式隐藏原文件、计划/原生校验/应用/复查/回滚，并通过真实 admin 配置变更确认用户 hide 漂移仍被拒绝。仅模拟 systemd/进程身份边界，不发布宿主端口。Docker 宿主适配单测 **17 项通过**，安装器回归 **55 项通过**（233.677 秒），原生回归通过；shell 语法、root/template 一致性和差异检查通过。此次不重跑无关完整套件；未 SSH、部署或修改真实服务。

## 2026-11-15 受管服务端两跳线路（本地 worktree）

实现保留三种安装角色和 v1.0.0 命名，仅增加中心线路模型、入口 desired 扩展、Xray user 路由与两种订阅的普通节点条目。每条线路持久化随机内部 ID；入口按用户 UUID namespace + 内部 ID 派生 UUID，普通出口保持 generation/lease_seconds/ws_path/users 四字段对象。控制扩展按入口裁剪为必要出口端点，避免发送完整拓扑或订阅/peer token。使用紧凑线路描述而非逐用户重复出站描述，入口再生成完整 Xray 配置。

本地 macOS / Docker Desktop 的 Linux arm64 验证：首次实现 **146 项完整单元/回归通过**（248.933 秒）；完整 `tests/integration/run.py` 通过，最终镜像的双节点/两跳部分另行复跑通过。两种 Compose 配置、shell 语法、root/template 包装器一致性和 `git diff --check` 通过。Linux amd64 GitHub Actions 未运行，未触发发布。

后续审查修复：finish 统计当前 manifest 所指订阅中、仍有有效租约和当前代次回执支持的可选线路，隐藏直连不再被当作可用订阅，端点隐藏的就绪两跳计为一条；核对本地资产哈希，并在 HTTPS 前后比较 manifest revision，拒绝并发发布期间的旧响应。原 local_required/local_ready 和订阅 URL 语义保留。租约过滤统一用于所有发布，包括未运行 reaper 时移除最后一条线路，以及没有 chains 字段的旧状态；不修改转发路径。Linux 容器定向回归 **52 项通过**（finish 16、chain 8、stack 22、routing parity 6），shell/root-template 一致性检查通过；此次修复未重跑完整 Docker 集成。

测试加入现有 `unittest discover` 与 `tests/integration/run.py` 路径，CI 和 main 发布流程自动执行，未修改测试后发布 GHCR 的门槛。新增覆盖旧状态/无线路快照、独立用户身份与撤销、精确出口协议、定向代次/回执、过期租约（含未运行 reaper 的刷新及续租恢复）、offline/remove/detach、CLI/中文菜单/包装器、名称/32 条上限和写入失败回滚。

本地 Docker 集成复用固定 digest 的 Xray 26.7.11、Caddy 2.11.4 和 Mihomo 1.19.15：普通节点正常加入，再创建线路并隐藏出口直连；不改测试网关配置，原生校验入口和出口 Xray 配置，实际导入生成的 Mihomo 条目并分别验证入口/出口源 IP。owner 与朋友分别使用自己的出口 UUID；出口停止后，入口直连仍成功、两跳及缓存线路凭据均无法出网，恢复出口后线路可用。撤销朋友、移除线路后原凭据拒绝，owner 保持可用。测试只给私有模拟 origin 的精确 IP/TCP 8000 增加测试许可，并通过容器环境指定一次性 CA；生产 TLS 验证和私网出站保护不变。

本次没有 SSH、部署、真实服务配置修改或 Git 提交/推送。临时测试网络、容器、凭据和目录由 harness 清理。运行中的入口配置更新沿用现有 Xray 重启机制；最多两跳。Quantumult X 只验证生成格式，原生 iOS 设备未验收；公网 ACME、真实中国 → 首尔 → 美国网络和长期规模性能未在本次验证。

## 2026-11-13 CI 冷启动镜像准备修复

远程 138 项单元测试通过后，新增路由集成因 `--pull=never` 且 runner 没有 Mihomo 镜像而失败，发布流程未进入镜像推送阶段。路由预检现先检查本地镜像，缺失时限时拉取固定 digest；五种原生解析仍使用 `--network none` 和 `--pull=never`，不依赖预热缓存。

## 2026-11-13 首尔路由 / DNS 对齐

本次对齐涉及源码、测试和文档，验证阶段未部署真实服务器。在线只读对比确认七个规则集的匹配内容一致（direct 有重复的 .cn 条目）；修正的是模板的顺序、内联兜底与 DNS 策略。测试使用虚拟身份，不保存真实凭据。

- 新增无真实身份的 `tests/fixtures/seoul-routing.json`，包含首尔有序规则和 DNS 基线，以及刻意重叠的合成源样本。六项回归验证 Mihomo/QX 规则匹配、强制代理/节点引导/私网优先级、七源 provider、旧九个 classical 资源、DNS 策略与动态部署身份；将改前的 model/render 放入临时包运行，六项测试均能检出旧模板问题（含子用例共 17 处断言失败）。
- 重建本地工具镜像后，Linux 容器内完整 `unittest discover -s /tests -v` **138 项全部通过**（235.939 秒，无跳过）；`git diff --check` 通过。安装器测试按要求在 Linux 容器运行，macOS 宿主直跑会触发平台限制和临时目录符号链接保护。
- `python3 tests/integration/routing.py` 通过 Mihomo **v1.19.15** 原生解析，覆盖域名、IPv4、IPv6、零就绪节点，以及旧九个 classical provider 缓存配置。容器 `--network none`，本地预置 provider，不请求订阅或公共 DNS。旧 lancidr payload 中的 `no-resolve` 与新 RULE-SET 参数均被原生解析器接受。
- 完整 `python3 tests/integration/run.py` 通过，包含上述五个配置、native Caddy/安装器预检、精确资源路由、可信测试 TLS、单/双节点 VLESS/WSS/TLS 出站、撤销/离线租约及规则更新隔离；临时容器/网络自动清理。
- QX 官方 sample.conf、filter.snippet、no-fake-ip.conf 和 README 未提供 IP `no-resolve` 参数；模板保留官方原生 IP 三列格式及已支持 DNS 字段，不宣称 QX 与 Mihomo 的 DNS 行为完全等价。

DNS 策略已做确定性配置回归和 Mihomo 原生解析；未对公共 DoH 做在线运行验收。QX 原生 iOS 导入和真实运营商环境仍待设备验证，导入/刷新说明见 [CLIENTS.md](CLIENTS.md)。

v1.0.0 为全新首版多机订阅；下方 0.1.x 内容保留为历史验证记录，不代表迁移支持。

2026-11-04，本地 macOS / Docker Desktop arm64 已通过：

- 61 项单元和回归检查，覆盖一次性加入码、过期/重放、独立节点认证、双协议订阅渲染、用户同步与撤销、并发规则刷新、启动失败和离线租约。
- 订阅中心加两台独立节点的真实 Docker 集成。Mihomo 分别经过两台节点完成 HTTPS/WSS/VLESS TCP 出站，模拟 origin 观察到对应节点的出站 IP。
- owner 与两位朋友的独立授权；撤销一位朋友后，两台节点均拒绝旧 UUID，owner 和另一位朋友正常；其他用户订阅链接保持不变。
- 中心离线、中心请求阻塞、节点启动失败、恢复和移除。独立看门狗在租期内终止实际 Xray 子进程；未成功启动的节点不进入订阅。
- 原有精确 HTTP 路由、可信测试 CA、IPv4/IPv6 SAN、无 SNI、错误证书拒绝、原生 Caddy/Xray/Mihomo 配置检查，以及只读模型 updater 隔离。

测试只使用唯一临时容器/网络及回环随机高端口，结束自动清理；未连接或修改真实服务器。测试 CA 与仅允许模拟 origin 的出站配置均限于临时测试环境。Linux amd64、公开镜像拉取及最终源代码 revision 的结果以 v1.0.0 发布工作流和 registry 核验记录为准。

公共 ACME 实际签发/续期、原生 iOS Quantumult X 导入、真实 VPS 与运营商网络仍需实际环境验收。

## 0.1.x 历史验证

### v0.1.0 本地验证记录

日期：2026-11-04。验证环境为 macOS 上 Docker Desktop，容器 linux/arm64；没有连接服务器、部署、提交、推送或发布。测试数据均为临时生成的虚拟凭据。

| 项目 | 结果 |
| --- | --- |
| Docker 工具镜像构建 | 通过，固定 Python index digest，PyYAML 6.0.3 |
| Python wheel 构建、安装、bootstrap 包数据 | 通过 |
| 离线 unittest | **21 项通过** |
| 两种 Compose config | 通过；外部模式仅发布回环 18090，无 Xray 发布端口 |
| 容器配置隔离 | 通过；updater 模型只读，无 runtime/docker.sock 挂载 |
| Xray 26.7.11 `run -test` | 生成配置原生验证通过，包括非 root 读取权限 |
| Caddy 2.11.4 `validate` | 独立和外部入口配置均通过 |
| Mihomo v1.19.15 `-t` | 生成配置及本地缓存 providers 离线验证通过 |
| HTTP GET/HEAD、MIME、no-store、内容哈希 | 通过；实际 Caddy 提供资源 |
| 未知路径/token、目录、原始 URI 穿越、额外 query、POST | 均拒绝 |
| TLS | 实际握手及 HTTPS 下载通过；测试 CA 信任链和 hostname 校验开启 |
| VLESS + WSS + TLS TCP 出站 | owner、朋友均通过，目标为隔离的模拟 HTTP 服务 |
| 独立撤销 | 订阅立即 404；重启 Xray 后旧 UUID 拒绝，owner/其他朋友正常 |
| 更新器只读模型、无 Xray 配置挂载下刷新 | 通过；使用离线模拟上游数据 |
| shell 语法、Python 语法、空白检查 | 通过 |

单元测试覆盖初始化幂等与设置变更拒绝、独立凭据/撤销、发布哈希、阶段及部分写入失败回退、上游失败保留最后好版本、相对缩减拒绝、跨进程 flock、停止信号、规则优先级/IPv4/IPv6/通配符转换、BOM、Quantumult X 区段/引用、注入/不安全 YAML/symlink 拒绝，以及发布切换期间 health 的重试。

集成脚本 `python3 tests/integration/run.py` 使用独立命名网络和临时目录，只绑定 `127.0.0.1` 上随机高端口；结束自动删除其测试容器/网络。Xray 默认拦截私有出站目标，因此只在另一份临时测试配置中精确允许模拟服务 IP 的 TCP 8000，生成的生产配置不变。测试不访问真实服务器、公共 ACME 或实时 GitHub 规则源。

复现命令见 [README](../README.md#测试与验收)。

Linux amd64 CI 曾在 init 阶段出现 `PermissionError`：非 root runner 创建的临时挂载属于 UID 1001，移除全部 capability 的容器 root 不能修改其权限；宿主机 Python 也不能读取随后生成的 root 所有 0700 模型目录。macOS 文件共享掩盖了这一差异。CI/release 的集成命令已改为 `sudo python3 tests/integration/run.py`，脚本在 Linux 非 root 启动时提前解释要求；生产 capability 和目录权限保持不变。修正后的 Linux amd64 CI 与多架构发布已通过，见下方发布记录。

## 尚未验证

- iOS Quantumult X 原生节点/完整配置导入、实际设备上的规则刷新和匹配。结构校验和服务端连通均不能替代设备验收。
- 公网 ACME 证书签发、真实 DNS、防火墙、用户运营商可达性。
- 目标 Debian/Ubuntu VPS 上的安装和重启恢复；Linux amd64 已通过 GitHub Actions 实际运行测试。
- 完整上游数据的规模性能和长时间运行尚未进行基准验证；一次真实下载与更新结果见下方父级补充。自动化测试仍使用离线数据，不把小型 bootstrap 称作完整规则库。
- GHCR 公开可见性：镜像已上传并验证，包仍为 private，公开前不能匿名拉取。

以上边界与后续客户端验收见 [CLIENTS.md](CLIENTS.md)。

## 独立审查与实际 Compose 验证补充

以下项目另行完成验证，最终打包调整未重跑这些测试：

- 独立审查及生产 Compose 冒烟测试通过。
- 真实上游规则更新通过，条目数为 private **132**、apple **165**、icloud **52**、proxy **27337**、direct **110189**、cncidr **9662**、lancidr **28**。
- 使用生产 Compose 的实际挂载布局验证通过；实际执行 `proxyctl user add/revoke` 成功，Xray 按预期重启，已撤销订阅 URL 返回 **404**。
- 全部临时测试数据已清理。

此补充不代表 iOS Quantumult X 原生导入、公网 ACME 或用户外部网络可达性已经验收，上述限制继续保留。

## v0.1.0 发布验证补充

- 私有源码 revision `c404240e21e5d292589b0d45c06cb32a77ca2b83` 已推送。
- [Linux amd64 CI](https://github.com/silaswei/proxy-stack/actions/runs/41788091162) 与 [双架构发布](https://github.com/silaswei/proxy-stack/actions/runs/41788094510) 均通过。
- GHCR `0.1.0` 包含 linux/amd64 和 linux/arm64；实际认证拉取及运行 `--version` 通过。index digest：`sha256:52613486431f29b7c24d4f8601bb9c75f60bb86d05fe42102b4cb2bce20b65e2`。
- 未部署或更改首尔服务器。

## v0.1.1 安装向导验证

- 原有21项与新增10项测试均通过，包含管道脚本通过 `/dev/tty` 问答、菜单添加/撤销、非法输入、端口冲突、重复运行及符号链接拒绝。
- arm64 原生配置、HTTPS、VLESS 出站与撤销集成通过；两种 Compose、shell 语法和 wheel 内模板打包均通过。
- 独立使用镜像导出的实际脚本在临时目录完成初始化、Compose 配置验证及重跑，凭据保持不变；未启动真实服务器服务。
- 新安装持久化独立 Compose 项目名，受管包装器清除继承的项目配置变量，避免操作另一目录的服务。
- Linux amd64 和0.1.1发布结果见 GitHub Actions；公网 ACME、外部可达性和Quantumult X iOS仍需实机验收。


## v0.1.2 公网 IP 和宿主机名默认值验证

2026-11-04，本地 macOS Docker Desktop / linux arm64；未连接真实服务器，未提交、发布、部署或修改已有服务。

- **40 项离线单元测试通过**：保留原有 31 项覆盖，新增默认值、两次回车、探测失败及显式离线覆盖、宿主机名清理、严格公网 IP/注入拒绝、IPv4/IPv6 客户端与引导规则测试。固定 HTTPS 探测有响应大小、单次请求和总时限，禁止重定向和环境代理。
- 固定 Caddy 2.11.4 对 IPv4/IPv6 独立配置及主机片段执行原生 `adapt` 和 `validate`，确认显式 Let’s Encrypt ACME `shortlived` profile、无内部签发器，且保留 `default_sni`；外部网关仍为纯 HTTP。这些检查禁用容器网络，不发公网 ACME 请求。
- 实际 Docker 集成通过：两种 Compose 布局和权限隔离、Mihomo/Xray/Caddy 原生配置、HTTP 路由拒绝、域名 TLS/VLESS、独立撤销、updater 离线刷新，以及 IPv4/IPv6 SAN 的可信测试 CA 链校验、HTTPS 下载与真实 WSS/VLESS TCP 出站。TLS ClientHello 日志确认 IP 客户端没有发送 SNI；错误 IP SAN、不可信 CA 均被拒绝。测试连接始终为回环随机高端口，没有绑定宿主机 80/443。
- 仅在独立测试运行配置中将已检查的 ACME 签发器替换为测试 CA；生产配置不变，未安装测试 CA 到系统/客户端。测试等待可验证握手，避免把 CA 文件创建误认为站点证书已经就绪。
- 工具镜像构建、wheel 构建/安装、版本和模板/bootstrap/discovery 包数据检查通过；更新渲染哈希。shell/Python 语法及 `git diff --check` 通过。临时测试容器、网络和数据自动清理。

公网 IP 的真实 ACME 签发/续期、真实服务器 IPv6 入站、Quantumult X iOS 原生导入仍待实机验收；本次未运行 Linux amd64 CI 或发布工作流。安装器明确区分证书待签发和就绪，IP 无需域名/DNS，但公网 80/443 必须可达并持续支持续期。GHCR 包可见性未更改。

## v0.1.2 独立安装检查补充

- 实际导出的安装脚本在临时物理目录中省略地址和名称，以非交互 prepare-only 模式初始化成功；模型地址确认为全局公网 IP、名称确认为宿主机 hostname，重跑后模型凭据保持不变。只探测回环18090，没有启动真实服务；临时项目网络和数据已清理。
- 公网 IP 探测实际运行成功，约1.1秒；主机名显式传入工具后输出正确。
- GHCR 包可见性已由用户改为 Public；0.1.1已通过匿名 manifest HTTP200 和空白 Docker 认证配置下的实际拉取/运行验证，旧401问题解除。0.1.2发布结果以 GitHub Actions 和 registry 核验记录为准。
