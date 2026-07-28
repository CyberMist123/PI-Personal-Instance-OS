# Clip Brain 产品边界

## 一句话定义

Clip Brain 是与 CMX/Mastodon 并列的临时剪贴板页面，用于短期搬运文本和文件；每条记录在创建 24 小时后自动删除。

## 与 CMX 的关系

- 同一站点，目标路径为 `/clipboard`；
- Mastodon 根路径继续承载时间线和嘟文；
- Clip Brain 内容不创建 status，不进入时间线；
- 右上角图标只做页面导航，不让一个标签页强制切换另一个标签页的页面；
- 两边未来可以共用登录态，但 Demo 不实现鉴权或后端。

## 当前数据边界

Demo 仅使用浏览器 IndexedDB：

```text
clip entry
├─ id
├─ text (0..10000 Unicode code points)
├─ files (0..30 arbitrary file blobs)
├─ createdAt
└─ expiresAt = createdAt + 24h
```

每条文件总量硬上限为 1 GiB。浏览器配额可能更低；配额不足必须失败，不得只显示成功。

## 明确不是

- 不是 NAS 或永久云盘；
- 不是 CMX 时间线附件区；
- 不是 AI 任务中枢；
- 不是知识库、搜索引擎或自动分类器；
- 不是文件预览器、编辑器或版本管理器。

## 代码边界

- 页面结构、视觉、存储、交互分文件；
- 任一前端文件接近 300 行先拆分；
- Demo 不修改 Mastodon、MCP、Nginx、compose 或生产数据；
- 未经 Owner 体验验收，不得开合并 PR。
