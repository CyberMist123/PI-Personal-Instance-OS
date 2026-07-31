# 全站搜索 · 临时交接单

> 临时文件。收口后把结论并回 [`PROJECT.md`](../PROJECT.md) 并删除本文件。
> 最后更新：2026-08-01。分支 `feat/clip-brain-backend`，已推送。

## 一句话现状

后端全部做完并验证；**只差前端拦截层用错了 API**——补丁打在 `window.fetch` 上，而 Mastodon 用 axios（XHR）。

## 唯一的待办

`mcp/src/cmx_mcp/search_widget.py` 里的 `SEARCH_WIDGET_JS` 改为拦截 **XMLHttpRequest**。

证据（从运行中的容器里读的真实源码，不是推断）：

```
/opt/mastodon/app/javascript/mastodon/api.ts:6   } from 'axios';
/opt/mastodon/app/javascript/mastodon/api.ts:7   import axios from 'axios';
/opt/mastodon/app/javascript/mastodon/api.ts:93  const instance = axios.create({
```

`axios.create()` 未覆盖 `adapter`，浏览器默认即 XHR。**容器里有完整前端 TypeScript 源码**（`/opt/mastodon/app/javascript/`），下次要查前端行为直接 `docker exec pi-os-web-1` 看，不要靠记忆推断——上一轮就是靠推断猜错了一整轮。

要拦的请求：`GET /api/v2/search?q=...`。改完 `SEARCH_WIDGET_VERSION` 要 +1（ETag 是 `"search-<version>"`），然后重启 `cmx-mcp-http`；nginx 不用动。

现有的所有回退纪律必须保留：**任何失败都原样返回 Mastodon 自己的响应**，绝不能让搜索框比现在更坏。

## 已完成且已部署

| 组件 | 位置 | 状态 |
|---|---|---|
| 全站 grep | `site_search.py` | ✅ psql 参数化、无 shell；注入实测无效且表完好 |
| 搜索端点 | `GET /files/search` | ✅ 公网 401；**居民 `gpt` 有效 Token 403** |
| `search_enabled` 开关 | `nginx/default.conf` sub_filter | ✅ `false`→`true`，前端已不再显示"不可用" |
| 脚本注入 | `/files/search.js` | ✅ 公网 200，ETag `W/"search-1"`，首页含标签 |
| 拦截逻辑 | `search_widget.py` | ❌ **打在 fetch 上，无效** |

`meta.search_enabled` 是前端画"在 pi.invalid 不可用"的唯一依据（`features/compose/components/search.tsx:127/667/688`）。为 false 时**连请求都不发**，所以在翻开这个开关之前，任何拦截都拦不到东西。

## 本轮其他已交付（均已部署）

- **中文子串搜索修复**：FTS5 `unicode61` 不切分 CJK，整段汉字是一个 token。生产实测：修前搜「摸鱼」0 条、「摸鱼打卡」1 条。顺带修掉 LIKE 兜底路径缺可见性过滤导致 direct/self 可能外泄。
- **`【url-xhs】` 链接占位符**：小红书分享 64→20 字符。广告语按**链接位置**截断（同一行、链接之后的一律来自平台），不做模板匹配——模板方案实测会吃掉用户原话。
- **图片识别链**：本机 PP-OCRv6（`D:\AI\models\rapidocr\`，`CMX_OCR_MODEL_TIER=medium` 已设为 User 级环境变量）+ Gemini `gemini-3.1-flash-lite`。`POST /files/recognize` 已部署，SHA-256 缓存实测 6032ms→3ms。**但没有任何东西自动调用它**——缺注入脚本里发图后的那一句。
- **SQLite v6**：`image_recognition`（按内容哈希全局存，故意不按 `bot_id`）+ `status_media`。生产库已迁移。**单向门**：v6 之前的代码会因版本闸拒绝启动，回滚需同时恢复数据库备份。

## 环境与凭据

- Gemini key：`cmx-admin gemini-key` 录入，DPAPI 加密存 `mcp/runtime/secrets/gemini.key.dpapi`。当前是**免费档**（数据会被 Google 用于训练；换付费档只需充值后重跑该命令）。
- `CMX_OCR_MODEL_TIER=medium`（User 级环境变量）。small 档 1.0s/张、medium 4.4s/张，均在 onnxruntime 单线程下测得。

## 踩过的坑，别再踩

1. **先停进程再 pip install**。`cmx-mcp-http.exe` / `cmx-mcp.exe` 被占用时 pip 会中途失败。venv 里那批 `~` 开头的目录就是历次失败的残留，可清。
2. **PowerShell 5.1 跑原生程序会假失败**。`smoke.ps1` 报 exit 1 但实际 `"ok": true` —— stderr 被包装成 `NativeCommandError`。判断成败看程序自己的输出，不要看 PowerShell 的退出码。
3. **`psql -c` 不做变量插值**，`:'term'` 会变成语法错误。SQL 必须从 stdin 送入。
4. **测试用生产路径会触发迁移**。`create_remote_app()` 会 `initialize()`，我就是这样意外把生产库推到 v6 的。
5. **STDIO 客户端跑的是会话启动时的代码**。改完要重连，否则看到的还是旧行为。

## 遗留

- issue [#31](https://github.com/CyberMist123/PI-Personal-Instance-OS/issues/31)：居民搜索仍排除 direct/self（对 AI 是**正确**的，别"修"）；机器人 Token 能通过网页 bearer 校验（对 `/files/transcribe`、`/files/recognize` 无害，`/files/search` 已单独校验 owner）。
- `nginx/default.conf` 的 CSP 头仍含真实域名，见 issue #29。
