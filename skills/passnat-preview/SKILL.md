---
name: passnat-preview
description: 使用 PassNAT CLI 分享本机服务的 HTTPS 预览，处理共享隧道、独立预览、静态 HTML 白名单、状态检查与安全回收。
compatibility: Python 3.11+、PassNAT CLI、frpc、受管 Caddy；Windows amd64 与 macOS Apple Silicon（arm64）；Windows 支持 DPAPI 凭证保存。
---

# PassNAT HTTPS 预览

先检查 `passnat --help`，再读取相关资源的 `--help`。未安装 CLI 时，按项目 README 安装；不要假定用户的仓库位置、账号、节点 ID 或域名 ID。frpc / Caddy 路径按下一节探测。

## 本机程序路径（macOS 优先）

`find_frpc()` 的解析顺序：`--frpc` > `PASSNAT_FRPC` > `command -v frpc`（PATH）> macOS 应用包。PassNAT 桌面客户端自带 frpc，但不在 PATH 上，必须检查：

- `/Applications/PassNAT.app/Contents/MacOS/frpc`
- `~/Applications/PassNAT.app/Contents/MacOS/frpc`

核对顺序：`command -v frpc`；`test -x <候选路径>` 确认文件存在且可执行；必要时 `<路径> --version`。Caddy 不在应用包内。macOS 运行 `passnat tools install-caddy` 安装受管的官方 v2.11.9（仅 Apple Silicon / arm64）：命令校验固定 SHA-256 后只读取归档中的常规 `caddy` 成员，写入 `<PASSNAT_HOME>/bin/caddy` 并赋予可执行权限。重复执行会复用摘要一致的已有文件；内容不同或符号链接直接拒绝。macOS 上 CLI **只认这个受管文件**，不回退 Homebrew、`PASSNAT_CADDY` 或 PATH。Windows 仍按 `PASSNAT_CADDY` > `<PASSNAT_HOME>\bin\caddy.exe` > `bin\caddy` > PATH 查找，也可用同一命令安装官方 v2.11.9。本机状态目录（macOS 默认 `~/.local/share/PassNatCLI/`）中的 `shared-<名称>.json` 会记录实际使用的 `frpc` / `caddy` 路径，可用来核对现场。

macOS 只有一种 Caddy 生命周期：**CLI 托管**，不再使用 Homebrew 服务或 LaunchAgent。

- 单次预览：`passnat preview up` 之后由 `passnat preview run NAME proxy` 前台启动专属 Caddy 实例。
- 多应用共享：`passnat shared init` 建一个底座，逐个 `passnat shared site` 加站点，再由 `passnat shared run NAME proxy` 启动一个 Caddy、`passnat shared run NAME frpc` 启动一个 frpc；站点变更用 `passnat shared sync NAME --reload --apply` 热加载。
- 两种用法都会占用公网 80/443，不能同时运行；切换前先确认旧实例已停止。不要建议用户 `brew install caddy`、`brew services start caddy` 或手工维护 `/opt/homebrew/etc/Caddyfile`。

Caddy 的配置与监听由 CLI 生成并启动；用户不需要事先已有公信证书，DNS 与挑战路径可达后由 Caddy 走 ACME 申请。CLI 的 dry run 只证明映射 payload，不证明本地端口真的讲 TLS、Caddy 正在监听或 ACME 挑战流量能穿过 frpc。

## 分流决策（先判断，再创建资源）

场景不确定时，先运行本地规划器；它不读凭证、不联网、不写状态，也不替用户猜设备：

```sh
passnat plan --device same --protocol https --apps 2
```

