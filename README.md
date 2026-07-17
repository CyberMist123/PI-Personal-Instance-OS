# PI OS

**π / Personal Instance OS**

一切无规律、不会终结的数字；也是一个属于自己的个人实例。

PI OS 是平行于 AI OS（memory + operation）的私人生活世界。它以动态和时间线为基本单位，不要求每条内容得到回复，也不默认把生活交给 AI 分析。

使用者和读者只有本人以及明确邀请的人。程序、数据库和媒体保存在本地 Windows 电脑，通过 Cloudflare Tunnel 供手机浏览器访问。

用途包括：

- 书影音档案；
- 日记与时间轴；
- 朋友圈式生活记录；
- 心情、碎碎念、图片和收藏；
- 后续由独立 AI 居民账号发布的博客、日记和状态。

## 当前状态

2026-07-17，基础网页 MVP 已在目标 Windows 电脑真实部署并完成验收：

- 手机和 PC 均可登录；
- 发布文字、图片和跨设备同步正常；
- 公开注册关闭；
- 全链路状态检查通过；
- 首次完整备份成功；
- Windows 重启后网页和旧内容恢复正常；
- Docker Desktop 静默自启与 PI OS 自定义启动链路共同工作。

当前网页由 Mastodon Web 提供。独立 CMX 前端、AI 居民和 MCP 属于下一阶段。

## 最重要的项目文件

**先读 [`PROJECT.md`](PROJECT.md)。**

它是当前需求、边界、架构、接口、数据位置、运行流程、进度表和下一步的唯一权威入口。Agent 执行纪律见 [`AGENTS.md`](AGENTS.md)。

## 当前架构

```text
手机 / PC 浏览器
      ↓ HTTPS
当前 WEB_DOMAIN
      ↓
Cloudflare Named Tunnel
      ↓
Nginx
 ├─ Mastodon Web / Session / REST / 上传
 └─ Mastodon Streaming
      ↓
Sidekiq / PostgreSQL / Redis / 本地媒体
```

域名角色已经拆开：

```env
LOCAL_DOMAIN=pi.invalid           # 永久内部身份，不访问、不更换
WEB_DOMAIN=pi.ler428.xyz          # 当前公网门牌，可以受控替换
STREAMING_API_BASE_URL=wss://pi.ler428.xyz
ALTERNATE_DOMAINS=                # 只在切换过渡期使用
```

实例不接公开联邦。`WEB_DOMAIN` 只能通过 `change-access-domain.ps1` 的 Prepare / Switch / Release 流程更换。

## 文档入口

- [当前项目事实与进度](PROJECT.md)
- [Agent 执行入口](AGENTS.md)
- [系统架构](docs/ARCHITECTURE.md)
- [需求与停止线](docs/MVP_SCOPE.md)
- [Windows 部署与运维](docs/DEPLOYMENT.md)
- [Cloudflare Tunnel](docs/CLOUDFLARE.md)
- [备份恢复](docs/RESTORE.md)

本地目录：

```text
D:\AI\PI-Personal-Instance-OS
```

## 日常运维

从任意 PowerShell 目录运行状态检查：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\status.ps1"
```

或者先进入项目目录：

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"
.\start.ps1
.\stop.ps1
.\status.ps1
.\backup.ps1
```

`start.ps1` 是手动启动、计划任务启动和故障恢复的统一入口，必须保留。

## Windows 自动启动

当前使用双层启动：

```text
Windows 登录
→ Docker Desktop 静默自启
→ PI-OS-Autostart 等待 Docker engine 就绪
→ start.ps1 明确拉起 tunnel profile 与全部服务
→ 健康检查与日志
```

Docker Compose 的 `restart: unless-stopped` 作为容器级兜底，但不替代 `start.ps1`。

安装或覆盖自动启动任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\install-autostart.ps1"
```

带进度测试自动启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\test-autostart.ps1"
```

## 更换公网门牌

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"
.\change-access-domain.ps1 -Phase Prepare -NewDomain "pi.new-domain.xyz"
.\change-access-domain.ps1 -Phase Switch  -NewDomain "pi.new-domain.xyz"
.\change-access-domain.ps1 -Phase Release
```

## 数据边界

仓库只保存“如何建造世界”，不保存实际世界。以下内容永远不得进入 Git：

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

这是自己的书影音档案、日记、朋友圈、时间轴、心情记录与碎碎念。正式社交平台只需要承担社交和暴露在外的表现欲望（笑）。

需要这样一个自留地让人安心。也相信这样的安全基地、个人实例 OS 能让生活变得更好。正因为外部没有依靠，所以会建造自己的通天塔。❤️
