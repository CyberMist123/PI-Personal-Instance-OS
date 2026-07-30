# 语音条：当前故障与交接单

**状态：v15 已部署，波形回来了（Owner 真机确认）。v16 已写好未部署，修的是「点了没
声音、旁边弹出一个播放器」——根因是 Mastodon 的画中画。**
最后更新 2026-07-31。分支 `feat/clip-brain-backend`。

---

## 0. 先止血

**v15 之后不再需要止血**。若仍要临时停掉播放器：在
`mcp/src/cmx_mcp/voice_widget.py` 的 `boot()` 里注释掉 `watchTimeline(state);`，
重启 `cmx-mcp-http`（需提权）。录音、上传、转写、回填这条链一直是好的，不受影响。

---

## 1. 三个故障（Owner 在真机实测）

| # | 现象 | 平台 | v13 | v15 |
|---|---|---|---|---|
| A | 播放器和柱子都在，但**柱子全是等高占位条**，没有真实波形 | 手机 | 有波形 | **v15 已修，Owner 真机确认波形回来了** |
| B | **仍是 Mastodon 原生黑框播放器**，完全没被接管 | PC | 也坏 | **未修**，已配 `__piVoiceDebug()` |
| C | **播不出声音** | 手机/PC | 能播 | **v16 修，根因确诊：画中画** |

### 故障 C 的真正根因（2026-07-31 确诊）

Owner 的两句话把它钉死了：「点击播放依然没有声音，但**弹出来的原 CMX 是有声音的**」，
以及截图里那个虚线「恢复」框。那是 Mastodon 的**画中画占位符**
（`components/picture_in_picture_placeholder.tsx`，文案 id 是
`picture_in_picture.restore`）。

`features/audio/index.tsx` 的 ref 回调：

```text
if (audioRef.current && !audioRef.current.paused && c === null) {
  deployPictureInPicture('audio', { src, currentTime, ... });
}
```

**元素在播放中被 React 卸载 → 部署画中画。** 而我们一直在驱动的正是 Mastodon 自己
那个 `<audio>`：一按播放它就 not paused，时间线一重挂载（虚拟列表滚动、新动态到达
都会）就把声音搬进角落里的弹出播放器，原位留下「恢复」占位。所以：条子是哑的、声音
在别处、两种形式横跳——全是同一件事。

v16 不再借用它的元素：在我们自己的 host 里建一个自己的 `<audio>`，同源同 src。
**Mastodon 那个永远保持 paused**，上面那个分支就永远不会成立。代价是媒体会话归我们
所有，所以补了两条：全局只允许一个在响（`playOnly`），以及 host 被 React 丢掉时先把
它自己的元素暂停——脱离文档的媒体元素是不会自己停的。

### v15 改了什么

1. **observer 回调里只藏，不建**（`claimQuietly`）。v14 把整个替换搬进同步回调，
   为的是原生播放器一帧都不露；代价是那一刻元素还没选定音源。完整构建回到合并
   那一轮，也就是 v13 的时机。
2. **波形采样不再是一次性的**。原来 `if (audio.currentSrc && window.AudioContext)`
   在 decorate 里判一次，而 decorate 每个元素只跑一次——这一次判空就永远没有第二
   次机会，正是 A 的根因。现在音源按 `currentSrc → src → <source>` 依次问，并在
   `loadedmetadata` / `canplay` / `play` 上重试，上限 3 次。
3. **藏法从 `display:none` 改为裁剪**（绝对定位 + 1px + `clip` + `opacity:0`）。
   不渲染的媒体元素在 iOS 上不播，这是 C 最像的机制。裁剪同样什么都看不见，但元素
   留在渲染树里。
4. **`play()` 的 rejection 被接住并 `warn`**。它是 reject 而不是 throw，未处理的
   rejection 正是「点了没反应」查不出原因的方式。
5. `window.__piVoiceDebug()` —— 一次 Console 调用回报整条链断在哪。

### 复现环境静置后实测（v16）

```text
natives 0    gap 0    hosts 7/7    nonFlat 7/7
bars 89      waveWidth 446         listeners 1
ourAudioExists true   oursPlaying true   anyNativePlaying false
```

最后一行是 v16 的核心断言：**7 个 Mastodon 元素没有一个进入播放状态**，画中画那个
分支因此永远不成立。另测：连点两个播放键，先按的自动停，`simultaneouslyAudible` 为
1，两个按钮图标各自正确。

柱高 5–29px、7 个不同值，与合成音频的「三段响声＋间隙」对得上；点播放键
`paused=false`、`currentTime` 递增、`readyState=4`，且此时 wrapper 的
`display` 仍是 `grid`（在渲染树里）而 `data-pi-voice-hidden="1"`。