| 场景 | 正确路径 | 禁止 |
| :--- | :--- | :--- |
| 同一设备、多个 HTTPS 应用 | 复用一套共享底座：首条映射 `tunnels create --caddy-tls-port`，之后 `shared site` 登记 + `shared sync --reload --apply` | 为第二个应用再建隧道或第二个 frpc |
| 同一设备、单个 HTTPS 应用 | 独立预览：`tunnels create --port-name <NAME>-tcp --caddy-tls-port 443` → `preview up NAME --public-port 443` → `preview run NAME proxy` / `frpc`（直接 `tunnels create` 仅用于已有自管 TLS 监听的高级场景） | 自动套用共享复用 |
| HTTP / TCP / UDP | 按协议单独建隧道/映射（`--protocol http` / `--protocol tcp`） | 进入 Caddy HTTPS 共享流程 |
| 不同设备 | 目标设备独立建隧道与独立 frpc 身份 | 复用同一隧道 ID、frpc 配置或进程身份 |

- **UDP 当前不支持**：CLI 只实现 `http`、`https`、`tcp`；`plan` / `ports add` / `tunnels create` 收到 `udp` 会在读凭证前返回 `unsupported_protocol`（退出码 3）。不要用 shared/preview 假装支持，也不要静默降级成 TCP。
- **`shared` 只承载 HTTPS 发布**：`shared site --upstream-port` 里的应用仍是普通 HTTP 上游，TLS 由 Caddy 终结；对外发布 HTTP 或 TCP 时不要走共享底座。
- **同一本机端口号不算冲突**：归属按登记名 `port_name` 判定，多个站点可以共用同一个本机 TLS 端口。

## 用户报告「frpc 未找到」时

先核实，再下结论：

1. 检查上述 macOS 应用包路径是否存在且可执行，不要只依赖 PATH 就判定缺失。
2. 读取 `shared-<名称>.json` 的 `frpc` / `caddy` 字段；如果记录的路径存在且可执行，本机二进制就没有问题。
3. 区分远端与本地：`shared status` 的 `connected=false`（远端 tunnel `status: 2`、`tun_conns_info: null`）只表示当前没有在线客户端连接，不等价于本机 frpc 缺失。不要把远端未连接误判成路径问题。
4. 未获用户明确授权时，不启动 frpc、不重启隧道、不改写配置；状态文件与生成的 frpc 配置含平台凭据，只读取程序路径字段，不输出、不复制密钥或完整配置。

## 操作流程

