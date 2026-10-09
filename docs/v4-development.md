# ReviewForge v4 开发记录

更新时间：2026-10-09。开发分支：`dev`。生产分支：`main@00c667556c88241d72d96f24430c7c16b4add24e`。

## 目标与边界

沿用已批准的 [假设流水线规格](hypothesis-pipeline-spec.md)，改善上下文交付、共享假设调查与最终表达。重用 v3 的仓库访问、模型路由、数据库与发布基础设施。

用户已授权清理旧分支、PR 与 Issue，并把有用 PR 合入 `dev`。因此后续统一在 `dev` 开发，替代旧任务卡的逐卡分支约定。`main` 的部署 workflow、部署脚本和默认审查配置保持原样。新建的 `dev-ci.yml` 只运行检查和测试。

## 旧工作处理结果

| 工作 | 处理 |
|---|---|
| PR [123](https://github.com/Wayne0607/ReviewForge/pull/123)–[130](https://github.com/Wayne0607/ReviewForge/pull/130) | 审查后 retarget 至 dev 并合入：生成器、lens、调查员、shadow、编辑器、健康状态/恢复、重复审计实现 |
| PR [114](https://github.com/Wayne0607/ReviewForge/pull/114)、[117](https://github.com/Wayne0607/ReviewForge/pull/117) | 关闭旧植入测试 PR |
| Issue 118 | 导出后删除旧测试报告 |
| 旧分支 | 删除 19 个远程分支、79 个本地分支，保留 main/dev；8 个旧工作树改为 detached HEAD，目录与文件保留 |

清理前已生成并验证完整 Git bundle：`.reviewforge/archives/v4-reset-20261009/repository-before-cleanup.bundle`，另存 refs、工作树状态、PR body/reviews/comments、Issue body/comments。归档在本机的忽略目录中；用户已有 `outputs/` 未改动。

合入只表示复用实现，**不表示任务卡对应的真实评测门槛已经通过**。导入基线测试为 1395 项通过。

## 第一轮修正

| 设计假设与偏差 | 修正 | 验证 |
|---|---|---|
| 生成器应分析 PR 引入的行为变化；原实现只渲染变更后的行，删除的 guard/lock 不可见 | 同时提供对应的原始 before/after hunk 与独立 RIGHT 坐标；删除行不会变成评论锚点 | 删除保护条件的回归用例；保留原有 excerpt 锚定测试 |
| schema 失败应 unresolved；数组类型错误原先会被视为没有假设 | 校验顶层数组类型，一次格式修复后仍失败记 unresolved；拒绝当前 block 之外的 unit_id | 错误数组与伪造 unit_id 回归 |
| 调查结论必须引用实际证据；原实现用子串识别 not_found、把 glob/空路径及无关引用算作 diff 外证据 | 精确识别无结果标记；从被引用的原文与具体工具结果定位证据来源 | 源码包含“not found”、diff 内/外搜索、无关外部文件用例 |
| 新增位置必须可发布；原 additional_sites 未校验 excerpt | 对新增 site 执行 RIGHT 行与逐字 excerpt 校验 | 真位置、伪造代码、错误行、diff 外位置 |
| 恢复调查应保留新的 observation；原计数每次归零会与旧记录冲突 | 新 observation 从已保存编号之后分配；同一工具与参数最多执行两次 | 恢复编号、重复调用预算 |
| 编辑器应接收确认依据，只表达已选择的问题；原输入没有 trigger/impact/observations 与 inline/summary 分配 | 交付完整事实和确定性分配；仅允许 confirmed inline IDs；合并评论列出全部 sites，锚点可选其中任一位置 | 证据输入、状态隔离、跨位置合并与精确行号用例 |
| 编辑器失败或遗漏不能删除 confirmed；原局部有效响应和 fallback 会丢弃问题/摘要 | 对未覆盖簇逐个模板补齐；始终保留 confirmed 溢出摘要；模板按输出语言生成并引用工具原文 | 部分响应、畸形 JSON、纯摘要配置、fallback 证据 |
| 语义编译器依赖 ContextEngine 的 impact_manifest；原独立流水线未提供，测试预填掩盖缺口 | 新 run 在编译前经绑定 PR head 的 gateway 建影响清单；shadow 复用已有清单 | 从真实本地快照文件出发，验证 callee/base_class/sibling/lock_usage 进入 ContextPack |

这些变更仅影响 v4 路径，未改变 legacy 的审查逻辑或阈值。

## 验证记录

- 新增 21 项确定性回归测试；先复现缺陷，再修正实现。
- `ruff check`、`ruff format --check` 与本地 `spec-check` 通过。spec-check 的凭据检查使用进程内占位值，它不调用 GitHub/LLM，不能证明真实服务连通。
- Windows/Python 3.12 全量 pytest：`1416 passed, 6 warnings`；新增 GitHub Ubuntu/Python 3.12 的 dev CI，执行结果以该 workflow 的检查记录为准。这里的测试是正确性验证，不是审查质量测评。6 项 warning 为既有依赖弃用提示。
- `main` 与 `origin/main` 保持上文 SHA；生产部署文件与清理前一致。

## 下一阶段与发布门槛

本轮尚未跑真实 LLM dev10/holdout40，因此没有 v4 的 P/R/F1，也不能宣称已超过 v3 或 Qodo。当前默认仍为 `pipeline_v4.mode: legacy`。

先处理接线审计发现的剩余问题：

1. 各 v4 角色接入 token tracking，替换 pipeline 中的占位 token telemetry，并落实调查 token 预算。
2. 生成/调查逐阶段持久化；恢复及投递接入现有 outbox，验证中断重跑不会重复评论。现有恢复测试主要覆盖账本状态跳过，不足以证明投递幂等。
3. workspace 全部不可用时应失败；核查新路径异常与数据库 run 状态的一致性。
4. 调查工具的 API-fallback 异步读取与符号语言处理；大型 PR 分块和截断覆盖的真实边界。
5. 复核 detector 种子的确认语义与未映射类别，避免未经验证的命中直接成为强证据问题。

然后按 SPEC 运行：相同模型、英文输出，dev10 上 legacy 对照与 shadow 调查漏斗；抽查 keycloak#36880、grafana#97529、sentry#80168 的实际 ContextPack。达标后才进行 holdout40 两轮配对验收；holdout 不用于调参。记录账本召回、调查误杀、发布遗漏、重复误报与 token 消耗。通过质量与运行可靠性门槛后，才把 v4 推入 `main` 并切换生产默认。