`gap`（藏了但还没建）在 churn 中会到 3–4，静置后归 0：这是 v15 用「一帧空白」换回
波形和声音的代价，是**滞后不是泄漏**。真实时间线只在滚动时重挂载，窗口应远小于
复现环境里每 250ms 重建三分之一行的压力。若真机上这段空白看得见，下一步是在
`claimQuietly` 里插一个等高占位，而不是把构建搬回同步回调。

---

## 2. 根因

v14 把整个替换动作搬进了 MutationObserver 回调里同步执行（`claimEarly` →
`decorate`），目的是让原生播放器一帧都不被画出来。**这个改动太早了。**

回调发生在 DOM 变更之后、绘制之前。此时：

### 故障 A —— 已确诊，v15 已修

Owner 复述：「声音图在，但没有变化的波形了」。柱子渲染了，但**全是等高的占位条**
（`new Array(count).fill(0.4)`），说明真实取样那段从未执行。

`voice_player.py` 里取样被这个条件挡住：

```js
if (audio.currentSrc && window.AudioContext) { fetch(audio.currentSrc)... }
```

`decorate` 现在在 observer 回调里同步跑，那一刻 React 还没把 `src` 赋到元素上
（或 `preload="none"` 尚未解析），`currentSrc` 是空字符串，整段被跳过，**而且再
也没有第二次机会**——`decorate` 对同一元素只跑一次。

v15 两条一起做了：音源按 `currentSrc → src → <source>` 依次问（`mediaSource`），
取样拆成 `sampleWaveform()` 并挂到 `loadedmetadata` / `canplay` / `play` 上重试，
上限 3 次——不再依赖「此刻 src 已就绪」这个恰好成立的假设。

`wave.clientWidth` 为 0 时 `layout()` 退到 `|| 300` 的问题仍在，但已不再是 A 的
成因：`loadedmetadata` 会重算一次，静置后复现环境量到 `waveWidth 446`、89 根柱子。

### 故障 C —— 同一根因的可能延伸，v15 已换掉这个机制

同步替换时 `original.style.display = "none"` 把容器藏掉。如果 Mastodon 的组件在
这之后才完成初始化（`currentSrc` 为空正是它还没初始化的证据），我们驱动的可能是
一个尚未接上音源的元素，`audio.play()` 自然无声。

v15 走的就是这个验证方向：`claimEarly` 只调 `claimQuietly`（只藏），完整替换回到
合并那一轮，并接受「一帧空白」这个折中（见第 1 节的 `gap`）。同时把藏法从
`display:none` 换成裁剪，元素留在渲染树里——iOS 不播不渲染的媒体元素，这是 C 最
像的机制。桌面 Chromium 已验证点播放出声，iOS 仍待真机。

关于 **B（PC 不接管）**，仍未定位。已排除：缓存（`window.__piVoicePlayer` 可确认
版本）、账号解析失败（v13 已加 `verify_credentials` 兜底）。

**v15 起不用再靠猜。** 在 PC 上登录后打开 Console 跑：

```js
window.__piVoiceDebug()
```

回报形如：

```js
{ version: "v15", acct: "owner", media: 3, inStatus: 3, own: 3, claimed: 3,
  samples: [{ tag: "AUDIO", src: "https://…/media/…", status: "status", acctLinks: 2 }] }
```

照这个顺序读，第一个塌成 0 的就是断点：

| 字段 | 为 0 的含义 | 下一步 |
|---|---|---|
| `version` 是 `undefined` | 脚本根本没加载 | 查注入与 CSP，与播放器逻辑无关 |
| `acct` 为空 | 账号没解析出来 | `verify_credentials` 兜底也失败了，查登录态 token |
| `media` | PC 上根本没有 `<audio>` / `<video>` | Mastodon 可能用 canvas 渲染或元素在 shadow DOM / 延迟创建，后面全部逻辑都无关 |
| `inStatus` | 找到了元素但 `statusOf()` 向上没命中 | `.status` / `<article>` 在 PC 布局下不适用，看 `samples[].status` |
| `own` | 命中了状态但 `isOwn()` 判否 | 看 `samples[].acctLinks`：为 0 说明状态里没有 `a[href*="/@"]`，PC 单栏视图链接形态不同 |
| `claimed` | 前面都对却没接管 | 才轮到看 `decorate` 本身 |

在复现环境里查这个没有意义：那里的 DOM 是手写仿造的，`statusOf` / `isOwn` 必然命中。

---

## 3. 我验证了什么，没验证什么

