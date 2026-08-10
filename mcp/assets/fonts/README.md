# 语音转写用的楷体

转写正文用楷体显示。字体栈分三层，按优先级：

| 优先级 | family | 来源 | 进 Git？ |
|---|---|---|---|
| 1 | 系统楷体 | Windows / macOS 自带 | — |
| 2 | `PI Kai Local` | 本机授权楷体的子集 | **否** |
| 3 | `LXGW WenKai GB` | 开源霞鹜文楷屏幕阅读版 | 是 |

桌面命中第 1 层，一个字节都不下载。手机没有系统楷体，才会去拿第 2 或第 3 层。

## 第 3 层：开源，随仓库分发

`lxgw-wenkai-screen-gb2312.woff2` 是
[LXGW WenKai Screen 霞鹜文楷屏幕阅读版](https://github.com/lxgw/LxgwWenKai-Screen)
v1.522 `LXGWWenKaiGBScreen.ttf` 的子集，SIL Open Font License 1.1
（见 `LXGW-WenKai-OFL.txt`，随字体分发是该许可证的要求）。

选 Screen 而非 Regular：Regular 字重在 PC 和 Android 屏幕上偏细，Screen 版把
Medium 转成了 Regular 并对齐了屏幕度量。选 GB 版：国标字形，和转写已偏向简体一致。

26.0 MB → **1.89 MB**。

## 第 2 层：本机授权字体，绝不上传

`kai-private.woff2` **不在 Git 里**（`mcp/.gitignore` 忽略 `assets/fonts/*-private.woff2`）。
本机有、GitHub 上没有，**这个差异是刻意的，不是漏传**。

原因是授权：**所有「正宗」楷体都禁止网页嵌入。**
[方正楷体](https://www.foundertype.com/index.php/About/powerbus.html)的免费授权明确排除
嵌入式场景；[中易楷体 SimKai](https://zh.wikipedia.org/wiki/%E4%B8%AD%E6%98%93%E6%A5%B7%E4%BD%93)
版权属北京中易中标，随 Windows 授权、不含再分发权；华康楷书只在阿里平台免费。
把它们推上公开仓库属于分发行为，有法律风险。

需要如实记一笔：即使不上传，通过公网域名把字体送进浏览器，严格意义上仍是分发。
本实例是单人私有、只服务 Owner 自己的设备，这是 Owner 知情后的取舍。

### 重新生成

```powershell
cd D:\AI\PI-Personal-Instance-OS
mcp\.venv\Scripts\python.exe mcp\scripts\subset_kai.py C:\Windows\Fonts\simkai.ttf mcp\assets\fonts\kai-private.woff2
```

换别的授权楷体同理，但**文件名必须以 `-private.woff2` 结尾**，否则 gitignore 兜不住。

没有这个文件时一切照常：字体栈直接落到第 3 层，`FontFace.load()` 静默失败，
不报错、不影响播放。所以别人克隆这个仓库也能正常跑，只是看到的是开源楷体。

## 缓存与命名

由 `cmx-mcp-http` 在 `/files/fonts/<name>` 提供，`immutable` + 一年 max-age。
**文件名即版本**：换字体就换文件名，不要原地覆盖，否则浏览器一年内不会重新拉取。
