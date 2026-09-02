# Hypothesis Pipeline 执行清单

> 给执行 agent 的任务卡。规格细节一律以 [hypothesis-pipeline-spec.md](hypothesis-pipeline-spec.md)（下称 SPEC）为准，本清单只负责"做什么、什么顺序、怎么算完成"。
> 诊断背景见 [architecture-diagnosis-20260901.md](architecture-diagnosis-20260901.md)。

## 通用规则（每张任务卡都适用）

- 开工前完整读 SPEC §0 的 9 条须知，违反任何一条的 PR 会被打回。
- 每张卡一个分支，命名 `hp/<卡号>-<短名>`（如 `hp/T2-workspace`），一张卡一个 PR，PR 描述里写：实现了 SPEC 哪一节、偏离了什么、为什么。
- 每个 PR 合并前必须通过：
  ```
  cd backend
  uv run ruff check . && uv run ruff format --check .
  uv run reviewforge spec-check
  uv run pytest -q
  ```
- `REVIEWFORGE_PIPELINE=legacy`（默认值）下,任何已有测试的行为不得改变。给旧行为"加分支"可以，改旧分支不行。
- 卡内列出的"产出文件"是完整清单；需要动清单之外的文件时，只允许最小接线改动，并在 PR 描述里逐个说明。
- 遇到 SPEC 没覆盖的决策：停下，在 PR 描述写明问题与两个候选方案，不要自行填空白（尤其不要发明新的过滤规则或阈值）。

## 依赖图

```
T1 ─┐
T2 ─┼─→ T4 ─→ T5 ─→ ┌ T6 ┐
T3 ─┘               │ T7 ├─→ T8 ─→ T9 ─→ T10 ─→ T11 ─→ T12
（T1–T3 可并行）     └────┘（T6、T7 可并行）
```

---

## Phase 0 — 测量修正（不改审查逻辑）

### T1 输出语言可配 + 固定评测拆分
- SPEC：§4.9（语言部分）、§6 Phase 0、§7。
- 产出：
  - `core/config.py`：`output_language` 字段（env `REVIEWFORGE_OUTPUT_LANGUAGE`），legacy 默认 `zh-CN`。
  - `engine/language.py`：`resolve_output_language(state, config) -> str`（`auto` 判定：PR body + diff 注释 CJK 占比 >30% → `zh-CN`，否则 `en`）。
  - `engine/prompt.py`：`_language()` 按解析结果生成（`en` 时要求英文 message/suggestion）。
  - `.reviewforge/benchmarks/martian_runner.py`：`--output-language` 参数透传到 config。
  - `backend/src/reviewforge/eval/workloads/dev10.json`、`holdout40.json`：从 `.reviewforge/benchmarks/minimax-m3-final-v3-full50-20260730/workload.json` 拆分；dev10 = `main-d48d88b-representative10-20260803/workload.json` 的 10 个 PR，holdout40 = 其余 40 个。
  - `backend/src/reviewforge/eval/paired_report.py`：输入两份 `judged-strict.json`，输出每 PR 配对差值、win/tie/loss、P10 F1、worst F1（JSON + markdown 表）。
  - 测试：`tests/test_language.py`、`tests/test_paired_report.py`。
- 完成标准：`REVIEWFORGE_OUTPUT_LANGUAGE=en` 时 reviewer 提示词无"必须使用中文"字样；未设置时与现状逐字节一致；两个 workload 文件合计恰好 50 个 PR 且无重叠。

### T1V（验证跑，人工/运维触发，可与 T2–T3 并行）
- dev10 上 legacy+`en` 跑一遍，与既有中文结果用 `paired_report.py` 配对，差值写进 `docs/benchmark.md` 附录"语言混淆量化"。不设通过门槛，只记录。

## Phase 1 — 确定性地基

### T2 PRHeadWorkspace
- SPEC：§4.1。
- 产出：`tools/workspace.py`；`tests/fixtures/workspace_repo/`（构造迷你多语言仓库 + 打包脚本）；`tests/test_workspace.py`。
- 接线：`tools/gateway.py` 的 `_read_file`/`_search_code` 在 `mode != legacy` 时走 workspace（legacy 分支不动）。
- 完成标准：SPEC §4.1"测试"小节 5 个场景全覆盖；tarball 失败时 `source="api-fallback"` 且 `grep` 返回空不抛异常。

### T3 ContextPack
- SPEC：§4.2。依赖 T2 的 fixture（可基于 T2 分支开发）。
- 产出：`engine/context_pack.py`；`tests/test_context_pack.py`。
- 完成标准：7 种 kind 各至少 1 个用例取到正确片段；同输入两次 `render_all` 字节相同；超限时按 risk 保序截断且 `truncated_kinds` 正确。

### T4 账本 + 配置 + 骨架
- SPEC：§4.3、§4.9、§4.10（前两个事件）、§4.8（骨架部分）。依赖 T1–T3。
- 产出：
  - `engine/hypothesis.py`（Mechanism/Hypothesis/Observation/Ledger + to_dict/from_dict）。
  - `core/database.py`：`hypotheses`、`observations` 表，append-only 写入；`core/state.py`：`StateStore.ledger` 字段。
  - `core/config.py`：`PipelineV4Config` 全量字段；`reviewforge.yaml` 的 `pipeline_v4:` 段。
  - `engine/model_router.py`：`ROLE_MAP` 加 4 个新 agent 名。
  - `engine/pipeline_v4.py`：`run_hypothesis_pipeline` 骨架，`shadow` 下执行 workspace → changeset → pack 并发 `workspace.built`/`context_pack.built` 事件；`Orchestrator.run()` 开头按 `mode` 分派。
  - 测试：`tests/test_hypothesis_ledger.py`（重点：`upsert` 的 identity 合并、并发 upsert 不丢 sites、resume 重建）、`tests/test_pipeline_v4_skeleton.py`。
