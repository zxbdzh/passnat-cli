---
layout: default
title: 让 AI 安装 PassNAT CLI
---

# 让 AI 一键安装

将下面的提示词复制给有终端和文件操作能力的 AI 助手。AI 会按实际环境完成安装；必要的系统权限仍由你确认。

```text
请在当前电脑安装 https://github.com/zxbdzh/passnat-cli 的 CLI，并将仓库 skills/passnat-preview 技能安装给当前 AI 助手使用。请直接执行安装，不要只给我教程。

1. 先读取仓库 README.md、SECURITY.md 和技能 SKILL.md；检查系统、Git、Python 3.11+、已有 passnat 和技能安装。保留用户改动；遇到覆盖或权限冲突再询问。
2. 从上述仓库进行用户级隔离安装。已有 uv 时优先执行 uv tool install "git+https://github.com/zxbdzh/passnat-cli"，选择 Python 3.11+；否则使用已有 pipx 或独立 venv。不要根据同名 PyPI 包猜测来源，不污染系统 Python。
3. 按当前 AI 的已确认技能安装约定安装 skills/passnat-preview；若由 Magpie 管理，走 Magpie Library。只安装给当前 AI，不批量修改其他助手、全局权限或安全设置。无法确认技能目录时再问我。
4. 处理命令入口和 PATH，分别验证 passnat --version、passnat --help、passnat schema；说明新终端或技能重新加载是否必要。检查 frpc、Caddy 是否可用，缺失时如实列为预览依赖，不冒充已安装完成。
5. 本次只安装与离线自检，无需 SecretKey。不要读取、打印或索取聊天中的密钥，不复制已有运行配置，不创建隧道、不启动常驻服务、不开放公网端口。

完成后告诉我 CLI 和技能的位置、自检结果、尚缺的依赖，以及下一步如何安全配置我自己的凭证。未完成的项目请明确说明。
```

## 更短的转发版本

```text
请按 https://zxbdzh.github.io/passnat-cli/ai-install.html 的安装说明，直接为当前电脑安装 PassNAT CLI 和当前 AI 的 passnat-preview 技能，并完成离线自检。只安装，不读取密钥、不创建隧道、不开放端口。
```

## 安装后的边界

- 安装不需要提供 SecretKey；真正使用 PassNAT 资源时，才需要在本机安全配置自己的凭证。
- frpc 和 Caddy 是 HTTPS 预览的额外依赖，不随 Python 包安装。CLI 和技能安装成功，不代表完整预览环境已就绪。
- 当前主要在 Windows 上验证使用；其他系统的 API 和预览能力应在目标环境实测。
- 本说明没有授权自动操作账号、接管现有隧道、替换认证或安装开机服务。

[返回首页](./) · [查看仓库 README](https://github.com/zxbdzh/passnat-cli#readme)
