# PassNAT CLI

用命令行管理 PassNAT 映射，为本地服务提供可信 HTTPS 预览。

[使用说明](https://zxbdzh.github.io/passnat-cli/) · [安全说明](SECURITY.md) · [MIT 许可证](LICENSE)

> 非官方社区工具，与 PassNAT 官方无隶属关系。使用者需自行提供具有相应权限的 PassNAT 账号与 SecretKey；本项目不提供公共隧道账号或共享密钥。

## 能做什么

- 查询账号、隧道、域名及端口映射，输出结构化 JSON。
- 写入平台的创建、删除、迁移和配置操作默认预演，核对后添加 `--apply`。
- 共享模式：多个站点复用一条隧道、一个 frpc 与一个 Caddy，可热重载新增站点。
- 独立模式：为需要隔离的应用创建专用 HTTPS 预览。
- 静态站点只公开逐个指定的 HTML 文件，不开放目录列表或整个仓库。
- 通过公网节点地址连接，保留域名 SNI/Host 并校验证书，区分本机直连与真实公网访问。
- 保留应用原有登录、Cookie 与回调边界，不提供绕过认证的能力。

## 环境与安装

- Python **3.11 或更高版本**，Python 运行依赖仅使用标准库。
- HTTPS 预览另需原版 **frpc** 与 **Caddy**；这些第三方程序不随本仓库分发。
- 本机凭证持久保存使用 Windows DPAPI。其他系统请使用环境变量。
- 当前主要在 Windows 上使用；其他系统的 API 操作与 HTTPS 预览请先在自己的环境验证。

```sh
git clone https://github.com/zxbdzh/passnat-cli.git
cd passnat-cli
python -m pip install .
passnat --help
```

也可不安装，进入项目目录运行 `python -m passnat --help`。Windows 提供 `passnat.cmd`，优先使用 Python Launcher，再回退到 `python`。

## 凭证

**不要把 SecretKey 放入源文件、命令参数、截图、Issue 或 Git 提交。**

支持两种来源：

1. 使用可信的本地凭证工具向当前进程提供 `PASSNAT_SECRET_KEY` 环境变量。
2. Windows 下通过标准输入执行 `passnat auth login --stdin`；先验证凭证，再以当前用户的 DPAPI 加密保存。不要把真实密钥直接写入 shell 命令或脚本。

```sh
passnat auth check
passnat tunnels list
passnat domains list
passnat schema
```

本地配置与状态默认存放在 Windows 的 `%LOCALAPPDATA%/PassNatCLI`，其他系统使用 `$HOME/.local/share/PassNatCLI`。可以通过 `PASSNAT_HOME` 指定**仅本人可访问的私有目录**；不要指定仓库、共享目录或公开站点根目录。frpc 配置、证书和状态记录可能包含敏感连接信息，即使登录凭证由 DPAPI 加密，也不能公开这些运行文件。

## 共享 HTTPS 预览

一条隧道只运行一个 frpc。不要在已有其他客户端的隧道中启动第二个客户端；它们可能互相踢下线。

以下大写标识表示应替换的账号资源或本机路径；节点 ID、域名 ID 和域名后缀均以当前账号查询结果为准。

```sh
# 首次初始化：选用本工具创建并登记、没有其他客户端占用的隧道。
passnat shared init dev --tunnel TUNNEL_ID
passnat shared init dev --tunnel TUNNEL_ID --apply

# 新增站点：先核对预演，再执行。
passnat shared site dev demo --subdomain YOUR_SUBDOMAIN --domain-id DOMAIN_ID --upstream-port 8000
passnat shared site dev demo --subdomain YOUR_SUBDOMAIN --domain-id DOMAIN_ID --upstream-port 8000 --apply
passnat shared sync dev --apply
```

在两个独立终端或项目既有进程管理器中，分别前台运行：

```sh
passnat shared run dev proxy
passnat shared run dev frpc
```

已有共享底座运行时，新增站点后使用 `passnat shared sync dev --reload --apply`，不要重复启动 frpc。

```sh
passnat shared status dev

# 静态页面：仅允许明确列出的 HTML，目录必须使用绝对路径。
passnat shared site dev docs --subdomain YOUR_DOCS_SUBDOMAIN --domain-id DOMAIN_ID --root ABSOLUTE_PUBLIC_DIRECTORY --file index.html --file guide.html
```

Caddy 使用 TLS-ALPN-01 为公网 HTTPS 443 申请和续期证书，平台映射指向本机共享 TLS 端口。普通 HTTP 上游不能直接当作 HTTPS 映射使用。共享单站点下线不能使用独立模式的 `preview down`。

## 独立预览

需要隔离时，创建专用隧道。将 frpc 放入 `PATH`、通过 `PASSNAT_FRPC` 提供，或向 `preview up` 传入 `--frpc`。Caddy 可以由 `passnat tools install-caddy` 安装，或在独立模式下通过 `PASSNAT_CADDY` 指定；共享模式使用私有目录 `bin/caddy.exe`。

```sh
# 下列创建命令先预演，确认后重复执行并添加 --apply。
passnat tunnels create mypreview --node-id NODE_ID --port-name demo-tcp --protocol https --local-port 443 --subdomain YOUR_SUBDOMAIN --domain-id DOMAIN_ID

# 使用创建后返回的隧道 ID；先预演，再加 --apply。
passnat preview up demo --tunnel TUNNEL_ID --local-port 8000 --public-port 443 --domain-id DOMAIN_ID --subdomain YOUR_SUBDOMAIN

# 配置完成后，在两个独立终端前台运行。
passnat preview run demo proxy
passnat preview run demo frpc
passnat preview status demo
```

`tools install-caddy` 会访问官方发行地址并校验下载文件；当前自动安装仅支持 Windows amd64。它会安装程序，不属于 `--apply` 控制的平台资源预演。

## 安全回收与迁移

```sh
passnat ports delete LOCAL_PORT_NAME
passnat preview down demo
```

核对预演后再添加 `--apply`。`ports delete` 接受本地登记名，不是代理 UUID。独立预览下线可能删除整个专用隧道，链接会失效，并非暂停。映射身份或参数变化、存在其他服务、缺少归属记录时拒绝自动下线。

共享迁移使用 `passnat shared migrate --help`。迁移会保存私有回滚快照，但同名域名切换可能短暂中断。写请求结果不确定时先对账，不盲目重发。

## 输出与退出码

成功返回 `{"ok": true, "data": {}, "meta": {}}`；失败返回 `{"ok": false, "error": {}}`。运行进程日志使用 stderr。

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 网络、平台 API 或运行错误 |
| 2 | 鉴权错误 |
| 3 | 参数或归属校验错误 |

结构化输出按敏感字段名递归脱敏，但不保证任意文本字符串、第三方进程日志或业务数据都可公开。分享前仍需人工检查；不要发布原始账号查询结果、frpc 配置、证书或迁移快照。

## Agent 技能

`skills/passnat-preview/SKILL.md` 提供随项目发布的通用操作指南。按所用 Agent 的技能安装方式使用；技能不包含凭证，不会自动安装 CLI、frpc 或 Caddy。

## 开发与验证

```sh
python -m unittest discover -s tests -v
```

单元测试使用合成数据和 mock，不会创建真实平台资源。测试通过不等于真实应用登录、上传或播放链路已验收。

## 许可证

本项目采用 [MIT License](LICENSE)。PassNAT 服务、frpc 和 Caddy 的商标、服务条款及许可证分别归各自权利人所有。
