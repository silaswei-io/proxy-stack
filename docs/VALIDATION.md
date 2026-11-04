# v0.1.0 本地验证记录

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

## 尚未验证

- iOS QX 原生节点/完整配置导入、实际设备上的规则刷新和匹配。结构校验和服务端连通均不能替代设备验收。
- 公网 ACME 证书签发、真实 DNS、防火墙、用户运营商可达性。
- linux/amd64 实际运行、目标 Debian/Ubuntu VPS 上的安装和重启恢复。
- 完整上游数据的规模性能和长时间运行尚未进行基准验证；一次真实下载与更新结果见下方父级补充。自动化测试仍使用离线数据，不把小型 bootstrap 称作完整规则库。
- GHCR release 工作流的实际发布结果与包可见性；由仓库所有者执行并确认。

以上边界与后续客户端验收见 [CLIENTS.md](CLIENTS.md)。

## 独立审查与实际 Compose 验证补充

以下项目另行完成验证，最终打包调整未重跑这些测试：

- 独立审查及生产 Compose 冒烟测试通过。
- 真实上游规则更新通过，条目数为 private **132**、apple **165**、icloud **52**、proxy **27337**、direct **110189**、cncidr **9662**、lancidr **28**。
- 使用生产 Compose 的实际挂载布局验证通过；实际执行 `proxyctl user add/revoke` 成功，Xray 按预期重启，已撤销订阅 URL 返回 **404**。
- 全部临时测试数据已清理。

此补充不代表 iOS QX 原生导入、公网 ACME 或用户外部网络可达性已经验收，上述限制继续保留。
