# 安装后的独立订阅域名

安装器原本就支持域名和 HTTPS。这里管理的是**已安装中心的另一个订阅入口**，尤其适合已经登记远程节点、原控制入口使用公共 IP 的中心。不要通过修改 `.env` 的 `DOMAIN` 或重跑安装器更换控制地址来实现别名。

## 使用

在当前安装目录操作。需要本机 Docker、Compose ≥ 2.24.4，以及宿主 Python 3.9+；不需要在宿主安装 PyYAML。`proxyctl` 从当前工具镜像导出仅包含包代码的临时 zip helper，再由宿主 Python 执行。只支持一个首选别名，node 角色拒绝此功能。

```sh
./proxyctl domain show                  # list 也可；只显示公开入口和恢复状态
./proxyctl domain add sub.example.com   # 只填 DNS 主机名，默认 HTTPS443
./proxyctl links --labelled
./proxyctl links --user alice --labelled
./proxyctl links --original --labelled  # 保留的原入口，朋友同样支持
./proxyctl verify                       # 校验首选订阅入口
./proxyctl domain remove
./proxyctl links --labelled             # 删除后恢复原入口
./proxyctl domain recover               # 恢复中断/失败的域名事务
```

中文菜单 16–19 分别提供查看、添加、移除和恢复。相同域名/端口重复添加会重新校验 DNS 与 HTTPS，不轮换身份；已有不同别名时明确拒绝，须先移除。`--port 8443` 可指定其他 HTTPS 端口；80 保留给 HTTP-01。无终端可直接使用以上命令。

操作保持 `settings.origin`、原公共端口、owner/朋友 UUID 和 token、WS 路径、节点登记、generation/回执与链路派生身份不变。仅修改独立 `subscription` 配置。新链接、Mihomo provider URL、QX 完整配置内的 server/filter URL 使用首选域名，并加入域名直连规则和 Mihomo DNS/fake-IP 例外。代理服务器地址和节点加入码仍指向原地址。原入口继续提供同一份订阅资源；首选别名启用时，通过原入口获取的完整配置也会引用首选域名。

## DNS、证书和端口

先由用户配置 A/AAAA；不支持 DNS SDK、自动改 DNS、CDN 代理或 DNS API 密钥。复用安装器已有的保守地址证明：所有解析出的公共 A/AAAA 都必须属于独立 IPv4/IPv6 HTTPS 出口探测证明的本机地址。私有 IP、IP 字面量、协议/端口/路径、注入字符、无法证明的额外地址都会拒绝。

公网须能访问别名的 HTTPS 端口；保留原入口及其 HTTP-01 路由。CAA 须允许 Caddy 使用的签发机构。TCP443 空闲时，栈内 Caddy 可通过 TLS-ALPN-01 完成别名签发；如果使用其他 HTTPS 端口，公网80须正确到达为该域名签发证书的 Caddy。已有宿主80仅转发原 IP challenge 时，不能假定别名的高端口 HTTP-01 已配置；无法签发/校验会回滚。工具不改 DNS、防火墙或 NAT，也不修改原安装已有的 TLS 策略。

证书由现有 Caddy 自动签发/续期，沿用其持久证书存储。客户端 HTTPS 和宿主别名的 HTTPS 上游都保持证书验证，不跟随重定向、不使用环境代理。添加前后均校验订阅资源的 GET/HEAD、内容哈希、MIME、no-store/no-referrer/nosniff 和未知路径404。HTTPS 每次检查总上限120秒，包括 DNS、锁竞争和慢响应；等待期间不持有模型操作锁。

## 两种部署方式

**栈内 Caddy：**当目标端口由当前 Caddy 容器发布，或宿主/Docker 探测确认空闲时，添加 `data/runtime/caddy/subscription/alias.Caddyfile`。该站点只提供精确的公开订阅资源，拒绝控制接口、WS、非 GET/HEAD、查询串及不安全路径。需要新端口时生成 `compose.subscription.yaml`，在当前 `.env` 的 `COMPOSE_FILE` 末尾追加它，保留原 HTTPS 与 HTTP-01 绑定。只重建/重启 Caddy，hub/Xray/updater 不重建。Caddy 短暂重启期间其现有连接可能中断。

运行目录中的 `subscription/` 是受管 include 命名空间，只允许 `alias.Caddyfile`；发现其他文件、符号链接、未知修改或不符合生成器的主配置，预检拒绝。完整候选配置连同其唯一允许的 include 用实际 Caddy 镜像在隔离容器中验证。不要把自定义配置放进这个目录。

**宿主 Caddy：**目标 HTTPS 端口被支持的 stock systemd Caddy 占用时，复用 `host_setup` 的程序/unit/PID/监听者、模块、import、磁盘和 admin 运行配置检查。要求 Linux root、`ss`、stock Caddy ≥2.6、标准本机 admin/reload。仅追加带安装身份标记的域名站点，精确资产路径通过验证的 HTTPS 代理回**原入口**；控制和代理传输路径不会经过别名。原 HTTP-01 区块和其他站点保留，程序/unit不升级。文件服务器的用户 hide 配置及 Caddy 自动隐藏源配置文件的路径沿用既有规范化校验。

