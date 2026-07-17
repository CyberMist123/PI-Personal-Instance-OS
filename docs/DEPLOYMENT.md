# PI OS 部署与运维

目标：在 Windows 闲置电脑上运行私人 Mastodon 后端，通过 Cloudflare Tunnel 供手机浏览器访问。当前网页由 Mastodon Web 提供；独立 CMX 前端尚未加入。

仓库不包含 Mastodon 源码，不 fork 上游。运行时使用固定版本的官方容器镜像。

## 0. 已验证环境

2026-07-17，以下链路已在目标电脑真实跑通：

- Windows + Docker Desktop WSL2；
- Mastodon Web、Streaming、Sidekiq、PostgreSQL、Redis、Nginx、cloudflared；
- 手机和 PC 登录、发文字、发图片、时间线同步；
- 公开注册关闭；
- `status.ps1` 全链路检查；
- `backup.ps1` 完整备份；
- Windows 重启后自动恢复网页与旧内容。

当前本地目录：

```text
D:\AI\PI-Personal-Instance-OS
```

## 1. 域名模型

```env
LOCAL_DOMAIN=pi.invalid
WEB_DOMAIN=<当前公网域名>
STREAMING_API_BASE_URL=wss://<当前公网域名>
ALTERNATE_DOMAINS=
```

`LOCAL_DOMAIN` 永远不改。以后更换公网门牌只能使用 `change-access-domain.ps1`。

## 2. 首次克隆

```powershell
git clone https://github.com/CyberMist123/PI-Personal-Instance-OS.git "D:\AI\PI-Personal-Instance-OS"
Set-Location "D:\AI\PI-Personal-Instance-OS"
```

## 3. Cloudflare route

按 [CLOUDFLARE.md](./CLOUDFLARE.md) 创建 Named Tunnel，并添加当前网页域名：

```text
pi.ler428.xyz → http://nginx:80
```

主网页域名不要套 Cloudflare Access、Challenge 或 Cache Everything。

## 4. 首次初始化

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\setup.ps1" `
  -AccessDomain "pi.ler428.xyz"
```

脚本会：

- 复制 `.env` 与 `.env.production`；
- 固定写入 `LOCAL_DOMAIN=pi.invalid`；
- 写入 `WEB_DOMAIN`、`STREAMING_API_BASE_URL` 和空的 `ALTERNATE_DOMAINS`；
- 生成数据库密码、Mastodon secrets、加密密钥和 VAPID keys；
- 拉取固定镜像并初始化数据库；
- 创建、确认、批准并赋予 Owner 角色；
- 关闭公开注册；
- 启动 Web、Streaming、Sidekiq、Nginx 与 cloudflared。

Owner 初始密码只显示一次，立刻保存到密码管理器。

忘记密码且仍能进入本机时：

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"

docker compose run --rm --no-deps web `
  bin/tootctl accounts modify owner --reset-password --enable --approve
```

## 5. 数据位置

- PostgreSQL：Docker named volume `pi-os_postgres_data`；
- Redis：Docker named volume `pi-os_redis_data`；
- 图片和视频：`data\media`；
- 数据库导出、媒体归档和密钥快照：`backups`；
- 自动启动日志：`logs\autostart.log`。

绝对不要运行：

```powershell
docker compose down -v
```

正常停止只使用 `stop.ps1`。

## 6. 手动启停

`start.ps1` 是统一启动入口，必须保留。它用于：

- 手动启动；
- 自动启动计划任务；
- 故障恢复；
- 备份后恢复运行；
- 域名切换和后续运维。

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"
.\start.ps1
.\stop.ps1
```

所有服务还配置了 `restart: unless-stopped`，作为容器级恢复兜底。

## 7. 状态检查

不要在 `C:\Windows\system32` 中使用相对路径 `-File .\status.ps1`，因为那个目录没有脚本。

