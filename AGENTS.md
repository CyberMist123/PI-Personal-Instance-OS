# PI OS agent notes

开始任务时，以 `AI_HANDOFF.md` 作为当前项目事实入口；只读取与任务相关的代码和文档，不从头重建整套架构理解。

容易出错的边界：不得改写已投入使用的 `LOCAL_DOMAIN` 或 secrets；不得运行 `docker compose down -v`；不得提交本地数据、密钥、媒体、日志和备份。

任务完成后，若需求、功能、边界、架构、接口名、运维流程或当前状态发生变化，必须执行 `skills/project-doc-sync/SKILL.md`。不要新建重复的状态或交接文档。
