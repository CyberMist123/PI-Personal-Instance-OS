# PI OS

**π / Personal Instance OS**

一切无规律、不会终结的数字；也是一个属于自己的个人实例。

PI OS 是平行于 AI OS（memory + operation）的私人生活世界。它以动态和时间线为基本单位，不要求每条内容得到回复，也不默认把生活交给 AI 分析。

使用者和读者只有本人以及明确邀请的人。程序、数据库和媒体保存在本地电脑，通过 Cloudflare Tunnel 供 iOS 手机端访问。

用途包括：

- 书影音档案
- 日记与时间轴
- 朋友圈式生活记录
- 心情、碎碎念、图片和收藏
- 以后按需加入拥有独立账号的 Bot 或 AI 居民

AI 与 Bot 不是全局监控器。它们只能通过独立 Mastodon API Token，在明确授权的时间线、提及或标签范围内行动。

## 当前 Beta

底座使用未经魔改的 Mastodon 官方容器：

```text
iOS Mastodon App
        ↓
Cloudflare Named Tunnel
        ↓
Nginx
   ├─ Mastodon Web
   └─ Mastodon Streaming
        ↓
Sidekiq / PostgreSQL / Redis / 本地媒体
```

默认关闭公开注册并启用 limited federation。主站不套 Cloudflare Access，以保证 iOS OAuth、API 和 streaming 正常。

## 部署与接手入口

- [AI 接手说明：定义、接口、架构和当前流程](AI_HANDOFF.md)
- [系统是怎么搭的](docs/ARCHITECTURE.md)
- [需求与停止线](docs/MVP_SCOPE.md)
- [Windows 一次部署](docs/DEPLOYMENT.md)
- [Cloudflare Tunnel](docs/CLOUDFLARE.md)
- [备份恢复](docs/RESTORE.md)
- [交给 Claude 做一次只读审计](docs/CLAUDE_REVIEW.md)

本地目录：

```text
D:\AI\PI-Personal-Instance-OS
```

克隆后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

首次部署和手机验收完成后，双击：

```text
安装开机自启.bat
```

它会安装计划任务 `PI-OS-Autostart`：Windows 用户登录后，必要时启动 Docker Desktop，再恢复 PI OS。日志写入 `logs\autostart.log`。移除时双击 `卸载开机自启.bat`，不会删除容器或数据。

日常运维只有：

```powershell
.\start.ps1
.\stop.ps1
.\status.ps1
.\backup.ps1
```

## 数据边界

仓库只保存部署代码和说明，不保存实际世界。

以下内容永远不得进入 Git：

```text
.env
.env.production
.pi-os-initialized
data/
backups/
logs/
Cloudflare token / credentials
```

PostgreSQL 与 Redis 使用本机 Docker named volumes；上传媒体和备份保存在项目目录。不要运行 `docker compose down -v`。

## 初心

希望做一个平行于 AI 系统的个人实例，使用者和读者只有本人和经过邀请的人。

这是自己的书影音档案、日记、朋友圈、时间轴、心情记录与碎碎念。正式的社交平台只需要承担社交和暴露在外的表现欲望（笑）。

需要这样一个自留地让人安心。也相信这样的安全基地、个人实例 OS 能让生活变得更好。正因为外部没有依靠，所以会建造自己的通天塔。❤️
