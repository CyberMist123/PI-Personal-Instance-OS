# PI OS Agent Entry

开始任何工作前：

1. 先读根目录 [`PROJECT.md`](./PROJECT.md)。它是需求、架构、接口、数据边界、进度和下一步的唯一当前事实入口。
2. 再只读与当前任务直接相关的代码和详细文档；不要为了接手而重新扫描整个仓库。
3. 明确当前事项属于：`计划中`、`已实现/未实测` 或 `已运行验证`。

完成工作后：

1. 若改变了需求、边界、架构、接口名、数据所有权、运行流程或进度，执行 [`skills/project-doc-sync/SKILL.md`](./skills/project-doc-sync/SKILL.md)。
2. 先原地更新 `PROJECT.md`，再更新受影响的详细文档与当前 Issue。
3. 删除陈旧事实，不新增重复的 `STATUS.md`、`PROJECT_STATE.md`、handoff 副本或第二套架构说明。
4. 没有目标电脑真实输出时，不得声称部署、备份、恢复、域名切换或手机 smoke 已通过。

硬边界：

- `LOCAL_DOMAIN` 永远固定为 `pi.invalid`。
- 当前 `WEB_DOMAIN` 可替换，但切换必须经过 `change-access-domain.ps1` 的 Prepare / Switch / Release 流程。
- 不开启公共联邦。
- CMX 必须同源、相对 API、网页 Session；不注册长期绑定公网域名的 OAuth application。
- AI/MCP 不直连 PostgreSQL，不使用 Owner 管理 Token。
- 不提交 `.env`、`.env.production`、`data/`、`backups/`、`logs/` 或 Cloudflare 凭据。
- 不运行 `docker compose down -v`。
- 失败时只修已证实的失败点，不顺手重构或扩展范围。