1. 识别本机 HTTP 应用端口、域名和公开范围。公网发布必须获得用户授权；不得直接暴露仓库、配置目录、管理 API、目录列表或私密 HTML。
2. 使用 `passnat auth check` 验证凭证。macOS 用 `passnat auth login --stdin` 将凭证保存到钥匙串；不要读取、打印、复制或写入真实密钥到参数、技能、日志或公开文件。
3. 先确认运行模式。macOS 上只有 CLI 托管模式：用 `passnat tools install-caddy` 准备受管 Caddy，再由 `shared run NAME proxy` / `preview run NAME proxy` 启动，站点变更走 `shared sync --reload --apply`。不要引导用户安装 Homebrew Caddy、编辑 `/opt/homebrew/etc/Caddyfile` 或执行 `caddy validate` / `caddy reload`。**启动顺序固定为：先运行 proxy，确认 Caddy 的 TLS 端口已监听，再运行 frpc；两条命令在两个前台终端中保持运行。** `shared run NAME frpc` 只负责隧道客户端，不会自动拉起 Caddy；frpc 出现 `connect to local service 127.0.0.1:PORT ... connection refused` 时，先补启动/检查 proxy，不重建隧道。独立预览与共享底座不可同时占用 80/443。
4. 核对 PassNAT 现有隧道/映射归属和端口，再决定创建什么资源；不要因本机 Caddy 已运行就假设公网请求已转发到本机。若已有错误映射（例如旧命令省略协议、把应用 HTTP 端口创建成首个 HTTP 映射），先停下确认它属于哪条隧道、哪个代理名/ID，向用户说明并取得授权后再用 `passnat ports delete NAME` 预演并处理；不要自动替换、掩盖或假定 CLI 会清理。
5. 一条 PassNAT 隧道只运行一个 frpc。多应用共享时，PassNAT 映射需把公开 HTTPS 443 转发到本机 Caddy 的 TLS 监听端口；域名 DNS/平台域名绑定必须一致。创建该映射优先用 `passnat tunnels create ... --caddy-tls-port PORT`：它固定 `local_port=PORT`、`protocol=https`、`remote_port=443`，无 HTTP 回退，且与 `--local-port`/`--protocol`/`--local-tls` 互斥。这里的 PORT 是 Caddy 自己的 TLS 端口（`shared init` 默认 `18446`），**不是** `shared init --tls-port` 那个初始化参数，也不是应用的 HTTP 端口——应用 HTTP 端口通过 `shared site --upstream-port` 登记，由 Caddy 反代。
6. 证书签发前确认挑战路径：TLS-ALPN-01 需要 Caddy 实例收到域名的公网 TCP 443 握手；HTTP-01 需要公网 TCP 80 的 `/.well-known/acme-challenge/` 请求到达该实例。**当前 CLI 生成的 PassNAT 映射/配置并不自动证明 ACME 挑战流量能穿过 frpc 到达 Caddy**；PassNAT 节点/映射必须确实转发相应挑战流量。仅配置 Caddy `http_port` / `https_port` 或本地开放端口不等于公网挑战已通。先用 dry run 和节点支持文档核对；不确定时停止，不要宣称证书流程就绪，也不要盲目重复创建映射。
7. **`--protocol https` 表示本地映射端口本身提供 TLS**。普通 HTTP 应用不能直接声明为 HTTPS；共享/预览模式下由 CLI 托管的 Caddy 终结 TLS，PassNAT HTTPS 映射指向本机 Caddy TLS 端口，Caddy 再反代到应用 HTTP 端口。直接 `ports add` / `tunnels create --protocol https` 需要 `--local-tls`，仅在本地端口确实讲 TLS 时使用；优先走已验证的 shared/preview 工作流，不拿这个标志绕过协议校验。`tunnels create` 必须显式给出 `--protocol`，遗漏会以 `protocol_required`（退出码 3）在读取凭证前拒绝，不再默认写 HTTP；目标是受管 Caddy 的 TLS 端口时直接用 `--caddy-tls-port PORT`，不要靠缺省值或事后猜映射。
8. 证书、frpc 配置、Caddy 管理 API 与密钥保持私有。验证顺序分开：Caddy 配置有效 → 本地域名路由/应用响应 → frpc 远端在线 → 公网 DNS/SNI/可信证书 → 页面业务行为。`status: 2` 只表示远端无在线客户端，不表示本机二进制缺失。

## 已验证的 macOS 共享 HTTPS 闭环

对本机 HTTP 应用（例如 `127.0.0.1:5000`），按以下顺序执行；不要把应用端口直接声明成 HTTPS：

```sh
# 1. 安装并核对受管官方 Caddy（Apple Silicon，固定 v2.11.9）
passnat tools install-caddy
passnat auth check

# 2. 首条映射必须明确指向 Caddy TLS 监听端口；先看预演
passnat tunnels create NAME --node-id NODE_ID --port-name PORT_NAME \
  --caddy-tls-port 18446 --subdomain BASE_SUBDOMAIN --domain-id DOMAIN_ID
passnat tunnels create NAME --node-id NODE_ID --port-name PORT_NAME \
  --caddy-tls-port 18446 --subdomain BASE_SUBDOMAIN --domain-id DOMAIN_ID --apply

# 3. 初始化共享底座、登记 HTTP 应用、生成并校验 Caddy/frpc 配置
passnat shared init NAME --tunnel TUNNEL_ID --tls-port 18446 --apply
passnat shared site NAME app --subdomain APP_SUBDOMAIN --domain-id DOMAIN_ID \
  --upstream-port 5000 --apply
passnat shared sync NAME --apply
```

然后保持两个前台终端持续运行，**先 Caddy、后 frpc**：

```sh
# 终端 A：先启动 Caddy；确认 18446 已 LISTEN 后再开终端 B
passnat shared run NAME proxy

# 终端 B：再启动唯一的 frpc 客户端
passnat shared run NAME frpc
```

验证必须分三层：