- 完成标准：`mode=shadow` 时 legacy 发布结果与 `mode=legacy` 完全一致（用现有集成测试断言）；telemetry 两事件字段齐全。

### T4V（Phase 1 验收跑）
- dev10 在 shadow 下跑通。抽查 keycloak#36880、grafana#97529、sentry#80168 的 ContextPack：`base_class`/`sibling`/`lock_usage` 片段必须包含对应 golden 所需代码（SPEC §6 Phase 1），把片段贴进验收记录。不达标 → 回到 T3 调收集规则，禁止进入 Phase 2。

## Phase 2 — 假设生成 + 调查（shadow）

### T5 假设生成器
- SPEC：§4.4、§5.1。依赖 T4。
- 产出：`engine/prompts_v4/generator.md`；`engine/hypothesis_generator.py`（分块、schema 校验、excerpt 锚定校验、账本 upsert）；`tests/test_hypothesis_generator.py`（mock LLM：合法输出、excerpt 不匹配丢弃、超量截断、解析失败→unresolved_units、分块共享账本）。
- 完成标准：SPEC §4.4 全部校验规则有对应测试；无任何"重试找问题"逻辑。

### T6 Lens
- SPEC：§4.5。依赖 T5。
- 产出：`engine/lenses.py`（触发规则 + 执行）；`engine/prompts_v4/lens.md`（generator.md 的变体模板）；`tests/test_lenses.py`（每条触发规则一正一反用例；≥3 个 lens 时按 risk 取前 3）。

### T7 调查员
- SPEC：§4.6、§5.2。依赖 T4（可与 T5/T6 并行开发，联调依赖 T5）。
- 产出：`engine/prompts_v4/investigator.md`；`engine/investigator.py`（预算函数、工具绑定 workspace、observation 自动记录、verdict 校验、并发执行器）；`tests/test_investigator.py`（SPEC §4.6"测试"小节 5 项 + provider 错误→unknown/retryable）。
- 完成标准：不存在任何路径能在无 success-observation 的情况下产出 `refuted` 或 `confirmed`。

### T8 Shadow 联调 + 账本召回评测
- SPEC：§6 Phase 2。依赖 T5–T7。
- 产出：`pipeline_v4.py` 接通 4–7 步（detectors 种子 → 生成 → lens → 调查），账本落库；`eval/ledger_recall.py`；telemetry `hypothesis.generated`/`lens.selected`/`investigation.*`。
- 完成标准（dev10、MiniMax-M3 单模型、`en`）：
  - 账本召回 ≥ legacy 发布召回 + 10pp；
  - REFUTED 中含 golden ≤ 2；
  - 每 PR token ≤ 250k。
  - 不达标：只允许调 prompts_v4 与 PipelineV4Config 限额，每轮消融记录在 `docs/tuning-log.md`；调不动就停下报告，不得动校验规则放水。

## Phase 3 — 发布切换

### T9 Editor + 发布
- SPEC：§4.7、§5.3。依赖 T8 达标。
- 产出：`engine/prompts_v4/editor.md`；`engine/editor.py`（确定性预处理、LLM 调用、schema 校验、fallback 模板）；接入 `_post_comments` 与 review body `<details>`；`tests/test_editor.py`（排序确定性、上限与溢出、fallback、path:line 必须来自 sites）。

### T10 RunHealth / telemetry / resume
- SPEC：§4.8 后半、§4.10。依赖 T9。
- 产出：`RunHealth` 新 stage；`pipeline_v4.completed` 事件；`evaluation_telemetry` schema 扩展；resume 测试（OPEN 重调查、CONFIRMED 不重做、outbox 不重复发）。
- 完成标准：SPEC §4.8 的 `completed/partial` 规则有属性测试。

### T11 Holdout 验收跑
- SPEC：§6 Phase 3、§7。依赖 T10。
- 产出：`eval/duplicate_audit.py`（`(path, mechanism)` 簇内 >1 条计重复）；dev10 与 holdout40 各 2 遍（`mode=hypothesis`），`paired_report.py` 配对；验收报告提交为 `docs/benchmark-hypothesis-v1.md`。
- 通过门槛：holdout40 上 F1 配对提升且 P10/worst 不回退；重复 FP ≤ 3；零评论含 golden PR ≤ 4。Qodo 对比仅列参考行。
- **holdout 结果不得回流调参**；不通过则回 Phase 2 在 dev10 消融后重跑（holdout 重跑次数记录在报告里）。

## Phase 4 — 默认切换与退役

### T12 切默认 + 退役
- SPEC：§6 Phase 4、§8。依赖 T11 通过。
- 产出：默认 `mode=hypothesis`；控制台显示当前 mode；按 §8 清单整体删除旧路径（一张卡一个 PR 不适用于此卡：拆成"切默认"和"退役"两个 PR，退役 PR 必须一次性完整删除，不留半死代码）；更新 README / benchmark.md / v3-architecture.md。
- 完成标准：全测试通过；`grep -r "look harder"` 无命中；退役模块无残余 import。

---

## 汇报模板（每张卡完成时贴在 PR 描述）

```
卡号：T_
SPEC 章节：§_
偏离 SPEC 的点及原因：（无则写"无"）
新增/修改文件：
测试：uv run pytest -q 输出末行
基准（T8/T11 适用）：paired_report 摘要表
待 Owner 决策的问题：（无则写"无"）
```
