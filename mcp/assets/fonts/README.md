# 自托管楷体

`lxgw-wenkai-gb2312.woff2` 是 [LXGW WenKai 霞鹜文楷](https://github.com/lxgw/LxgwWenKai)
v1.522 `LXGWWenKai-Regular.ttf` 的子集，许可证为 SIL Open Font License 1.1
（见 `LXGW-WenKai-OFL.txt`，随字体一并分发是该许可证的要求）。

## 为什么要自托管

Windows 和 macOS 自带楷体，iOS 和 Android 不带，会退到平台衬线体——笔形不是楷。
唯一的解是自己提供一份。

## 为什么是子集

完整字面 25.6 MB，为一行转写背这个体积不合理。子集取 GB2312 的 6763 个汉字
加拉丁字母与中西文标点，覆盖日常口语几乎全部，压到 **1.62 MB**；更生僻的字
退回平台衬线体，和桌面以外本来就会发生的降级是同一种。

重新生成：见 `scripts/` 之外的 fontTools `subset` 用法，charset 由 Python 内置
`gb2312` 编解码器穷举得到，不依赖任何外部字表。

## 缓存

由 `cmx-mcp-http` 在 `/files/fonts/<name>` 提供，`immutable` + 一年 max-age。
文件名即版本：换字体就换文件名，不要原地覆盖。
