# PI OS — 私人生活时间线 Beta 范围

## 项目定位

**PI = π / Personal Instance OS。**

这是一个只服务本人及明确受邀居民的私人数字自留地，与 AI OS（memory + operation）平行存在。

核心不是“让 AI 回复所有内容”，而是提供独立于聊天系统的生活时间线：动态可以发布、保存、浏览，也可以无人回应。AI 与 Bot 是可选居民，不是全局监控器或默认旁白。

## 已确认需求

### 当前 MVP

- 使用 Mastodon v4.6.3 官方容器作为稳定后端，当前不 fork 上游。
- 部署在家中闲置 Windows 电脑，本地目录为 `D:\AI\PI-Personal-Instance-OS`。
- 手机通过浏览器访问网页，不使用 Mastodon App。
- 通过 Cloudflare Named Tunnel 提供 HTTPS，不开放家庭路由器入站端口。
- Owner 可以网页登录、发布文字和图片、浏览时间线与实时更新。
- 关闭公开注册、公开页面与公开联邦。
- 数据、媒体、密钥和备份不进入 Git。
- 重启后可恢复，且存在可验证的 PostgreSQL/媒体备份流程。

### 域名要求

- 永久内部身份固定为 `LOCAL_DOMAIN=pi.invalid`。
- 当前公网入口由 `WEB_DOMAIN` 表示，可以按受控流程替换。
- `STREAMING_API_BASE_URL` 与当前 `WEB_DOMAIN` 同步。
- `ALTERNATE_DOMAINS` 只用于过渡 Host，不被当成完整 origin 迁移机制。
- 可变门牌实例永久不加入公共联邦；不全库改写历史 ActivityPub URI。

### CMX 网页要求

独立 CMX 前端属于后续增量，尚未在本仓库实现。加入时必须：

- 与 Mastodon 同源部署；
- 使用网页 Session / CSRF 或页面派发的当前用户 token；
- REST、媒体和 streaming 不硬编码公网域名；
- 不注册长期绑定某个 `WEB_DOMAIN` 的 OAuth application；
- 把浏览器 origin 相关的 Service Worker、Web Push 和缓存视为可重建状态。

### AI / Bot / MCP 要求

属于已确认的后续需求，尚未实现：

- AI 可拥有独立正式用户账号、头像、主页和发布历史；
- AI 可通过独立最小权限 Token 或窄权限 MCP 工具发布博客、日记、图片和回复；
- 可见范围至少覆盖：仅自己、指定圈子、实例居民、明确公开；
- AI 默认不读取全站，不因每条动态自动触发；
- AI/MCP 不直连 PostgreSQL，不使用 Owner 管理 Token。

## Beta 架构

```text
手机浏览器
        │ HTTPS
        ▼
当前 WEB_DOMAIN
        ▼
Cloudflare Named Tunnel
        ▼
Nginx
├─ Mastodon Web / Session / REST / 上传
└─ Mastodon Streaming
        │
        ▼
Sidekiq / PostgreSQL / Redis / 本地媒体
```

主网页域名不套 Cloudflare Access，避免阻断登录、Session、API、媒体与 streaming。管理工具以后使用独立子域名和独立策略。

## MVP 验收

- [ ] 本地 Compose 可以启动。
- [ ] `.env.production` 中 `LOCAL_DOMAIN=pi.invalid`。
- [ ] `WEB_DOMAIN` 公网地址可以打开网页。
- [ ] `/api/v2/instance` 的 `domain` 为 `pi.invalid`，streaming URL 使用当前门牌。
- [ ] Owner 创建、确认、批准并拥有 Owner 角色。
- [ ] 公开注册关闭。
- [ ] 手机浏览器可以完成密码/TOTP登录。
- [ ] 手机可以发布文字和至少一张图片。
- [ ] 旧数据、媒体、时间线和 streaming 正常。
- [ ] 电脑或服务重启后容器与 Tunnel 恢复。
- [ ] `backup.ps1` 生成可读 PostgreSQL dump 和媒体归档。
- [ ] `.env`、Tunnel 凭据、数据库、媒体、日志和备份未进入 Git。

满足以上条件即视为基础 Beta 部署完成，不继续扩范围。

## 明确不做

- 当前阶段不开发独立 CMX 前端。
- 当前阶段不接 AI、Bot、MCP、Cyberlink、Telegram、记忆系统或 520 面板。
- 不创建扫描全部动态的 Agent。
- 不做全文搜索、S3、Elasticsearch、Kubernetes、VPS 迁移或复杂监控栈。
- 不开启公共联邦。
- 不把 WebAuthn/passkey 作为唯一认证方式。
- 不为了“以后也许需要”提前增加额外服务。
- 不做第三轮纸面审计、性能压测或反复全量检查；真实运行失败时只修失败点。

## 后续增量

1. 实际恢复演练和离线加密备份。
2. 同源 CMX 移动网页。
3. 中文内容可见性语义与博客/日记视图。
4. 独立 AI 居民账号及最小 Token。
5. 窄权限 MCP 发布接口。
6. 邀请真人账号和指定圈子权限。

## 安全边界

自托管与 Tunnel 提供数据控制权和较小暴露面，但不是端到端加密。Cloudflare、服务器操作系统和 Owner 管理权限仍在信任边界内。

安全目标是：不开放家庭端口、关闭注册和联邦、同源 Session、最小权限、密钥不进 Git、可恢复备份、域名切换不改内部身份。