从任意目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\status.ps1"
```

或者：

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"
.\status.ps1
```

它检查：

- `LOCAL_DOMAIN=pi.invalid`；
- 当前 `WEB_DOMAIN` 与 streaming URL；
- 容器、Nginx、Web、Streaming、Sidekiq；
- 当前公网门牌与 Tunnel；
- `/api/v2/instance` 的 `domain=pi.invalid`；
- 公网 streaming route；
- Git 是否误追踪运行数据和密钥。

## 8. 首次备份

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\backup.ps1"
```

成功标志：

```text
Backup completed: D:\AI\PI-Personal-Instance-OS\backups\pi-os-...
```

快照包含 PostgreSQL dump、媒体归档、env、compose、manifest 与版本信息。至少复制一份到加密离线位置。

## 9. Windows 自动启动

当前采用双层启动，二者共同使用：

```text
Windows 用户登录
→ Docker Desktop 静默启动，负责启动 WSL2 / Docker Linux engine
→ PI-OS-Autostart 计划任务等待 Docker 就绪
→ 调用 start.ps1，明确启动 tunnel profile 与全部 PI OS 服务
→ 检查 http://127.0.0.1:8080/_pi/health
→ 写入 logs/autostart.log
```

### 9.1 Docker Desktop

在 Docker Desktop Settings → General 中启用：

```text
Start Docker Desktop when you sign in to your computer
```

保持 Dashboard 不在启动时主动弹出，即可静默运行在托盘。

### 9.2 PI OS 计划任务

安装或覆盖任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\install-autostart.ps1"
```

带进度验证：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "D:\AI\PI-Personal-Instance-OS\test-autostart.ps1" `
  -TimeoutSeconds 360
```

相关文件全部保留：

- `start.ps1`；
- `install-autostart.ps1`；
- `autostart-run.ps1`；
- `test-autostart.ps1`；
- `安装开机自启.bat`；
- `卸载开机自启.bat`。

Docker Desktop 自启负责 engine，PI OS 计划任务负责等待、调用 `start.ps1`、明确拉起 tunnel profile 和健康检查。二者不是替代关系。

## 10. 重启验收

重启 Windows 并登录后：

1. 等待 Docker Desktop engine 就绪；
2. 确认 `pi.ler428.xyz` 可打开；
3. 确认旧文字与图片仍存在；
4. 手机与 PC 均可访问和同步；
5. 需要完整诊断时，用绝对路径运行 `status.ps1`。

2026-07-17 已完成该验收。

## 11. 更换公网域名

先在 Cloudflare 为新域名添加同一个 Tunnel route，然后运行：

```powershell
Set-Location "D:\AI\PI-Personal-Instance-OS"
.\change-access-domain.ps1 -Phase Prepare -NewDomain "pi.new-domain.xyz"
.\change-access-domain.ps1 -Phase Switch  -NewDomain "pi.new-domain.xyz"
```

Prepare 只证明新 Host/TLS/基础 GET 可用。Switch 才正式修改 `WEB_DOMAIN`、同步 streaming、清 Redis 并重建应用进程。

过渡期结束：

```powershell
.\change-access-domain.ps1 -Phase Release
```

不对 `statuses.uri` 等历史字段做全库替换。

## 12. 邮件边界

第一版可以没有 SMTP。以下功能启用前必须配置：

- 忘记密码邮件；
- 邮箱确认；
- 网页邀请或注册其他真人；
- 系统安全通知。

发件地址必须使用真实域名，不能从 `pi.invalid` 推导。

## 13. 本地文件边界

永远不要提交：

- `.env`；
- `.env.production`；
- `.pi-os-initialized`；
- `data/`；
- `backups/`；
- `logs/`；
- Cloudflare token 或 credentials JSON。

## 14. 当前停止线

基础网页 MVP 已完成。独立 CMX、AI/MCP、内容权限中文语义和公开博客出口属于后续独立阶段。
