# Cloudflare Tunnel

PI OS 使用 dashboard-managed Named Tunnel。家庭路由器不开放 80/443 入站端口。

Cloudflare 只负责把当前公网门牌送到本机 Nginx；它不决定 PI OS 的永久内部身份。

## 创建 Tunnel

进入 Cloudflare Zero Trust：

```text
Networks → Tunnels → Create a tunnel → Cloudflared
```

创建后选择 Docker 连接方式。Cloudflare 会显示包含长 token 的命令；只复制 `--token` 后面的 token。

Token 是密钥：

- 只写入本地 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN=`；
- 不贴到 Issue、README、截图或聊天；
- 不提交 Git。

初始化时没有 token，可以之后写入 `.env`，再运行：

```powershell
.\start.ps1
```

## 当前 Public Hostname

在 Tunnel 中添加当前网页门牌：

```text
Hostname: pi.ler428.xyz
Service type: HTTP
URL: nginx:80
```

界面若要求完整 origin，应显示：

```text
http://nginx:80
```

关键点：

- 必须指向 `nginx:80`；
- 不要填 `localhost:8080`，因为 cloudflared 运行在 Docker 容器内；
- 不要直接填 `web:3000`，否则 `/api/v1/streaming` 不会经过 Nginx；
- Cloudflare 处理公网 HTTPS，Docker 内部保持 HTTP。

## 主网页域名不要额外加的东西

为了让浏览器登录、Session、CSRF、API、媒体和 streaming 正常：

- 不配置 Cloudflare Access application/policy；
- 不启用 Under Attack Mode；
- 不给 `/api/*`、`/auth/*`、登录或 streaming 添加 Managed Challenge、JS Challenge 或 CAPTCHA；
- 不创建 Cache Everything 规则；
- 不重写 `/api/v1/streaming`；
- 保持 WebSockets 可用。

可以继续使用 Cloudflare 默认 TLS、DNS 代理和常规 DDoS 防护。以后若有独立管理面板，应使用另一个子域名和独立 Access 策略。

## 验证当前门牌

```powershell
.\status.ps1
```

配置 token 后，脚本会检查：

- `https://<WEB_DOMAIN>/_pi/health`；
- `https://<WEB_DOMAIN>/api/v2/instance`；
- `https://<WEB_DOMAIN>/api/v1/streaming/health`；
- `instance.domain=pi.invalid`；
- streaming URL 与当前 `WEB_DOMAIN` 一致。

随后用手机浏览器直接打开当前 `WEB_DOMAIN` 登录，不使用 Mastodon App。

## 每年更换门牌

### 1. 先加新 route

在同一个 Tunnel 中增加：

```text
pi.new-domain.xyz → http://nginx:80
```

旧 route 暂时保留。

### 2. Prepare

```powershell
.\change-access-domain.ps1 -Phase Prepare -NewDomain "pi.new-domain.xyz"
```

Prepare 会确认新域名的 `/_pi/health` 已通过 Cloudflare 到达同一个 Nginx，再把新 Host 加入 `ALTERNATE_DOMAINS`。

此时主 `WEB_DOMAIN` 仍是旧域名。Prepare 只证明 Tunnel、TLS、HostAuthorization 和基础 GET，不证明 Cookie、CSP、媒体 URL 或 streaming 已独立切换。

### 3. Switch

```powershell
.\change-access-domain.ps1 -Phase Switch -NewDomain "pi.new-domain.xyz"
```

脚本会正式切换 `WEB_DOMAIN`、同步 WSS、清 Redis 短期状态并重建应用进程。随后必须在新域名完成浏览器人工 smoke。

人工 smoke 中临时 disable/block 旧 route，再确认旧内容、媒体和 streaming 仍可使用，以排除新网页暗中依赖旧 origin。

### 4. Release

过渡完成后：

```powershell
.\change-access-domain.ps1 -Phase Release
```

再从 Cloudflare 删除旧 Public Hostname。

不要让已过期、可能被别人重新注册的旧域名长期留在 `ALTERNATE_DOMAINS`。

## 常见故障，只查失败点

### Tunnel 在线但页面 502

检查 Public Hostname origin 是否精确为：

```text
http://nginx:80
```

再运行：

```powershell
docker compose --profile tunnel logs --tail 100 cloudflared nginx
```

### 页面能开但实时更新失败

```powershell
docker compose exec -T streaming curl -fsS http://localhost:4000/api/v1/streaming/health
```

同时检查 `.env.production`：

```env
STREAMING_API_BASE_URL=wss://<当前 WEB_DOMAIN>
```

### 新域名 Prepare 能开，但页面仍引用旧域名

这是预期行为。新域名只在 `ALTERNATE_DOMAINS` 时，canonical URL、CSP、Cookie 和主 WSS 仍由旧 `WEB_DOMAIN` 生成。完成 Switch 后再做完整功能判断。

### 浏览器跳到额外验证页

删除主网页 hostname 上的 Cloudflare Access、Challenge 或 CAPTCHA，只保留 Tunnel。

### 域名切换后无法登录

新 origin 不继承旧 Cookie。使用密码/TOTP重新登录。旧域名创建的 WebAuthn/passkey 不可用；不要把它作为唯一认证方式。