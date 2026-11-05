# ReviewForge Agent 开发指南

## 当前开发状态

- `fd15297` 小样本再测仍未通过：43 完整响应、472815 已知 tokens、0 未知/在途，1 Case、0 持久 verdict、0 区域完成；操作者停止文件已排空在途并保留 partial 报告。确认仅新增 3 个搜索与 1 个 diff 导航就改变 Case revision，丢弃已取得但未落盘的核验结果并使主责失效；新 profile 修复先完整校验引用，再将导航保留在来源库/审计而非语义 Case 集合。完成回执另逐项指出未知/导航引用，来源门禁不变。真实新绑定待验收，**完整 PR 尚未运行**。v4.2 累计 **36675242 已知 tokens、217 请求未知**，非账单。[记录](docs/v4.2-delivery-plan.md#133-版本回执修复后核验导航触发候选失效)。

- `30c67d3` 参数修复后小样本在第 5 个 Reviewer 响应、39755 已知 tokens 时交付一个符合样例描述的候选，但未获独立 verdict；55 完整响应后因候选回执覆盖工作项 revision 的接口错误人工停止：567686 已知 tokens、0 未知/在途、0 区域完成。现显式区分 `work_item_revision`/`draft_revision`，仅投影最新判断并提供 `read_decision_history` 回读；相关专项 **63 passed**，真实新绑定待验收。完整 PR 门槛仍未通过。v4.2 累计 **36202427 已知 tokens、217 请求未知**，非账单。[记录](docs/v4.2-delivery-plan.md#132-参数校验修复后出现候选交付仍未通过)。

- `982fc03` 首轮小范围真实对照未通过：同一缺陷样本基线 11 完整响应/186585 tokens，新循环 6 完整响应/61111 tokens，均 0 Case、0 区域完成，请求间隙人工停止，无新增未知或在途；本地分别只完成 10/5 批审计。新循环暴露 `find_references(query=...)` 缺 `symbol` 被误记工具不可用，现已补模型可见 JSON Schema 的嵌套参数校验并锁定依赖，需新绑定复测。**尚未运行完整 PR**。v4.2 合计 **35634741 已知 tokens、217 请求未知**，非账单；原诊断不改写，不计算节省比例。[记录](docs/v4.2-delivery-plan.md#131-首轮小范围真实诊断尚未通过)。

- 2026-11-05 用户授权两名 GPT-6.1 Terra / xhigh 实现交付方案前两阶段。可选 `REVIEWFORGE_V42_DECISION_CYCLE=decision-cycle-1` 绑定单一 `review_step`、持久问题/判断/来源 outbox、当前问题原文投影、候选即时核验和完整配置依赖，保留现有责任分组。完整后端 **4165 passed、1 skipped**；随后仅补全模型可见的完成动作嵌套格式，相关专项 **42 passed**，lint/format/spec-check/严格裁判离线检查通过。真实小范围 A/B 尚待完成，**通过后才跑完整 PR**；旧运行、legacy 默认和 GitHub 零投递保持。跨租约且原 exchange 未完整落盘、来源 in-flight 未知仍 partial，整合非最终动作沿用逐项事务。[实施与门槛](docs/v4.2-delivery-plan.md#13-2026-11-05-首个实现与验收记录)。

- `5527278` 同 PR 空状态诊断人工停止：**12612418 已知 tokens**、7 次失败用量未知、76 完整响应、0 Case/草稿、0/24 完成、3 未核实导航，裁判未运行；候选修复未实际触发。确认同请求固定源码重叠，后续可选 `REVIEWFORGE_V42_FRONTIER_PROJECTION=source-line-union-1` 的完整后端 **4124 passed、1 skipped**，质量与成本均未验收。[记录](docs/v4.2-architecture.md#1922-候选恢复策略真实诊断与中断记录)。v4.2 开发累计 **35387045 已知 tokens、217 请求未知**，非供应商账单。

- 可选 `REVIEWFORGE_V42_CANDIDATE_DRAFT=candidate-draft-1` 为每次可解析的候选提交保留独立纠错尝试；错误目标须显式处理，Case 晋升与草稿处置事务一致，未知关联保守阻断。需 `v42-units-6` 和 `shared-1`；旧 checkpoint、默认路径不变。完整后端 **4084 passed、1 skipped**；真实质量和成本未验收，同请求重叠源码仍未去重。详见 [设计 19.20](docs/v4.2-architecture.md#1920-可恢复的候选提交尝试)。

- 2026-11-03 14:46，中转的小型流式推理成功，实报 3193 tokens；随后启动 `6bc8eda` 的 Cal.com #14740 空状态实验评测。16:11 +08:00 因确认候选纠错丢失的交付缺口，已在请求间隙人工停止：73 个完整响应、7952142 已知 tokens，5 次失败用量未知、无在途，0/24 完成、1 Case 尚未核验、0 评论，裁判未运行；另有 3 个不同候选因不存在的证据 ID 被拒并从后续当前上下文消失。单次输入还确认 48205 字符源码页重叠；两项缺口、完整原账本和后续设计见 [设计 19.19](docs/v4.2-architecture.md#1919-中转恢复后的运行中诊断)。不得把该中断诊断视为完整质量结果。
- v4 在 `dev` 开发；`main` 保留现有生产版本与自动部署。未经验收不向 `main` 推送 v4。
- v4.2 可选 `REVIEWFORGE_V42_UNIT_PLAN=single-owner-1` 与 `REVIEWFORGE_V42_SHARED_INVESTIGATION=shared-1` 分别启用单主责和运行绑定的共享调查；共享笔记仅供导航，跨单元反证使相关结论失效并重新独立核验。新运行绑定 Git LF 行语义，旧 checkpoint 不升级；未知写入中断保留 partial，生产挂载硬链接能力未验证。`a8ce20d` 同 PR 旧诊断人工停止，815881 已知 tokens、61 次失败用量未知、0/24 完成、0 Case；`d70bb49` 新诊断及同绑定续跑均 partial，累计 20 次失败用量未知、0 完整响应。2026-11-03 03:31 单次独立小型推理请求返回 HTTP 530、用量未知，随后恢复情况见设计 19.19。新路径真实质量及成本未验收，详见 [设计 19.18](docs/v4.2-architecture.md#1918-明确问题的委派与交付)。
- 独立 `review_v42` 新运行使用 `v42-units-6`：固定区域有唯一主责，共享原始来源和不可信导航，另有逐项跨单元整合与独立核验；正文送达审计不能证明模型理解或发现全部关联，真实 PR 与 holdout 未验收。实验性 `REVIEWFORGE_V42_QUESTION_ASSISTANT=question-delivery-1` 需同时启用 `shared-1`，允许主责委派具体问题，由独立租约和来源送达回执约束；回答仍是不可信导航。新运行绑定细粒度问题输入，旧 checkpoint 不升级；未知写入保持 partial，生产默认仍为 legacy。详见 [v4.2 设计](docs/v4.2-architecture.md#1918-明确问题的委派与交付)。
- `5826b79` 同 PR 复测因 4 次候选提交使用模型自造 ID、接口按未知更新拒绝且反馈不清而停止；2149178 已知 tokens、5 失败及 1 中断请求未知、0 持久 Case/0 评论，未判分。新 ID 契约已修正，可选公平调度仍需真实验收。v4.2 开发诊断累计 14003411 已知 tokens、123 请求未知，不含历史协议，非供应商账单。[记录](docs/v4.2-architecture.md#1916-候选交付失败与可选公平调度)。
- 用户已授权统一使用 `dev`，替代旧任务卡的 `hp/<卡号>` 分支约定。
- 新流水线事实源：[hypothesis-pipeline-spec.md](docs/hypothesis-pipeline-spec.md)。进度与待验收项：[v4-development.md](docs/v4-development.md)。
- 默认仍是 `legacy`；`shadow` 与 `hypothesis` 是尚待真实基准验收的 v4 路径。下述 Planner/Reviewer/Verifier/Commenter 描述适用于 legacy。
- v4.2 使用独立 `review_v42` 内核（[设计与实施](docs/v4.2-architecture.md)），不升级协议 1–13 checkpoint。用户于 2026-11-02 授权三名 GPT-6.1 Terra / xhigh 编码；状态/证据、连续 Reviewer、独立核验分别实现，主代理负责真实入口和基准集成。共享当前工作板与完整证据分离，审计历史不自动注入；固定 merge-base/head 原始字节生成责任范围；外部 Issue 保留自身版本与采集时间。新模式现为显式开启、本地报告与 outbox，默认不变，不向 GitHub 投递。离线机制验证不等于质量/成本验收，单 PR 诊断、消融及未见集的结果须分别记录。
- `c148c72` 新免费模型诊断及同绑定续跑累计 4225839 已知 tokens、35 个失败及 1 个中断请求用量未知，31 个完整响应仍 0 Case、0 评论、0/24 区域完成；输入累计至 897515 字符。原 partial 报告不含续跑，完整物理账本和中断说明保留。新 `v42-stateful-2` 主 Reviewer 每个来源批次持久化当前问题与可回读来源导航，下轮只携带当前工作板、完整精简来源目录及最新完整工具交换；原文和可见历史可审计回读但笔记不能作为证据。独立 QuestionAssistant/Verifier 会话不在本次裁剪范围；旧运行不升级。完整后端合计 3400 passed、1 skipped；真实新策略尚待验收，详见设计 19.6。
- `ff9f32a` 同 PR 新状态契约首测 partial：15 个完整响应、241589 已知 tokens、无未知，0 Case/0 评论，裁判未运行；输入最大 62616 字符，但无交付不能宣称成本或质量改善。审计证明旧式单动作循环检测将新策略下合法的证据回读误判为停滞，导致下一批调用中途退出；已改为完整批次失败检测并澄清每批一次状态，完整后端 3412 passed、1 skipped，真实复测待完成。本次记录和工作板保持原样，详见设计 19.7。
- `54804a9` 同 PR 续测因确认共享状态覆盖而停止：43 个完整响应、671017 已知 tokens，另 2 个失败及 1 个中断请求未知；0 Case/0 评论，裁判未运行。真实审计中焦点切换时空观察与问题数组覆盖了尚未解决的共享线索，另有多个读取批次因未附状态被拒。新 `v42-workspace-3` 将未关闭问题/带引用观察持久化为增量导航条目，正常读取不强制状态，支持跨 scope 查询和逐步记录审查范围；笔记不作为独立证据。完整后端 3497 passed、1 skipped，真实质量/成本待验收。完整证据目录超上下文时明确 partial，模型侧目录分页尚未接入；详见设计 19.8。
- `5bbead0` 同 PR 测试经 29 个完整响应后停止：530871 已知 tokens、另 1 个失败及 1 个中断请求未知，0 Case/0 评论，裁判未运行。审计证实只保留上一批原文会使跨批待比较源码互相挤出，模型反复读取；另有不存在的引用被误登记为永久工具故障。新 `v42-frontier-4` 按来源身份去重并保留可显式释放的原文工作集，不存在的引用返回可纠正参数错误，真实来源损坏仍 partial；完整后端 3542 passed、1 skipped。上下文超限自动分页尚未接入，真实质量与成本未验收，详见设计 19.9。
- `ff80bb1` 同 PR 诊断经 9 个完整响应后停止：469811 已知 tokens、另 2 个失败及 1 个中断请求未知，5 条未核实导航项、0 Case/0 评论，裁判未运行。6 份搜索导航反复注入同一批 314 条跳过路径，原始正文共 94105 bytes；搜索 complete=false，不可当作无匹配证明。新 `v42-frontier-5` 将经过来源校验的旧搜索回执转为按查询/页/完整性可追溯的索引及显式分页，最新工具回执和原文完整保留，diff/source 不按导航标记一概丢弃。完整后端 3582 passed、1 skipped，真实质量与成本未验收，详见设计 19.10。
- `3fcb3e1` 同 PR 自然结束为 partial：32 个完整响应、2205279 已知 tokens，46 个失败请求用量未知；0 Case、0/24 区域回执、0 评论，最后一次逻辑请求 10 次失败，裁判未运行。共享空间出现 2 条未核实导航；实际只有一个 Reviewer 负责全 PR，源码工作集不断增长。该版未达到交付与成本目标，不能宣称优于 v3；详见设计 19.11。三名代理正实现新 `v42-units-6`：明确唯一主责任、跨单元独立整合及可查询共享来源；完整性、隔离、旧 checkpoint 不变均须验收，详见 19.12。
- v4.2 `957f6d4` 在新控制台配置 `deepseek-ai/deepseek-v4.1-flash` 上实测 Cal.com #14740：partial、215805 已知 tokens、另 6 个失败调用用量未知、0 候选/0 评论，裁判未运行；最后同输入连续三次达到约 120 秒超时。已补 v4.2 原始 SSE 聚合传输，旧默认与 Anthropic 路径保留，完整后端 3297 passed、1 skipped。定向传输成功不等于完整 PR 或质量验收，详见方案实施记录。
- 2026-11-02 用户说明当前为免费中转站，要求充分等待响应并重试；本轮时延仅记录，不作为架构优劣指标，重点评估 token、完整性与质量。临时网络失败可使用同一冻结绑定、持久会话和证据续跑；全量尝试与未知用量保留，不从头重复已完成工作，不重新采样已冻结的语义判定。
- v4.2 流式复测暴露控制台 default profile 的4096输出上限被继承，出现全部输出用于推理而 `finish_reason=length` 的真实响应。已修正 OpenAI 兼容新角色省略旧 profile 容量，由实际供应商采用默认值，模型/温度/thinking不变且缓存隔离。独立中转设置为900秒网络空闲窗口、10次可重试请求、接受900秒Retry-After；旧协议仍1–3次，所有新参数绑定新运行。MiniMax Anthropic SDK自身4096默认仍是独立未验收边界，不能套用其他供应商容量。
- 控制台随后改为`deepseek-v4.1-flash-free`，同输入探针首尝试返回4条有效工具调用；完整诊断暴露正常EOF窗口、未变更diff、已知不存在路径被误当工具不可用，以及Reviewer缺陷审查任务契约表达不足。已按完整固定快照明确区分query导航与真正unavailable，精确EOF原文及实际行号保留；query不能成为独立事实/契约/反证，真实故障仍partial并给Question ID与原因。全套合计3360 passed、1 skipped，真实新版本仍须验收；旧0b5292b诊断1635356已知tokens、6失败+1中断未知、0Case/0评论，未判分。此前本轮开发含独立探针共3026559已知tokens、23未知，详见方案19.4–19.5，不能把旧冻结运行改写为已修正。
- 新 v4 运行使用内部协议 13，继承协议 10 的完成驱动策略（2026-10-30 用户授权取消人为执行配额）：token 只计量；发现、调查、来源还原与证明核验不设固定请求/取证步骤/格式修复/候选/引用/结果字数或字节数/评论数量上限。取证工具在任务未完成期间保持可用，原生结果可伴随说明；DeepSeek 使用自动工具选择，不再为强制调用统一关闭思考。重复同一证据状态和失败回执才识别为无进展循环，明确未完成，不假装无问题。已完整校验的案例结论及证据摘要冻结后只能重验；未通过引用/来源校验的交付可按实际反馈纠正。旧协议 1–12 保留原语义与冻结计数。上下文种子、分页和分组目标不等于审查停止条件；显式请求的源码完整保存为多个原有大小的 Observation。来源真实性、固定版本/案例绑定、独立发布核验、网络故障退避及机器资源保护保留。新评测不设置 external_request_budget_scope、authorized_budget 或 --token-budget，仍计量实际用量；历史绑定额度不解除，新运行使用新目录。工作循环跑通不等于质量验收。
- 协议 11 在现有 ledger 增加共享调查工作空间（[方案](docs/shared-review-workspace-plan.md)）：按任务/路径查询最新进展、未解问题及原始证据目录；历史笔记保留审计，笔记仅供导航，不能成为证据或发布依据。发现与调查共用运行级只读缓存，原始结果完整返回；证据绑定 run/head/workspace，消费者重建本地引用。before 来源不能伪装成 head Observation；独立来源还原与 ProofGate 不接收共享笔记。新行为不回写旧 checkpoint。 协议 11 同时实现发现回执之间交错调查、显式原子子案例、按盲审缺口分段补证、固定仓库文档与部分 Issue 快照，以及 Docker 隔离重放已有 Python unittest / Node tests。时间戳和临时镜像 ID 不构成新证据进展；缺环境/依赖明确 unavailable，非零测试退出仍需独立证明。
- 协议 11 首次 Cal.com 诊断实报 1948659 tokens、0/18 完成、0 候选，另 1 中断请求未知，未通过质量验收。协议 12 在发现取证动作强制携带可见结构化进展，默认聚合查询且共享导入幂等，统一发现引用和增量提交语义；不回放 provider reasoning，不修改旧诊断记录。
- 协议 12 Cal.com 诊断实报 1678486 tokens、47 完整响应、0/18 完成、0 候选，另 1 中断请求未知。同响应后一个工具的局部进展覆盖前一个工具已解决事项，已停止并保留原账本。协议 13 采用有序持久进展日志，保留局部快照与明确纠正的来历；笔记仍是不可信导航，独立核验不接收。离线和同 PR 复测完成前不宣称质量或成本改善。
- 协议 13 已推 dev `8605923`，完整后端 3144 passed、1 skipped，远端 CI 成功。同一 Cal.com #14740 空状态实测 9832616 tokens、12/18 完成、5 候选（调查 confirmed 3 / refuted 1 / unknown 1），末尾 6 个 HTTP 402 阻断剩余发现和独立来源还原；partial、0 评论、裁判未运行。三轮已知合计 13459761 tokens，另 8 请求用量未知，评测进程已退出。真实质量与成本未验收；有序历史保持了进展，但自动 journal/累计证据元数据输入过大，任务交付仍慢。不得把已实现共享空间或调查 confirmed 视为 v3 优势/可发布结论。完整记录与后续设计建议见方案末节。
- 历史有限额度评测的资金事实源是 `.reviewforge/eval-private/current-v4-grant-lineage-root.txt` 指向的完整子账本：当前 500000 grant 实报 367064、历史未知用量保守预占 129989、无在途，保守剩余 2947；旧独立余额 12797 / 35820 / 2662 / 3673 / 2721 未合并。不得从旧父目录重新复制 62455/19066 剩余额度。`34336aa` 的三案定向回归交付完成，独立来源还原后确认项因缺行为契约被 ProofGate 预检判为 UNKNOWN，零评论；完整空状态 PR/holdout 仍未验收。

## Agent 架构

### Planner (Conductor)
- 角色：决策者，每轮一次 LLM 调用
- 输入：PR diff summary + 已有 findings
- 输出：TaskProposal[]（哪些文件需要哪些 Reviewer 审查）
- 不执行任何工具，只做决策

### Reviewer (Operative)
- 角色：执行者，无状态单任务
- 每个 Reviewer 专注一个维度（security/performance/style/architecture）
- 通过 `BaseReviewer.execute_agentic` 的工具循环运行（默认开启，`REVIEWFORGE_AGENTIC_DEFAULT`），
  可调用 `read_file`/`search_code`/`read_diff`/`read_reference`；亦可降级为 `execute_singleshot`
- 由 `core/scheduler.py` 的 Scheduler 按优先级 + 并发上限调度
- 输出写回 State Store

### Verifier (Auditor)
- 角色：验证者，纯逻辑（无 LLM）
- 输入：Reviewer 输出的 candidate findings
- 输出：去重/合并重复（`engine/verifier.py`），再交给 Dynamic Calibrator 做对抗式确认

### Commenter (Analyst)
- 角色：综合者
- 输入：confirmed findings
- 输出：格式化的 GitHub review comment

## 新增 Reviewer 步骤

1. 在 `core/specs.py` 的 `build_registry()` 中注册 AgentSpec
2. 在 `skills/` 下创建对应 Skill 目录
3. 在 `engine/reviewers.py` 中实现 Reviewer 类
4. 在 `engine/prompt.py` 中添加 prompt section
5. 运行 `spec-check` 验证

## Skill 编写规范

```
skills/{skill_name}/
  SKILL.md              # 主文件（注入 prompt）
  references/           # 深层参考（按需读取）
    rules.md
    examples.md
```

SKILL.md 格式：
- YAML frontmatter：name, description, category, reviewer_type
- Body：审查规则、判断标准、示例（好/坏对比）
- 写法：方法论 > 具体代码，告诉 agent 怎么判断而不是给模板
