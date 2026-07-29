# Clip Brain 文档入口

## 当前状态

`feat/clip-brain-backend` 是当前工作分支，基线 `demo/clip-brain-site-link`。
未部署、未开 PR、禁止合并，`main` 不移动。

2026-07-29 按 Owner 决策完成一次文档同步：v0 Demo 的分页、纯本地 IndexedDB、
「只有两个批量动作」「每条 30 个文件」等表述已作废；搜索、手动主题标签、★ 收藏
由「明确不做」改为 v1 正式范围。原 `DEMO_SCOPE.md` 已被 `V1_SCOPE.md` 取代并删除。

正式目标路径只记录为：

```text
https://<private-domain>/clipboard
```

本地开发 Demo（保留 IndexedDB adapter）：

```text
http://127.0.0.1:4173/clipboard/
```

仓库文档不重复写真实公网地址。真实地址只应存在于未提交的私人运行配置中，
并在项目收官时按 `CLAUDE.md` 做隐私审计。

## 阅读顺序

1. [`PRODUCT_BOUNDARY.md`](./PRODUCT_BOUNDARY.md)：Clip Brain 是什么、明确不是什么。
2. [`V1_SCOPE.md`](./V1_SCOPE.md)：v1 功能清单、代码停止线与验收范围。
3. [`CODEX_BACKEND_HANDOFF.md`](./CODEX_BACKEND_HANDOFF.md)：后端施工单与分阶段交付。
4. [`CLAUDE_REVIEW.md`](./CLAUDE_REVIEW.md)：只读验收单。
5. [`../../demos/clip-brain/README.md`](../../demos/clip-brain/README.md)：本地运行与自动测试。

设计稿位于 `demos/clip-brain/design/`，是视觉参考，不是上线代码，不受 300 行停止线约束。

## 关联 Issue

- [#27](https://github.com/CyberMist123/PI-Personal-Instance-OS/issues/27) 后端同步、跨设备联调与风格收口（当前）
- [#24](https://github.com/CyberMist123/PI-Personal-Instance-OS/issues/24) Clip Brain 产品定位与范围
- [#26](https://github.com/CyberMist123/PI-Personal-Instance-OS/issues/26) 分享码 / 二维码：已关闭，明确取消

## 事实边界

本文档不修改 `PROJECT.md` 的当前生产事实。Clip Brain 通过 Owner 体验验收、
只读测试并形成正式架构决策之前，不进入主线状态表。