已有目标域名、通配符/catch-all、未知自定义模块、漂移配置或启用了不能隔离的访问日志时会明确拒绝，不自动覆盖。当前宿主适配保守拒绝带访问日志配置的服务器，避免私密订阅路径进入已有日志。上游错误由本站错误处理器转为404；不会把 token/私密路径写入例行操作输出。

源码安装和受管安装均支持上述生命周期，源码项目需要使用本次代码构建的工具镜像及 `.env`，例如 `docker build -t proxy-stack-tool:v1.0.0 -f manager/Dockerfile .` 后设置 `TOOL_IMAGE=proxy-stack-tool:v1.0.0`。高级 `MODE=existing-caddy` 保持原专家安装角色：别名可以另由栈内 TLS 站点提供，也可以由受支持宿主 Caddy 通过原 HTTPS 入口提供；它不会替代原反代配置。

## 已有中心升级与安装器重跑

先更新为包含本功能的工具镜像和匹配的 wrapper。现有 `upgrade` 只刷新镜像；旧 wrapper 没有 `domain` 命令时，需要使用含本功能的安装器重跑以刷新模板，或源码安装同步匹配的 wrapper。

同版本安装器重跑会保留订阅配置、include 文件和端口 overlay，并把 overlay 写回有效 `COMPOSE_FILE`。原安装角色不变。别名启用期间，重配原域名、端口或部署模式会明确拒绝；先移除别名再按原流程重配。安装与域名操作共享目录锁，未完成域名事务须先 `domain recover`，不要用安装器恢复命令处理域名事务。

## 已有手工宿主别名的迁移

当前版本**不提供自动 adopt**，也不会仅凭域名相同或上游端口相同就宣称拥有原站点。采用以下可审查迁移路径；需要维护者正常的外部变更授权后执行：

1. 运行 `links --original --labelled`，确认原 HTTPS 入口可用，必要时让使用者暂用该入口。保留原 IP/控制地址及原 HTTP-01 区块。
2. 备份宿主实际主 Caddyfile 和相关 import，记录文件哈希；用 `caddy adapt` 与本机 admin 确认运行配置等于磁盘配置。备份应私有保存，不上传 token。
3. 审查并只移除**该确切手工别名站点**，不删除通配符/其他站点、不改原入口、不动 HTTP-01 区块。用原二进制执行 `caddy validate --config /实际/Caddyfile`，验证成功后 `systemctl reload caddy`。再次确认原入口可用。
4. 在中心执行 `./proxyctl domain add sub.example.com`。若宿主仍占用443，将生成托管宿主站点；如果移除旧站点后443空闲，按正常策略转由栈内 Caddy 提供。HTTPS 校验成功后才选为首选入口。
5. 如果新别名添加失败，工具回滚自己的变化。人工确认备份后未发生其他配置变更，再恢复原手工站点并 native validate/reload；若有并发变更，逐段合并，不能整文件盲目覆盖。

这样允许维护者迁移已有的「域名443 → 原公共 HTTPS 高端口」别名，同时保留未知站点的所有权边界。

## 事务、回滚和移除

持久记录位于 `data/domain-transaction/`，目录/文件为私有权限，写入意图并 fsync 后才修改部署文件；宿主变更另用 `host_setup` 原生日志。成功后删除事务记录。进程中断或机器重启后使用 `domain recover`，默认恢复操作前状态；Caddy 停止或被删除时也可重建原 Caddy 服务。

恢复先逐个校验自身文件变更，拒绝覆盖不同于事务前/后值的配置；还会校验恢复后的 Compose 挂载和持久身份。首选项回滚基于最新模型，仅恢复自己的字段，保留期间新增的朋友、节点、链路和规则更新。无关 dotenv 编辑保留；涉及同一 `COMPOSE_FILE`、受管 include、宿主 import/unit/admin 或相同文件内容的冲突会保留事务和锁供人工审查，再执行恢复。不要删除锁/日志来强行跳过冲突。

移除时验证受管区块/文件是否仍符合预期，只删除本次管理的别名和新增端口绑定，确认原入口可用后恢复原地址链接。原证书卷保留，其他站点不删除。移除前应让客户端刷新到原入口；已缓存的域名资源 URL 不会被远程自动改写。回退到不含本功能的镜像/代码前，也应先执行 `domain remove` 并验证原链接，再回退版本，避免旧生成器遗漏受管 include。

## 验证边界

自动测试使用合成身份、隔离 Caddy/internal CA、随机回环高端口与私有临时目录；不访问真实服务器，不在宿主绑定80/443。覆盖别名生命周期、原入口/朋友资源、上游和外层 TLS 验证、GET/HEAD/严格404、重启证书保留、失败回滚、停止后的恢复、并发模型保留和模板重跑。

公共 CA 的真实签发/续期、防火墙外部可达性和 iOS Quantumult X 原生导入需要维护者在授权环境验收；本次没有声称完成这些验证。
