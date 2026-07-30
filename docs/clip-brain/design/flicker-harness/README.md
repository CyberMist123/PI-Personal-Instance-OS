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

## 必须为真的指标

先在 Console 跑 `window.__harnessFreeze()` 停掉churn，再读**静置后**的数字：
「乱流中是否正确」和「乱流停了是否收敛」是两个问题，只有静置的答案能区分
**滞后**和**泄漏**。

| 指标 | 期望 | 违反时说明 |
|---|---|---|
| `natives` | 恒为 **0** | Mastodon 原生播放器被画出来过，就是可见的闪烁 |
| `gap`（`hidden - hosts`） | 静置后 **0** | 藏了原生却没插自己的。churn 中非 0 是滞后，静置后非 0 才是漏 |
| `nonFlat` | 静置后 **= hosts** | 有播放器的柱子全等高，说明真实振幅从没到过 |
| `bars` / `waveWidth` | **> 20** / **> 0** | 柱子没画出来，或波形容器没有宽度 |
| `listeners` | 恒为 **1** | 每个播放器一个 resize 监听会随滚动无限累积 |
| DOM 节点数 | 不单调增长 | 有节点泄漏 |

音频是页面自己合成的一段 1.6 秒 WAV（三段响声＋间隙），走 Blob URL。**不要换回
404 或一段静音**：采样失败和从没采样长得一模一样——都是等高柱子，v14 的波形回归
就是这样躲过去的。

`voice.js` 由 loader 带时间戳加载。浏览器会开开心心继续用上一版，读起来就像「改了
没用」——这个坑本身踩过。

## 这里抓到过的真 bug

1. **rAF 死锁**。合并扫描只用 `requestAnimationFrame` 时，页面不合成帧就永远不
   回调，in-flight 标志卡住，observer 从此失效。真机表现是「切后台回来播放器没
   了、再也不回来」。现在 rAF 与 `setTimeout` 双路，谁先到谁生效。
2. **resize 监听泄漏**。每次 decorate 加一个，而虚拟列表滚动时不断重挂载。
3. **只藏 vs 全做**。v14 把整个替换搬进 observer 回调同步执行，为的是原生播放器
   一帧都不露；结果那一刻元素还没选定音源，波形采样被跳过且不再重试，驱动一个
   React 还没接好的播放器也没声音。现在回调里**只藏**，构建回到合并那一轮：
   `natives` 仍恒为 0，`gap` 在 churn 中短暂非 0、静置后归 0。

## 它证明不了什么

DOM 结构是仿的，不是 Mastodon 真实产物。类名与层级若与真实实例不符，这里全绿也
可能真机失败。它能证明的是**行为**：循环、泄漏、闪烁、波形有没有画出来；不能证明
**选择器**对不对——PC 端从没被接管过这个故障就卡在这条缝里，真机上用
`window.__piVoiceDebug()` 查，不要在这里查。
