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

## 主域名不要额外加的东西

为了让 Mastodon iOS 客户端正常完成实例发现、OAuth、API、媒体上传和 streaming：

- 不配置 Cloudflare Access application/policy。
- 不启用 Under Attack Mode。
- 不给 `/oauth/*`、`/api/*`、`/auth/*` 添加 Managed Challenge、JS Challenge 或 CAPTCHA。
- 不创建“Cache Everything”规则；动态 API 和登录页面保持默认不缓存。
- WebSockets 保持可用，不对 streaming 路径做重写。

可以继续使用 Cloudflare 默认的 TLS、DNS 代理和常规 DDoS 防护。以后若出现管理面板，使用另一个子域名并单独套 Access，不要套在主 Mastodon hostname 上。

## 验证

```powershell
.\status.ps1
```

配置 token 后，脚本会检查：

- `https://你的域名/_pi/health`
- `https://你的域名/api/v2/instance`
- `https://你的域名/api/v1/streaming/health`

三条都通过后，再用 iOS Mastodon 客户端输入同一域名登录。

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

### iOS 客户端跳到额外登录页或验证页

删除主 hostname 上的 Cloudflare Access、Challenge 或 CAPTCHA 规则，只保留 Tunnel。

### 网页可以登录，但 App 找不到实例

确认 `/api/v2/instance` 没有被缓存、重写或挑战；再运行 `status.ps1` 看 Public Mastodon API discovery 项。
