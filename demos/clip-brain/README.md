# Clip Brain

CMX 同域下的临时剪贴板：条目默认 24 小时自动焚毁，★ 收藏的条目不焚毁。
当前分支 `feat/clip-brain-backend`，未部署、未开 PR、禁止合并。

范围与边界见 [`../../docs/clip-brain/V1_SCOPE.md`](../../docs/clip-brain/V1_SCOPE.md)
与 [`../../docs/clip-brain/PRODUCT_BOUNDARY.md`](../../docs/clip-brain/PRODUCT_BOUNDARY.md)。

## 两种运行模式

**正式模式** —— 后端为事实源，跨设备同步：

```text
https://<private-domain>/clipboard/
```

由 Nginx 同源代理到 `cmx-mcp-http` 的 `/clipboard-api/*`，复用当前 Mastodon 登录态。

**本地开发模式** —— 仍走 IndexedDB adapter，不需要后端：

```powershell
py -3 -m http.server 4173 --bind 127.0.0.1 --directory demos\clip-brain
```

```text
http://127.0.0.1:4173/clipboard/
```

只有 `127.0.0.1:4173` 会切到本地 adapter；其余地址一律走后端。
不要直接双击 `index.html`：`file://` 下 IndexedDB、剪贴板和下载权限表现不一致。

## 当前功能

- 一条记录包含文本、文件或两者，两者不能同时为空；
- 文本上限 10000 个 Unicode 字符；每条最多 20 个文件；单条合计严格小于 1 GiB；
- 账户总量上限 2 GiB（含收藏）；用量超过 1.5 GiB 才显示容量计；
- 默认 24 小时后由服务器焚毁；★ 收藏后不焚毁，取消收藏重新起算 24 小时；
- 顶栏「临 / ★」门牌单击翻面切换视图，同时只显示一个字；
- 手动主题标签与文本/图片类型筛选；`Alt + Space` 聚焦关键词检索；
- 文件显示原始文件名（去后缀）与大小，类型由前置徽章表示；
- 保存前移除单个文件，保存后删除单个文件；
- 双击正文即复制，无复制按钮；
- 右上角「N 条」是唯一批量入口，悬停或点击展开：全部复制 / 全部下载 / 全部焚毁；
- 焚毁均为按钮内二次确认，不调用浏览器原生确认框；
- 无分页，条目区单一滚动，每条文件列表独立滚动；
- 单条与多选 ZIP 仍在浏览器本地生成：不满 256 MiB 走普通下载，超过走本地流式直存；
  服务器不参与压缩，也不保存 ZIP；
- 亮 / 暗两套主题，滚动条隐形，无 transition / animation。

## 设计稿

```text
demos/clip-brain/design/mockup.html
```

视觉参考，不是上线代码，不受 300 行停止线约束。

## 自动测试

需要 Python 3 与 Node.js：

```powershell
py -3 -m unittest discover -s demos\clip-brain\tests -p "test_*.py" -v
```

后端测试另行运行：

```powershell
Set-Location mcp
.\.venv\Scripts\python.exe -m pytest -q
Set-Location ..
```

前端契约覆盖：300 行停止线、JS 语法、无分页 DOM、批量入口与三个动作、门牌单字、
站点标识单面、亮暗令牌齐备且批量胶囊两态不同色、检索框无文案、卡片无日期、
滚动边界、无 `window.confirm` / transition / animation、正式模式不以 IndexedDB 为事实源、
真实 ZIP 与 CRC、危险文件名清理、256 MiB 分流、1 GiB 严格拒绝、批量删除只提交显式 ID。

## 不能证明什么

- 自动测试通过不代表生产 `/clipboard/` 已部署；
- 浏览器实际可用空间可能低于服务端配额，直存失败必须明确报错；
- iOS Safari 大包直存、真机跨设备同步与解压体验仍需 Owner 人工验收；
- 不包含公网分享、二维码、AI、语义检索、自动分类、预览或无上限永久保存。
