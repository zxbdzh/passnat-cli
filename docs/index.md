---
layout: default
title: PassNAT CLI
---

# 把本地服务，分享成 HTTPS 链接

**PassNAT CLI** 是一个非官方开源命令行工具，用于管理 PassNAT 映射，为本机应用和明确指定的静态 HTML 提供可信 HTTPS 预览。

[查看源码与完整使用说明](https://github.com/zxbdzh/passnat-cli) · [MIT 许可证](https://github.com/zxbdzh/passnat-cli/blob/main/LICENSE)

## 让 AI 一键安装

把这段发给有终端操作能力的 AI 助手即可：

```text
请按 https://zxbdzh.github.io/passnat-cli/ai-install.html 的安装说明，直接为当前电脑安装 PassNAT CLI 和当前 AI 的 passnat-preview 技能，并完成离线自检。只安装，不读取密钥、不创建隧道、不开放端口。
```

[查看完整提示词与安装边界](ai-install.html)。安装无需 SecretKey，frpc 和 Caddy 的准备情况会单独报告。

## 在自己的电脑上运行

需要 Python 3.11+，预览功能另外需要 PassNAT 账号、frpc 和 Caddy。

```sh
git clone https://github.com/zxbdzh/passnat-cli.git
cd passnat-cli
python -m pip install .
passnat --help
```

**这是一页公开文档，不是在线隧道控制台。** 本页没有账号登录、密钥输入或远程命令执行功能，也不会连接访问者的本地服务。

## 先预演，再修改

平台资源写操作默认给出预演计划；核对范围后再添加 `--apply`。配置归属不明确、映射已被外部修改或隧道存在其他服务时，拒绝危险回收。

```sh
passnat auth check
passnat tunnels list
passnat domains list
passnat shared status YOUR_SHARED_NAME
```

认证需要由使用者在本机安全提供自己的密钥；上述命令不是匿名公共 API。

## 一条隧道，多个站点

共享模式统一管理一个 frpc 和一个 Caddy，新站点通过热重载加入。需要隔离时，也支持专用预览。静态页面只开放明确列出的 HTML，而不是整个目录。

## 密钥留在本机

- 仓库不提供 SecretKey、共享账号或真实运行配置。
- macOS 可使用系统钥匙串、Windows 可使用当前用户的 DPAPI 保存登录凭证；其他系统使用环境变量。
- 运行配置、证书和状态记录保持私有，不能上传到公开仓库或本站。
- 原应用的登录、权限、Cookie 和回调边界继续有效。

[阅读安全边界](https://github.com/zxbdzh/passnat-cli/blob/main/SECURITY.md) · [查看 Agent 操作指南](https://github.com/zxbdzh/passnat-cli/tree/main/skills/passnat-preview)

## 获取帮助

```sh
passnat shared --help
passnat preview --help
passnat schema
```

请先阅读仓库 README，再提交已脱敏的问题描述。PassNAT 服务、frpc 和 Caddy 由各自权利人维护，本项目与 PassNAT 官方无隶属关系。
