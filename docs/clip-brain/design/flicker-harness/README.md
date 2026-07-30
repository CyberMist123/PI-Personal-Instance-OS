# 语音条频闪复现环境

一个假的 Mastodon 时间线，用来在**不登录真实实例**的前提下量测注入式播放器的
渲染行为。频闪那一轮 bug 就是在这里找到的——真机上只能看到「在抖」，看不到为什么。

## 跑

```powershell
cd D:\AI\PI-Personal-Instance-OS
mcp\.venv\Scripts\python.exe -c "from cmx_mcp.voice_widget import VOICE_WIDGET_JS as J; open(r'docs\clip-brain\design\flicker-harness\voice.js','w',encoding='utf-8').write(J)"
cd docs\clip-brain\design\flicker-harness
py -3 -m http.server 4180 --bind 127.0.0.1
```

打开 `http://127.0.0.1:4180/index.html`，右上角实时显示指标；`window.__harness`
可在 Console 里取到同一组数字。`voice.js` 是生成物，已忽略，不进 Git。

## 它模拟什么

- **音频与文字交错**的时间线（Owner 报告频闪的正是这种）
- **每 250ms 重建部分行**——虚拟滚动列表在行进出视口时就是这样重挂载的
- **每 700ms 顶部插入新动态**——另一个报告的触发路径

## 四个必须为真的指标

| 指标 | 期望 | 违反时说明 |
|---|---|---|
| `natives` | 恒为 **0** | Mastodon 原生播放器被画出来过，就是可见的闪烁 |
| `audios - hosts` | 恒为 **0** | 藏了原生却没插进自己的，会留一段空白 |
| `listeners` | 恒为 **1** | 每个播放器一个 resize 监听会随滚动无限累积 |
| DOM 节点数 | 不单调增长 | 有节点泄漏 |

## 这里抓到过的真 bug

1. **rAF 死锁**。合并扫描只用 `requestAnimationFrame` 时，页面不合成帧就永远不
   回调，in-flight 标志卡住，observer 从此失效。真机表现是「切后台回来播放器没
   了、再也不回来」。现在 rAF 与 `setTimeout` 双路，谁先到谁生效。
2. **resize 监听泄漏**。每次 decorate 加一个，而虚拟列表滚动时不断重挂载。
3. **绘制前才来得及**。只在 observer 回调里「藏原生、稍后再插自己的」，中间那段
   仍会露出空白；必须**在同一个同步回调里把整个替换做完**，因为该回调发生在
   变更之后、绘制之前。

## 它证明不了什么

DOM 结构是仿的，不是 Mastodon 真实产物。类名与层级若与真实实例不符，这里全绿也
可能真机失败。它能证明的是**行为**：循环、泄漏、闪烁；不能证明**选择器**对不对。
