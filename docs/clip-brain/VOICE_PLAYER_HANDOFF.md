# 语音条：当前故障与交接单

**状态：v14 已部署，比 v13 更坏，建议先回退再调试。**
最后更新 2026-07-30。分支 `feat/clip-brain-backend`。

---

## 0. 先止血

线上现在是 v14，Owner 实测三项全坏。调试期间建议把播放器停掉，只留录音功能——
录音、上传、转写这条链是好的，坏的只有「替换 Mastodon 播放器」这一块。

最小停用改动：在 `mcp/src/cmx_mcp/voice_widget.py` 的 `boot()` 里注释掉
`watchTimeline(state);` 那一段，重启 `cmx-mcp-http`（需提权）。录音键不受影响。

---

## 1. 三个故障（Owner 在真机实测）

| # | 现象 | 平台 | v13 时的状态 |
|---|---|---|---|
| A | 播放器和柱子都在，但**柱子全是等高占位条**，没有真实波形 | 手机 | v13 有真实波形 |
| B | **仍是 Mastodon 原生黑框播放器**，完全没被接管 | PC | v13 也坏，一直没修好 |
| C | **播不出声音** | 手机 | v13 能播（MP3 之后） |

B 是老问题；**A 和 C 是 v14 引入的回归**，很可能同一个根因。

---

## 2. 最可疑的根因

v14 把整个替换动作搬进了 MutationObserver 回调里同步执行（`claimEarly` →
`decorate`），目的是让原生播放器一帧都不被画出来。**这个改动很可能太早了。**

回调发生在 DOM 变更之后、绘制之前。此时：

### 故障 A —— 已确诊

Owner 复述：「声音图在，但没有变化的波形了」。柱子渲染了，但**全是等高的占位条**
（`new Array(count).fill(0.4)`），说明真实取样那段从未执行。

`voice_player.py` 里取样被这个条件挡住：

```js
if (audio.currentSrc && window.AudioContext) { fetch(audio.currentSrc)... }
```

`decorate` 现在在 observer 回调里同步跑，那一刻 React 还没把 `src` 赋到元素上
（或 `preload="none"` 尚未解析），`currentSrc` 是空字符串，整段被跳过，**而且再
也没有第二次机会**——`decorate` 对同一元素只跑一次。

修法（择一，第二个更稳）：

1. 取源改为 `audio.currentSrc || audio.src || (audio.querySelector("source") || {}).src`；
2. 把取样从 `decorate` 里拆出来，挂到 `loadedmetadata` / `canplay` 上，或在
   `loadedmetadata` 时若 `peaks` 仍是占位就补取一次。**推荐这条**，因为它不依赖
   「此刻 src 已就绪」这个恰好成立的假设。

顺带：同一时刻 `wave.clientWidth` 也可能是 0（尚未布局），`layout()` 会退到
`|| 300`，柱子数量算得不准。`loadedmetadata` 时已重算过一次，但值得一并确认。

### 故障 C —— 同一根因的可能延伸

同步替换时 `original.style.display = "none"` 把容器藏掉。如果 Mastodon 的组件在
这之后才完成初始化（`currentSrc` 为空正是它还没初始化的证据），我们驱动的可能是
一个尚未接上音源的元素，`audio.play()` 自然无声。

**验证方向**：把 `claimEarly` 里的 `decorate(element, acct)` 换回「只藏原生」，
完整替换放回合并那一轮（v13 的做法），看 A 和 C 是否消失。若消失，需要一个折中：
同步只藏、下一帧再建（接受一帧空白），或建好后把 `layout()` 与取样推迟到
`requestAnimationFrame`。

关于 **B（PC 不接管）**，尚未定位。已排除：缓存（`window.__piVoicePlayer` 可确认
版本）、账号解析失败（v13 已加 `verify_credentials` 兜底）。仍待查：

- PC 上 Mastodon 是否根本没有 `<audio>` / `<video>` 元素（可能用 Web Audio +
  `<canvas>` 渲染，元素在 shadow DOM 或延迟创建）；
- `statusOf()` 向上找 `.status` 或 `<article>` 在 PC 布局下是否命中；
- `isOwn()` 依赖状态内存在 `a[href="/@<acct>"]`，PC 单栏视图里链接形态可能不同。

**第一步应该是**：在 PC 上登录后打开 Console，跑
`document.querySelectorAll("audio, video").length`。若为 0，则问题在「找不到
元素」，与后续逻辑全部无关。

---

## 3. 我验证了什么，没验证什么

