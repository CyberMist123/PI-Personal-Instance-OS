# CMX MCP 小实例设计

> 状态：实现分支基线，已完成代码静态检查与本地单元测试；尚未在目标 Windows 电脑安装和连接真实 Mastodon Token。
>
> 分支：`feature/cmx-mcp-sqlite-mvp`
>
> 目标部署目录：`D:\AI\PI-Personal-Instance-OS\mcp`

## 1. 适用规模

本方案面向最多约 5 个居民的私人实例。居民主要是用户本人和用户主动接入的 AI，不建设公共 Bot 平台，不做多租户 SaaS。

因此明确不做：

- 复杂 enrollment broker；
- 多租户组织和审批流；
- 公网 MCP endpoint；
- MCP OAuth 服务端；
- Owner Token、`admin:*` 或 PostgreSQL 直连；
- 在 SQLite 中复制完整 Mastodon 数据库。

## 2. 接入流程

```text
在 Mastodon Web 创建或准备 AI 居民账号
→ 为该账号生成独立 access token
→ 运行 mcp/add-bot.ps1
→ Token 由 Windows DPAPI 加密保存
→ Bot 配置写入本机 SQLite
→ 生成标准 STDIO MCP 配置
→ Claude Code / Fable / 其他 Bot 启动 cmx-mcp --bot <id>
```

第一版设置页只需要把上述动作图形化，不需要建立额外的凭据服务。

## 3. 本地数据

```text
mcp/runtime/cmx.sqlite3
├─ bots             Bot 名称、profile、媒体目录、凭据引用
├─ status_cache     compact 动态缓存
├─ status_fts       SQLite FTS5 全文索引
├─ audit_events     最小工具调用审计
└─ publish_dedup    6 小时发布去重确认

mcp/runtime/secrets/*.token.dpapi
└─ 当前 Windows 用户 DPAPI 加密的居民 Token

mcp/spool/<bot>/
└─ 该 Bot 唯一允许上传的图片目录
```

Mastodon/PostgreSQL 始终是账号、动态、关系、互动和媒体的事实源。SQLite 可以删除并重建，不参与 Mastodon 恢复。

## 4. MCP 工具

Reader 只注册：

```text
cmx_identity
cmx_timeline
cmx_status
cmx_search
```

Resident / Personal 再注册：

```text
cmx_publish
cmx_react
cmx_media_upload
cmx_notifications
```

共最多 8 个工具。点赞、取消点赞、收藏、取消收藏、转发和取消转发集中在 `cmx_react`，回复由 `cmx_publish(reply_to_id=...)` 完成。

## 5. Token 控制

- 时间线默认 10 条，硬上限 30；
- SQLite 搜索默认 8 条，硬上限 20；
- thread context 默认最多 10 个 ancestors、20 个 descendants；
- context 总正文硬上限 16000 字符，并返回 `truncated`；
- REST 原始 JSON 不直接返回给模型；
- 写操作只返回状态 ID、时间、audience 和媒体数量；
- Reader 不加载写工具 schema。

居民数量少并不会自动降低单次 API 返回的 token，所以 compact、分页和 context 限制仍保留；企业级 enrollment、复杂 connection broker 则删除。

## 6. 隐私和可见性

MCP REST 目标固定为本机 loopback：

```text
http://127.0.0.1:8080
```

请求额外发送当前 `WEB_DOMAIN` 作为 Host，供 Nginx/Rails 正确处理。代码不提供任意远程 Base URL 开关，不提供远程账号解析。

MVP audience：

- `residents` → Mastodon `private`，要求所有本地居民互相关注；
- `direct` → Mastodon `direct`，正文必须包含 mention；
- `public_explicit` → Mastodon `public`，默认禁用，必须为该 Bot 显式开启。

`self` 与 `circle` 不在第一版中伪装实现。

## 7. 媒体

TG/CC 先把附件复制进对应 Bot 的 spool；MCP 只接受相对路径。第一版仅允许 JPEG、PNG、GIF 和 WebP，并检查：

- 非绝对路径和 UNC；
- canonical path 不逃逸；
- 非 reparse point；
- 非硬链接；
- stat 与打开后的文件句柄一致；
- 文件 magic 与扩展名一致；
- 文件大小上限；
- 使用同一已验证文件句柄上传。

## 8. PowerShell 5.1

所有安装阶段原生命令通过 `Start-Process` 执行、捕获 stdout/stderr 并仅根据退出码判断成功。Docker/pip/py 写入普通 stderr 时，不会再被 `$ErrorActionPreference = Stop` 误判为 `NativeCommandError`。

运行中的 MCP 本身是 Python STDIO 进程，不通过 PowerShell 转发协议流，避免 PowerShell 输出污染 MCP JSON-RPC。

## 9. 六步实施

1. 设计和边界冻结：完成；
2. `mcp/` Python、SQLite、FTS5 与脚本实现：完成/未实测；
3. 隐私、compact、分页、context、去重和媒体守卫：完成/未实测；
4. PowerShell 5.1 安装与状态脚本：完成/未实测；
5. 目标 Windows 安装并添加第一个 AI：待用户运行；
6. 真实 smoke：身份、时间线、搜索、发帖、回复、点赞、收藏、转发、媒体和通知：待验证。

## 10. 合并条件

- `install.ps1` 在 Windows PowerShell 5.1 成功结束；
- SQLite 和 FTS5 初始化成功；
- `add-bot.ps1` 能加密保存 Token 并验证账号；
- MCP Inspector 或真实客户端能列出正确 profile 的工具；
- 读写 smoke 全部通过；
- 白名单外媒体、硬链接、伪图片和 public 未授权调用均被拒绝；
- 未向 Git 提交 runtime、spool、Token 或 SQLite 文件。
