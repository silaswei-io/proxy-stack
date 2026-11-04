# v1.0.0 验证记录

v1.0.0 为全新首版多机订阅；下方 0.1.x 内容保留为历史验证记录，不代表迁移支持。

2026-11-04，本地 macOS / Docker Desktop arm64 已通过：

- 61 项单元和回归检查，覆盖一次性加入码、过期/重放、独立节点认证、双协议订阅渲染、用户同步与撤销、并发规则刷新、启动失败和离线租约。
- 订阅中心加两台独立节点的真实 Docker 集成。Mihomo 分别经过两台节点完成 HTTPS/WSS/VLESS TCP 出站，模拟 origin 观察到对应节点的出站 IP。
- owner 与两位朋友的独立授权；撤销一位朋友后，两台节点均拒绝旧 UUID，owner 和另一位朋友正常；其他用户订阅链接保持不变。
- 中心离线、中心请求阻塞、节点启动失败、恢复和移除。独立看门狗在租期内终止实际 Xray 子进程；未成功启动的节点不进入订阅。
- 原有精确 HTTP 路由、可信测试 CA、IPv4/IPv6 SAN、无 SNI、错误证书拒绝、原生 Caddy/Xray/Mihomo 配置检查，以及只读模型 updater 隔离。

测试只使用唯一临时容器/网络及回环随机高端口，结束自动清理；未连接或修改真实服务器。测试 CA 与仅允许模拟 origin 的出站配置均限于临时测试环境。Linux amd64、公开镜像拉取及最终源代码 revision 的结果以 v1.0.0 发布工作流和 registry 核验记录为准。

公共 ACME 实际签发/续期、原生 iOS QX 导入、真实 VPS 与运营商网络仍需实际环境验收。

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

单元测试覆盖初始化幂等与设置变更拒绝、独立凭据/撤销、发布哈希、阶段及部分写入失败回退、上游失败保留最后好版本、相对缩减拒绝、跨进程 flock、停止信号、规则优先级/IPv4/IPv6/通配符转换、BOM、QX 区段/引用、注入/不安全 YAML/symlink 拒绝，以及发布切换期间 health 的重试。

集成脚本 `python3 tests/integration/run.py` 使用独立命名网络和临时目录，只绑定 `127.0.0.1` 上随机高端口；结束自动删除其测试容器/网络。Xray 默认拦截私有出站目标，因此只在另一份临时测试配置中精确允许模拟服务 IP 的 TCP 8000，生成的生产配置不变。测试不访问真实服务器、公共 ACME 或实时 GitHub 规则源。

复现命令见 [README](../README.md#测试与验收)。

Linux amd64 CI 曾在 init 阶段出现 `PermissionError`：非 root runner 创建的临时挂载属于 UID 1001，移除全部 capability 的容器 root 不能修改其权限；宿主机 Python 也不能读取随后生成的 root 所有 0700 模型目录。macOS 文件共享掩盖了这一差异。CI/release 的集成命令已改为 `sudo python3 tests/integration/run.py`，脚本在 Linux 非 root 启动时提前解释要求；生产 capability 和目录权限保持不变。修正后的 Linux amd64 CI 与多架构发布已通过，见下方发布记录。

## 尚未验证

- iOS QX 原生节点/完整配置导入、实际设备上的规则刷新和匹配。结构校验和服务端连通均不能替代设备验收。
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

此补充不代表 iOS QX 原生导入、公网 ACME 或用户外部网络可达性已经验收，上述限制继续保留。

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
- Linux amd64 和0.1.1发布结果见 GitHub Actions；公网 ACME、外部可达性和QX iOS仍需实机验收。


## v0.1.2 公网 IP 和宿主机名默认值验证

2026-11-04，本地 macOS Docker Desktop / linux arm64；未连接真实服务器，未提交、发布、部署或修改已有服务。

- **40 项离线单元测试通过**：保留原有 31 项覆盖，新增默认值、两次回车、探测失败及显式离线覆盖、宿主机名清理、严格公网 IP/注入拒绝、IPv4/IPv6 客户端与引导规则测试。固定 HTTPS 探测有响应大小、单次请求和总时限，禁止重定向和环境代理。
- 固定 Caddy 2.11.4 对 IPv4/IPv6 独立配置及主机片段执行原生 `adapt` 和 `validate`，确认显式 Let’s Encrypt ACME `shortlived` profile、无内部签发器，且保留 `default_sni`；外部网关仍为纯 HTTP。这些检查禁用容器网络，不发公网 ACME 请求。
- 实际 Docker 集成通过：两种 Compose 布局和权限隔离、Mihomo/Xray/Caddy 原生配置、HTTP 路由拒绝、域名 TLS/VLESS、独立撤销、updater 离线刷新，以及 IPv4/IPv6 SAN 的可信测试 CA 链校验、HTTPS 下载与真实 WSS/VLESS TCP 出站。TLS ClientHello 日志确认 IP 客户端没有发送 SNI；错误 IP SAN、不可信 CA 均被拒绝。测试连接始终为回环随机高端口，没有绑定宿主机 80/443。
- 仅在独立测试运行配置中将已检查的 ACME 签发器替换为测试 CA；生产配置不变，未安装测试 CA 到系统/客户端。测试等待可验证握手，避免把 CA 文件创建误认为站点证书已经就绪。
- 工具镜像构建、wheel 构建/安装、版本和模板/bootstrap/discovery 包数据检查通过；更新渲染哈希。shell/Python 语法及 `git diff --check` 通过。临时测试容器、网络和数据自动清理。

公网 IP 的真实 ACME 签发/续期、真实服务器 IPv6 入站、QX iOS 原生导入仍待实机验收；本次未运行 Linux amd64 CI 或发布工作流。安装器明确区分证书待签发和就绪，IP 无需域名/DNS，但公网 80/443 必须可达并持续支持续期。GHCR 包可见性未更改。

## v0.1.2 独立安装检查补充

- 实际导出的安装脚本在临时物理目录中省略地址和名称，以非交互 prepare-only 模式初始化成功；模型地址确认为全局公网 IP、名称确认为宿主机 hostname，重跑后模型凭据保持不变。只探测回环18090，没有启动真实服务；临时项目网络和数据已清理。
- 公网 IP 探测实际运行成功，约1.1秒；主机名显式传入工具后输出正确。
- GHCR 包可见性已由用户改为 Public；0.1.1已通过匿名 manifest HTTP200 和空白 Docker 认证配置下的实际拉取/运行验证，旧401问题解除。0.1.2发布结果以 GitHub Actions 和 registry 核验记录为准。