**验证过（复现环境 `docs/clip-brain/design/flicker-harness/`）**：
节点结构层面的行为——原生容器是否被隐藏、我们的 host 是否存在、resize 监听是否
泄漏、DOM 节点是否增长。加压到每 40ms 重建每一行仍全绿。

**上一轮没验证、v15 已补上**：外观与功能。复现环境现在自己合成一段真实可解码的
WAV（三段响声＋间隙，Blob URL），并断言柱子数量、波形容器宽度、以及**有多少个
播放器的柱高不是全等的**。加上 `window.__harnessFreeze()`，可以在静置状态读数，
区分滞后与泄漏。上面第 1 节的数字就是这么来的。

用真音频这一步不是可选的：**采样失败和从没采样长得一模一样**——都是等高柱子。
之前指向一个 404，所以 A 在这里躲了整整一轮。

**仍然无法在此验证**：

- 真实 Mastodon 的 DOM 结构。复现环境的类名和层级是仿的，`statusOf` / `isOwn`
  在这里必然命中。故障 B 只能在真机用 `__piVoiceDebug()` 查。
- **iOS Safari 的播放行为**。C 的根因（`display:none` 让元素脱离渲染树）已经换
  掉，桌面 Chromium 上点播放确实出声，但 iOS 的媒体策略只有 iPhone 能回答。
- 真实音频文件的解码耗时与体积。合成的 1.6 秒 WAV 解得飞快，真实录音是 MP3、可能
  几分钟长，`decodeAudioData` 的开销是另一回事。

---

## 4. 代码地图

服务端把五个模块拼成一个 `/files/voice.js`（`voice_widget.py` 末尾组装）：

| 模块 | 行 | 职责 |
|---|---:|---|
| `voice_widget.py` | 770 | 录音键本体 + 组装。**录音链路是好的，别动** |
| `voice_waveform.py` | 181 | 配色、RMS 取样、画柱子、楷体注册 |
| `voice_owner.py` | 79 | 账号解析、`statusOf`、`isOwn` |
| `voice_player.py` | 337 | `mediaSource`、`hideNativeChrome`、`claimQuietly`、`playOnly`、`decorate` |
| `voice_scan.py` | 185 | observer、合并扫描、`claimEarly`、`__piVoiceDebug`、全局 resize |

改任一模块后 `VOICE_WIDGET_VERSION` 必须 +1，否则浏览器缓存不会更新
（`voice.js` 是 `no-cache` + ETag，改版本号即可，无需 purge）。
Console 里 `window.__piVoicePlayer` 会回当前版本，用它确认浏览器拿到的是哪一版。

---

## 5. 必须守住的约束

改动时以下每条都有测试盯着（`mcp/tests/test_voice_widget.py`；全量 178 passed）：

- **不移动 Mastodon 拥有的节点**。曾把 `.status__content` 搬到播放器下，React 放
  回去 → observer 触发 → 再搬 → 时间线频闪。播放器插在正文**前面**，谁都不动。
- **不注入 `<style>` 元素**，不用反引号。样式一律 `element.style.*`。
- **只观察 `childList`**。加上 `attributes` 会让自己改样式重新触发自己。
- **rAF 必须配超时兜底**。只用 rAF 时，页面不合成帧就永远不回调，标志卡死，
  observer 从此失效（真机表现：切后台回来播放器再也不出现）。
- **全页面只有一个 resize 监听**。每播放器一个会随滚动无限累积。
- **只接管 Owner 自己的动态**。
- **observer 回调里只藏，不建**。v15 起，构建必须留在合并那一轮：那个回调发生得
  太早，元素还没选定音源。
- **波形采样必须可重试**。一次性的 `currentSrc` 判空就是 A 的根因。
- **绝不驱动 Mastodon 自己的 `<audio>`**。它一旦 not paused 又被卸载，画中画就会把
  声音搬走（C 的根因）。播放、seek、事件全部走我们自己那个带 `OWN_MARK` 的元素。
- **全局只允许一个在响**。脱离文档的媒体元素不会自己停。
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

指标见该目录 README。视觉断言与 `window.__harnessFreeze()` 已经在里面了；音频由页面
自己合成，**不要换回 404 或静音**——采样失败和从没采样长得一模一样。

---

## 8. 部署

改完跑 `mcp\.venv\Scripts\python.exe -m pytest -q`（当前 178 passed），然后：

```powershell
cd D:\AI\PI-Personal-Instance-OS\mcp; .\http-stop.ps1; .\http-start.ps1; .\http-status.ps1
```

**必须提权**：`cmx-mcp-http` 是以管理员身份启动的，普通权限杀不掉。
（若改为普通权限启动，以后就不需要提权了。）
