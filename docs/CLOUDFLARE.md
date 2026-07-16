# Cloudflare Tunnel

PI OS 使用 dashboard-managed Named Tunnel。家庭路由器不开放 80/443 入站端口。

## 创建 Tunnel

进入 Cloudflare Zero Trust：

```text
Networks → Tunnels → Create a tunnel → Cloudflared
```

创建后选择 Docker 连接方式。Cloudflare 会显示一条包含长 token 的命令；只复制 `--token` 后面的 token。

Token 是密钥：

- 只写入本地 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN=`。
- 不贴到 Issue、README、截图或聊天记录。
- 不提交 Git。

如果初始化时没有 token，之后手动写入 `.env`，再运行：

```powershell
.\start.ps1
```

脚本会自动启用 Compose 的 `tunnel` profile。

## 配置 Public Hostname

在该 Tunnel 中添加 Public Hostname：

```text
Hostname: 最终 Mastodon 域名
Service type: HTTP
URL: nginx:80
```

关键点：

- 必须是 `nginx:80`。
- 不要填 `localhost:8080`；`cloudflared` 运行在 Docker 容器里。
- 不要直接填 `web:3000`；Nginx 负责把 `/api/v1/streaming` 转发到 streaming 容器。
- Cloudflare 负责公网 HTTPS，Docker 内部保持 HTTP。

## Access 设置

主 Mastodon hostname 不配置 Cloudflare Access policy。

额外的 Access 登录页可能阻断 Mastodon iOS 客户端的 OAuth、API、WebSocket/streaming 和未来 Bot token。

以后出现管理面板时，使用另一个子域名并单独套 Access，不要套在主实例上。

## 验证

```powershell
.\status.ps1
```

通过后访问：

```text
https://你的最终域名/_pi/health
```

应返回：

```text
PI OS OK
```

随后用 iOS Mastodon 客户端输入同一域名登录。

## 常见故障，只查失败点

### Tunnel 在线但页面 502

检查 Public Hostname 的 origin 是否精确为：

```text
http://nginx:80
```

再运行：

```powershell
docker compose --profile tunnel logs --tail 100 cloudflared nginx
```

### 网页能开但通知或时间线不实时

检查 Nginx streaming 路由，不要绕过 Nginx。运行：

```powershell
docker compose exec -T streaming curl -fsS http://localhost:4000/api/v1/streaming/health
```

### iOS 客户端跳到额外登录页

删除主 hostname 上的 Cloudflare Access application/policy。只保留 Tunnel。