```sh
# 本机 Caddy/SNI 路由（域名必须保留，不能只打开 127.0.0.1）
curl --resolve APP_SUBDOMAIN.DOMAIN:18446:127.0.0.1 \
  https://APP_SUBDOMAIN.DOMAIN:18446/

# PassNAT 远端状态和公网 TLS/应用响应
passnat shared status NAME
curl -fsS https://APP_SUBDOMAIN.DOMAIN/
```

交付前应同时看到：Caddy TLS 端口在监听、`connected: true`、`tls_verified: true`，以及公网首页的实际 HTTP 状态（通常为 `200`；若应用设计为跳转登录页，则记录实际 `3xx`）。`frpc` 报 `connect to local service 127.0.0.1:18446 ... connection refused` 时，说明 Caddy 没在监听或已退出；先检查/重启 `shared run NAME proxy`，不要重建隧道。上次实测中 `localhost:5000` 在 macOS 解析到 `::1` 并命中 AirTunes 返回 `403`，因此 Caddy 上游必须使用 `127.0.0.1:5000`。

## 远端已删除，但本地状态仍占用名称

1. **分清是哪条记录。** 创建预检会先检查本地端口登记名，`state_conflict` 可能表示该名称属于另一隧道，不代表本机监听端口号冲突。`tunnels create` 只有在创建参数、远端身份和初始映射归属一致时才复用 `create-<隧道名>.json` 的登记；`reconciliation_required` 表示需要对账，不代表远端已删除。`shared init` 也可能因 `shared-<底座名>.json` 仍绑定旧 ID 返回 `state_conflict`。以实际 `PASSNAT_HOME` 或平台默认目录为准，只核对名称、旧 ID 和归属，不输出完整状态或配置。
2. **按旧 ID 核对远端。** 用同一账号执行 `passnat tunnels get --tunnel <旧ID>`，只把平台明确返回旧 ID 不存在，或管理界面明确确认该 ID 已删除，作为删除证据。离线、列表中没看到、鉴权失败、超时和含义不明的 API 错误都不算。遇到同名新隧道，按 ID 区分；创建结果不确定时，先对账，不清记录重试。
3. **按用户要求直接清理已确认的孤儿状态。** 这些 CLI 状态/导出文件不是项目源码；若用户明确确认远端已删除并授权清理，可直接删除逐条验证为旧 ID 所属、且会阻塞重建的本地记录，不必强制归档。先确认该实例进程已停止，按内容中的 `tunnel_id` 精确匹配 `create-*.json`、`port-*.json`、`shared-*.json`；导出的 TOML 没有 tunnel_id 时，需用其中代理 UUID 与已核实的旧端口记录对应。只删除明确匹配项，不用宽泛通配符；不碰其他 ID、锁、证书、凭证或运行配置。若用户没授权删除，先列出文件名和归属供确认。
4. **锁文件单独处理。** 正常退出后锁文件可留存，OS 会释放进程锁。不能因为远端删除就删锁文件；只有确切确认不是活动锁且用户明确要求时才处理。
5. **按新 ID 重新预演。** 新隧道已创建且有 CLI 归属登记时，用新 ID 预演 `shared init`，不重复创建；否则先预演创建。核对名称、ID 和映射范围后，再按授权执行。不要手改旧记录的 ID，也不要用 `preview down` 清理已删除隧道的本地孤儿记录。

## 独立模式与回收

单应用默认使用独立预览；多应用同设备复用共享底座；明确需要隔离时也可选择 `preview up/run/down`。不得绕过共享或在线客户端保护。下线先预演，核对本地登记、远端映射 ID、完整参数及专用隧道归属，再使用 `--apply`。独立下线可能删除整个专用隧道；共享单站点不能使用 `preview down`。

迁移先查 `shared migrate --help`，校验来源和目标，保留私有回滚快照。写请求异常先对账，不盲目重发或接管外部资源。

## 交付标准

交付公共 URL、实际验证结果和未验证范围。不得把登录页 200 当作完整登录、上传或播放验收。原认证、分享期限和其他隧道保持不变。技能只包含操作指南，不包含平台凭证。
