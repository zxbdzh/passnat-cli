---
name: passnat-preview
description: 使用 PassNAT CLI 分享本机服务的 HTTPS 预览，处理共享隧道、独立预览、静态 HTML 白名单、状态检查与安全回收。
compatibility: Python 3.11+、PassNAT CLI、frpc、Caddy；Windows 支持 DPAPI 凭证保存。
---

# PassNAT HTTPS 预览

先检查 `passnat --help`，再读取相关资源的 `--help`。未安装 CLI 时，按项目 README 安装；不要假定用户的仓库位置、账号、节点 ID、域名 ID 或本机程序路径。

## 操作流程

1. 识别要分享的本地服务、端口和公开范围。公网发布必须获得用户授权；不得直接暴露仓库、配置目录、管理 API、目录列表或包含私密内容的 HTML。
2. 使用 `passnat auth check` 验证凭证。仅使用可信环境变量或 Windows 下 `auth login --stdin`；不要读取、打印、复制或写入真实密钥到命令参数、技能、日志或公开文件。
3. 使用 `tunnels list`、`domains list` 和已登记底座的 `shared status NAME` 查询现场。先复用归属明确的共享隧道，端口额度不足才创建下一条。保留已有服务和原认证。
4. 一条隧道只运行一个 frpc。共享模式统一管理全部登记代理，新增站点后 `shared sync NAME --reload --apply`，不为每个站点另起客户端。
5. 共享模式使用 `shared init/site/sync/run/status/migrate`，先查帮助并预演。仅在已获授权、预演范围正确时使用 `--apply`。使用两个独立终端或项目既有进程管理器分别运行 proxy 和 frpc；不在运行中的终端排队启动命令。
6. 静态站点使用绝对目录与逐个 `--file` 指定的 HTML 白名单。应用站点保留独立域名、登录与 Cookie 边界。普通 HTTP 上游不能直接作为 HTTPS 映射。
7. Caddy 使用 TLS-ALPN-01 为公网 HTTPS 443 申请和续期证书。证书、frpc 配置与管理接口保持私有，不能放在公开根目录。
8. `shared status` 或 `preview status` 必须验证平台公网地址、域名 SNI 和可信证书。代理在线不等于目标页面正常；状态码、认证跳转、刷新以及用户要求的业务流程需分别验证。需要登录时交给用户，不复制 Cookie、不伪造 Token。

## 独立模式与回收

只有明确需要隔离时使用 `preview up/run/down`。不得绕过共享或在线客户端保护。下线先预演，核对本地登记、远端映射 ID、完整参数及专用隧道归属，再使用 `--apply`。独立下线可能删除整个专用隧道；共享单站点不能使用 `preview down`。

迁移先查 `shared migrate --help`，校验来源和目标，保留私有回滚快照。写请求异常先对账，不盲目重发或接管外部资源。

## 交付标准

交付公共 URL、实际验证结果和未验证范围。不得把登录页 200 当作完整登录、上传或播放验收。原认证、分享期限和其他隧道保持不变。技能只包含操作指南，不包含平台凭证。
