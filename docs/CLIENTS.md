# 客户端导入与待完成验收

## Mihomo / Clash Meta

将 `./proxyctl links [--user alice]` 输出的 `/<token>/clash` URL 导入支持 VLESS、WS、TLS、classical rule-provider 的 Mihomo 客户端。不是泛称支持旧 Clash。每个节点使用自身地址与 WS Host；域名节点使用自身 servername，纯 IP 节点使用 IP SAN 验证，不发送域名 SNI。不要关闭 TLS 验证。客户端要能直连域名解析及订阅中心的规则镜像。

启动后确认 VPS 策略可选择每个已就绪节点、规则刷新成功。测试私网/Apple/国内直连，Google 等已知代理域名和 `api.ip.sb`、`ipapi.co`、`api.ipapi.is`、`ipwho.is` 走 VPS。使用其他网络或外部设备复核，服务器自测不能代表用户运营商可达。

## Quantumult X — iOS 原生验收待完成

- `/<token>/quantumult-x/nodes`：导入“服务器资源”，是原生 `vless=… method=none … obfs=wss` 行，不需要第三方 resource parser。
- `/<token>/quantumult-x/rules`：导入“分流资源”，依赖名为 VPS 的策略；不能用 force-policy 把所有行覆盖为一个策略。
- `/<token>/quantumult-x`：完整配置，含节点资源、规则资源和独立 `VPS` static 策略，包含全部已就绪节点的 tag。完整导入可能替换已有配置，应先备份客户端现有配置。

模板采用官方 [sample.conf](https://github.com/crossutility/Quantumult-X/blob/master/sample.conf) 展示的区段和字段；本仓库 fixtures 仅用于格式回归，不是成功设备样本。没有 iOS 成功基线或承诺的最低 Quantumult X 版本。此前关于无效配置的区段/测速 URL/策略名调整均未得到设备最终反馈，根因未确定。

发布使用前记录以下结果（不要记录真实 URL、UUID 或 token）：

| 验收项 | 当前状态 |
| --- | --- |
| iOS / Quantumult X 应用版本 | 待设备提供 |
| 原生 VLESS/WSS 节点导入 | 待验证 |
| 完整配置原生解析并启用 | 待验证 |
| 全部节点 tag 与 VPS 策略引用一致 | 已结构测试；待原生解析 |
| 订阅和规则自动刷新 | 已 HTTP 测试；待设备 |
| 四个 IP 查询域名走代理 | 已模型测试；待设备 |
| 私网、Apple/iCloud、国内直连 | 已模型测试；待设备 |
| 外部网络访问及 IPv6 环境 | 待验证；默认禁用 IPv6 DNS |

若有错误，记录应用版本、脱敏错误行和区段、具体导入方式；不要仅凭 HTTP 200 或模板结构声称兼容性已证实。

没有就绪节点时，完整 Mihomo/Quantumult X 配置的默认组选择 REJECT/reject，显式国内、LAN、Apple/iCloud 直连规则保留。Quantumult X 节点资源单独导入时，已有客户端策略由用户管理，不会被资源自动替换。加入节点上的 links 命令提示使用中心链接。

以上 token 为原有 48 位十六进制私密 token。旧 `/sub/<token>/clash.yaml`、`/sub/<token>/qx/nodes.txt`、`/sub/<token>/qx/full.conf`、`/sub/<token>/qx/rules.list` 继续兼容；新配置中嵌入的规则和服务器资源均使用新地址。不要公开任何一种链接。
