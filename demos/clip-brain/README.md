# Clip Brain Demo

当前仅验证一个产品切片：24 小时自动焚毁的文本与文件剪贴板。

## 本地打开

在仓库根目录运行：

```powershell
py -3 -m http.server 4173 --directory demos\clip-brain
```

浏览器打开：

```text
http://127.0.0.1:4173/clipboard/
```

不要直接双击 `index.html`。`file://` 下 IndexedDB、下载和剪贴板权限可能表现不一致。

## Demo 能做什么

- 一条记录包含文本、文件或两者；
- 文本上限 10000 个 Unicode 字符；
- 任意文件类型，每条最多 30 个文件、合计最多 1 GiB；
- IndexedDB 保存，24 小时后自动删除；
- 同浏览器标签页通过 BroadcastChannel 同步数据；
- 点击正文复制，点击文件名下载，支持手动删除。

## Demo 不能证明什么

- 不代表生产 `/clipboard` 已部署；
- 不支持跨设备同步；
- 浏览器实际可用空间可能小于 1 GiB，配额不足会明确失败；
- 不包含 AI、搜索、分类、预览、批量下载或永久保存。
