# 客户端导入与待完成验收

## Mihomo / Clash Meta

将 `./proxyctl links [--user alice]` 输出的 `/<token>/clash` URL 导入支持 VLESS、WS、TLS、classical rule-provider 的 Mihomo 客户端。不是泛称支持旧 Clash。每个节点使用自身地址与 WS Host；域名节点使用自身 servername，纯 IP 节点使用 IP SAN 验证，不发送域名 SNI。不要关闭 TLS 验证。客户端要能直连域名解析及订阅中心的规则镜像。

启动后确认 VPS 策略可选择每个已就绪节点、七个源规则资源刷新成功。测试私网/Apple/国内直连，Google 等已知代理域名和 `api.ip.sb`、`ipapi.co`、`api.ipapi.is`、`ipwho.is` 走 VPS。使用其他网络或外部设备复核，服务器自测不能代表用户运营商可达。

完整订阅采用首尔基线的顺序（首尔的 Proxy 策略在本项目仍叫 `VPS`）：四个 IP 查询域名 → 本部署中心/各节点地址 → localhost/`.local`/`.lan` → 八条内联私网 IP → private → lancidr → iCloud → Apple → proxy → `.cn` → direct → cncidr → 默认 VPS。八条内联 IP 为 `127.0.0.0/8`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`100.64.0.0/10`、`::1/128`、`fc00::/7`、`fe80::/10`。即使规则源的 LAN 内容变化，这些私网兜底仍在完整配置内。Mihomo 的内联 IP、节点 IP 和 lancidr 使用 `no-resolve`；cncidr 保留解析能力。已知代理规则优先于 `.cn` 与 direct。

Mihomo DNS 同步基线：启用 fake-ip（`198.18.0.1/16`）、关闭 IPv6 DNS、启用 `respect-rules`。引导解析器为 `223.5.5.5`、`119.29.29.29`，常规和代理节点解析使用阿里/腾讯国内 DoH；private/direct/apple/icloud 与 `.cn` 使用国内 DoH，proxy 使用 `https://1.1.1.1/dns-query` 和 `https://8.8.8.8/dns-query`。中心和全部启用节点（包括尚未就绪节点）的域名优先使用国内 DoH，避免解析代理节点时递归依赖代理。fake-ip 排除 `*.lan`、`*.local`、`localhost`、`+.msftconnecttest.com`、`+.msftncsi.com` 及本部署中心/节点域名和 IP。

升级已有客户端时，要刷新或重新导入**完整配置订阅**，再确认客户端采用订阅中的 DNS 与规则顺序；仅刷新 provider 不会更新客户端缓存的完整模板。部署自己的域名/IP、端口、UUID、WS 路径及节点名称继续来自本部署，不复制首尔的身份。旧九个规则地址继续提供 classical YAML，包括 force-proxy/bootstrap；新完整配置仅引用七个源资源，旧地址的 behavior 不变。旧 direct 资源继续包含 `.cn`，旧 bootstrap 增加私网兜底，旧 lancidr 的 IP 行增加 `no-resolve`。

## Quantumult X — iOS 原生验收待完成

- `/<token>/quantumult-x/nodes`：导入“服务器资源”，是原生 `vless=… method=none … obfs=wss` 行，不需要第三方 resource parser。
- `/<token>/quantumult-x/rules-portable`：独立导入“分流资源”（`filter_remote`），仅使用内置 `proxy/direct`，无需自建 VPS 组；代理节点由客户端选择。不能用 force-policy 把混合规则全部覆盖为一个策略。
- `/<token>/quantumult-x/rules`：完整配置配套的受管规则，依赖该配置定义的 VPS 策略。保留此链接与旧 `qx/rules.list` 的语义。规则顺序与独立版本一致，仅代理策略名称不同。
- `/<token>/quantumult-x`：完整配置，含节点资源、规则资源和独立 `VPS` static 策略，包含全部已就绪节点的 tag。完整导入可能替换已有配置，应先备份客户端现有配置。

完整配置、受管规则和独立规则采用上述相同的有序匹配条件；完整配置还在 `[filter_local]` 内联四个 IP 查询域名、中心/节点地址、localhost/LAN 和八条私网规则，远程规则不可用时仍有这些保护及 `final, VPS`。节点资源只提供节点，单独导入节点不会改变用户自己的分流和 DNS。独立分流资源也不修改已有 DNS 或最终策略；需在自己的 `[filter_local]` 保留所需的 `final, proxy`，并避免现有高优先级规则覆盖本资源。

QX 使用官方三列 `ip-cidr` / `ip6-cidr` 语法。2026-11-13 检查官方 sample.conf、filter.snippet、no-fake-ip.conf 和 README，未找到 IP 规则的 `no-resolve` 参数，因此不输出 Mihomo 的第四列，也不把 Mihomo `nameserver-policy` 搬入 QX。QX 完整模板继续使用官方支持的 `no-ipv6` 与国内 DNS。这里对齐的是规则顺序和匹配条件；Mihomo 的 DNS 选择与抑制 IP 规则主动解析的效果不能据此宣称在 QX 完全等价，仍需 iOS 原生验收。

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

独立规则不能使用 Mihomo 的 `rules/<name>` YAML，也不能将完整配置 URL 当作 `filter_remote`。新增独立资源修正了自定义策略未声明时的导入兼容问题；尚未通过原生 QX 复现或确认用户所报“格式错误”的具体根因。
