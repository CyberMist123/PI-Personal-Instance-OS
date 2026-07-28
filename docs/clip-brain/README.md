# Clip Brain 文档入口

## 当前状态

`demo/clip-brain-v0` 是隔离产品 Demo，未部署、未开 PR、禁止合并。

正式目标路径只记录为：

```text
https://<private-domain>/clipboard
```

本地 Demo：

```text
http://127.0.0.1:4173/clipboard/
```

仓库文档不再重复写真实公网地址。真实地址只应存在于未提交的私人运行配置中，并在项目收官时按 `CLAUDE.md` 做隐私审计。

## 阅读顺序

1. [`PRODUCT_BOUNDARY.md`](./PRODUCT_BOUNDARY.md)：Clip Brain 是什么、明确不是什么。
2. [`DEMO_SCOPE.md`](./DEMO_SCOPE.md)：当前实现、停止线和验收范围。
3. [`../../demos/clip-brain/README.md`](../../demos/clip-brain/README.md)：本地运行与自动测试。

## 事实边界

本文档不修改 `PROJECT.md` 的当前生产事实。Demo 通过 Owner 体验验收、Claude 只读测试并形成正式架构决策之前，不进入主线状态表。