**验证过（复现环境 `docs/clip-brain/design/flicker-harness/`）**：
节点结构层面的行为——原生容器是否被隐藏、我们的 host 是否存在、resize 监听是否
泄漏、DOM 节点是否增长。加压到每 40ms 重建每一行仍全绿。

**没验证**（这是本轮失误所在）：
**外观与功能**。复现环境只数节点，从不检查柱子有没有画出来、宽度是否合理、点击
播放是否真的出声。三个故障全部落在这半边。

**永远无法在此验证**：真实 Mastodon 的 DOM 结构。复现环境的类名和层级是仿的。
它能证明「不循环、不泄漏、不闪」，证明不了「选择器选对了」。故障 B 大概率就在
这条缝里。

**建议**：给复现环境补上视觉断言——柱子数量 > 0、`wave.clientWidth > 0`、柱子高度
非全零；并且用一个真实可播放的短 MP3 替换 `/silence.mp3`，才能覆盖故障 C。

---

## 4. 代码地图

服务端把五个模块拼成一个 `/files/voice.js`（`voice_widget.py` 末尾组装）：

| 模块 | 行 | 职责 |
|---|---:|---|
| `voice_widget.py` | 763 | 录音键本体 + 组装。**录音链路是好的，别动** |
| `voice_waveform.py` | 177 | 配色、RMS 取样、画柱子、楷体注册 |
| `voice_owner.py` | 79 | 账号解析、`statusOf`、`isOwn` |
| `voice_player.py` | 206 | `decorate`：建 UI、藏原生、驱动 `<audio>` |
| `voice_scan.py` | 129 | observer、合并扫描、`claimEarly`、全局 resize |

改任一模块后 `VOICE_WIDGET_VERSION` 必须 +1，否则浏览器缓存不会更新
（`voice.js` 是 `no-cache` + ETag，改版本号即可，无需 purge）。
Console 里 `window.__piVoicePlayer` 会回当前版本，用它确认浏览器拿到的是哪一版。

---

## 5. 必须守住的约束

改动时以下每条都有测试盯着（`mcp/tests/test_voice_widget.py`，共 174 个）：

- **不移动 Mastodon 拥有的节点**。曾把 `.status__content` 搬到播放器下，React 放
  回去 → observer 触发 → 再搬 → 时间线频闪。播放器插在正文**前面**，谁都不动。
- **不注入 `<style>` 元素**，不用反引号。样式一律 `element.style.*`。
- **只观察 `childList`**。加上 `attributes` 会让自己改样式重新触发自己。
- **rAF 必须配超时兜底**。只用 rAF 时，页面不合成帧就永远不回调，标志卡死，
  observer 从此失效（真机表现：切后台回来播放器再也不出现）。
- **全页面只有一个 resize 监听**。每播放器一个会随滚动无限累积。
- **只接管 Owner 自己的动态**。
- 不 fork Mastodon，不改它的前端源码，不碰它的数据库与媒体卷。

---

## 6. 与本故障无关、确认良好的部分

- 录音 → remux → 上传 → 发布 → 本地转写 → 回填正文：**通的**，Owner 实测过。
- MP3 转码（`voice_media.py`）：Ogg 在 iOS 上放不了，MP3 是唯一同时满足
  「Mastodon 收」与「iOS 能放」的格式，两条源路径都过了实例自身的校验。
- 简体偏置（`transcribe.py` 的 `initial_prompt`）。
- 楷体三层栈与自托管字体。**注意** `assets/fonts/*-private.woff2` 是本机授权字体，
  已 gitignore，**绝不可提交**，有测试守着。

---

## 7. 复现环境怎么跑

```powershell
cd D:\AI\PI-Personal-Instance-OS
mcp\.venv\Scripts\python.exe -c "from cmx_mcp.voice_widget import VOICE_WIDGET_JS as J; open(r'docs\clip-brain\design\flicker-harness\voice.js','w',encoding='utf-8').write(J)"
cd docs\clip-brain\design\flicker-harness
py -3 -m http.server 4180 --bind 127.0.0.1
```

指标见该目录 README。**记得先按第 3 节补上视觉断言**，否则会重蹈本轮覆辙。

---

## 8. 部署

改完跑 `mcp\.venv\Scripts\python.exe -m pytest -q`（当前 174 passed），然后：

```powershell
cd D:\AI\PI-Personal-Instance-OS\mcp; .\http-stop.ps1; .\http-start.ps1; .\http-status.ps1
```

**必须提权**：`cmx-mcp-http` 是以管理员身份启动的，普通权限杀不掉。
（若改为普通权限启动，以后就不需要提权了。）
