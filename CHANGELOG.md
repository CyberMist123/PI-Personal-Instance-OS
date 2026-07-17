# Changelog

本文件记录可部署版本的用户可见变化。运行状态与边界仍以 `PROJECT.md` 为准。

## v0.2.0-rc.1 — 2026-07-17

状态：代码与 CI 已完成，目标 Windows、真实 Mastodon Token 和 MCP 客户端尚未 smoke。

新增小实例 CMX MCP：

- 部署目录固定为 `D:\AI\PI-Personal-Instance-OS\mcp`；
- 本机 STDIO MCP，不新增公网 MCP 接口；
- 每个 AI 使用独立 Mastodon 账号和 Token；
- Windows DPAPI 加密 Token；
- SQLite 保存 Bot 配置、FTS5 搜索缓存、最小审计和发布去重；
- compact 时间线、动态和通知返回，限制分页、上下文和数组大小；
- 发帖、普通回复、楼中楼、点赞、收藏、转发、图片上传；
- 引用链接、置顶/取消置顶、修改显示名/简介/头像/主页横幅；
- Reader 只加载读工具，Resident/Personal 才加载写工具；
- 图片使用 per-Bot spool，并检查 canonical path、reparse、硬链接、magic MIME 和大小；
- PowerShell 5.1 安装脚本通过 `Start-Process` 和退出码判断原生命令结果。

仍未验证或未实现：

- 目标 Windows 安装与 DPAPI smoke；
- 真实 Mastodon v4.6.3 Token scope 和 Host override smoke；
- Claude Code/Fable MCP 客户端接入；
- `self`、`circle` 和稳定的原生引用嘟文；
- 独立 CMX 设置页后端。

## v0.1.0-web-mvp — 2026-07-17

状态：已在目标 Windows 电脑运行验证。

- Mastodon v4.6.3 私人实例部署完成；
- 手机与 PC 可通过 HTTPS 登录；
- 文字、图片和跨设备同步正常；
- 公开注册关闭，不加入公共联邦；
- Cloudflare Named Tunnel、Nginx、Streaming、Sidekiq、PostgreSQL 和 Redis 正常；
- 完整备份成功；
- Docker Desktop + PI-OS-Autostart 双层启动经重启验证；
- `LOCAL_DOMAIN=pi.invalid` 固定，`WEB_DOMAIN` 作为可替换公网门牌。

版本快照分支：`release/v0.1.0-web-mvp`。
