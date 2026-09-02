# ReviewForge 架构诊断：为什么超不过 Qodo，以及该怎么改

> 日期：2026-09-01。基线：`main@892de6a`。
> 数据来源：`.reviewforge/benchmarks/minimax-m3-final-v3-full50-20260730/`（固定 50 PR、137 golden、MiniMax-M3 严格裁判）+ 代码走读。
> 本文是诊断与设计提案，不代表任何代码已经实现。

## 0. 一句话结论

当前架构是"**多个互不知情的生成器堆候选，再用多个只看 diff 的过滤器往下删**"。它产出的候选 90% 会被删掉（1675 → 151），留下的 151 条里一半以上是**同一根因的重复报告**；而真正漏掉的 golden 大多需要**读 diff 之外的代码**（调用方、父类、schema、锁的作用域）才能发现——恰恰是唯一贡献 77% 命中的 correctness reviewer 没有的能力。Qodo 赢在结构，不在模型：单一上下文看完整个 PR，输出经过排序的少量结论。

"拉高架构下限"要改的不是再加一层过滤或再加一个 reviewer，而是把范式从 **generate-then-filter** 换成 **shared-state hypothesis → investigate → select**。

## 1. 数据说了什么

### 1.1 漏斗（50 PR 合计）

| 阶段 | 数量 | 说明 |
|---|---:|---|
| Planner 派发任务 | 194 | ≈3.9 / PR |
| Reviewer 直接产出 | 455 | |
| V3 coverage closure 产出 | 1269 | 4758 cell → 选 991 → 每 cell 单发调用，未发现问题时带 "look harder" 重试 |
| 候选总数 | **1675** | ≈33 / PR |
| Calibrator（对抗式，同 diff）| 610 留 / 846 删 | |
| Publication policy pre | 477 留 / 138 删 | |
| Triage（批量）| 43 确认 / 43 删 / 387 需工具 | |
| Gate（每条一个工具循环）| 104 确认 / 280 删 / 30 ungrounded | |
| **发布** | **151** | 65 TP / 87 FP |
| 根因聚类实际合并 | 20 / 2156 | dedup 层几乎不工作 |
| Escalation | 395 次 → 确认 7 | 794 步工具循环换 7 条确认 |
| Cross-PR analyzer | 0 findings | 2434 行代码，50 PR 零产出 |

### 1.2 按 reviewer 拆 TP/FP

| Reviewer | TP | FP | 精度 | Token 占比 | 每 PR 调用次数 | 返回空结果比例 |
|---|---:|---:|---:|---:|---:|---:|
| correctness | **50** | 58 | 46% | 16.6% | 15 | **47%** (356/750) |
| performance | 2 | 17 | 11% | 3.6% | 3.9 | 0 |
| security | 5 | 5 | 50% | **45.2%** | **25.6** | 13% |
| testing | 1 | 4 | 20% | 7.2% | 5.5 | 0 |
| quality(detector) | 5 | 1 | 83% | ~0 | – | – |
| localization | 2 | 0 | 100% | 0.3% | – | – |
| accessibility | 0 | 2 | 0% | 0.3% | – | – |

- correctness 贡献 77% 的 TP，但它是**单发调用、无工具、平均 3.6k token、上限 6 条 findings**，并且 47% 的调用直接返回 `{"findings": []}`。
- security 花掉 45% 的 token（7.5M）换 5 个 TP。16 个 PR 中 security 消耗超过一半预算，其中一半的 PR 没有任何安全类 golden（如 grafana#76186：528k token 给 security，27k 给 correctness，两个 golden 全是 nil-check 回归，0 评论）。
- performance reviewer 精度 11%，是净负资产。

### 1.3 置信度没有区分力

| | 平均 confidence | 分布 |
|---|---:|---|
| TP | 0.93 | 0.8: 11, 0.9: 35, 1.0: 19 |
| FP | 0.91 | 0.7: 2, 0.8: 25, 0.9: 45, 1.0: 15 |

模型自报的 confidence 对真假几乎无区分。但架构中多处以它为开关：escalation 只对 0.4–0.7 触发（几乎没有 finding 落在这个区间）、confidence_threshold 0.5、calibrator 用 Δconfidence>0.2 决定是否进 judge。**这些开关绑在一个噪声信号上。**

### 1.4 误报是什么

逐条读 87 条 FP 原文（`scratchpad/fp_bodies.txt`），粗分三类：

| 类型 | 估计条数 | 例子 |
|---|---:|---|
| **同一根因的重复报告**（严格裁判下每条重复 = 1 FP） | ~45–50 | grafana#80329 `log.Error` 用于非错误：1 TP + **6 FP**（每个调用点一条）；cal.com slots.ts `end` 误用 `slotStartTime`：1 TP + **6 FP**（correctness 与 performance 各报数条）；keycloak#33832 `getBouncyCastleProvider` 返回错误 provider：1 TP + **4 FP**（security 2 条 + correctness 2 条）；sentry#77754 `timezone.now()` 默认值：1 TP + 3 FP |
| 看起来成立但不在 golden 里 | ~20 | cal.com `statusText !== "OK"` 在 HTTP/2 下为空串；sentry OAuth 未设 Accept 头 |
| 琐碎/nit | ~15 | `any()` 用列表推导、注释措辞、aria-label、测试用例名拼写 |

第一类是**结构性**的：9 个 reviewer × N 个 coverage cell 各自独立产出，彼此不知道对方已经报过；后面所有 LLM 过滤层（calibrator/triage/gate）都是**逐条或按文件分批**判断，看不到"这条和另一条是同一件事"；确定性 dedup 又要求同文件、近行号、同 category。跨 reviewer、跨行、跨文件的重复从构造上就无法被消除。

### 1.5 漏报是什么

72 条 FN（`scratchpad/fn_goldens.txt`）中，High/Critical 22 条。典型：

- keycloak#36880（0 评论，727k token）：`findByName(server, client.getId(), server.getId())` 与 `getOrCreateResource` 的 owner 约定不一致——需要读另一个类；feature flag V1/V2 不一致——需要看同类其它方法。
- grafana#97529（0 评论）：`cacheMu` 锁作用域缩小导致重复构建索引 / `TotalDocs` 无锁遍历 map——需要理解锁前后范围。
- sentry#80168：子类没实现父类抽象方法——需要读父类。
- grafana#106778：`RuleActionsButtons` 内部组件仍依赖 ruler rule——跨组件。
- sentry#93824：`SpawnProcess` 不是 `multiprocessing.Process` 子类——需要库知识 + 上下文。

这些是 Qodo 命中而我们为 0 的 PR。Qodo 评论里明确引用了 diff 之外的事实（"in the schema, only the all-resource entry is typed as Clients"）。我们的 correctness reviewer 在这些 PR 上的 completion 是 **11 个 token**（空数组）。

11 个零评论 PR 承担了 29% 的 FN，其中多个 PR 消耗 600k+ token——钱花了，花在了错的 agent 上，然后仅有的几条候选被过滤层删光。

### 1.6 两个测量混淆（不是主因，但要修）

- 提示词强制中文输出（`prompt.py:118` "所有 message、suggestion、reason 字段必须使用中文"），golden 与 Qodo 候选是英文，裁判是 LLM。跨语言匹配必然损失一部分命中。
- 我们的评论平均 370 字符，Qodo 1380 字符（含 issue description / context / fix focus）。裁判做语义匹配时，短评论更难被判定覆盖 golden。

### 1.7 在 10 PR 上调参、在 50 PR 上回归

`main-d48d88b-representative10-20260803/REPORT.md`：稳定基线在同一 10 PR 上 F1 54.55%（Qodo 50.00%），全量 50 PR 上 44.98%（Qodo 50.82%）。git log 近 60 条提交里有约 30 条是发布层的 dedup/gate/rescue/revert。这是典型的在样本上过拟合发布层规则。

## 2. 五个结构性根因

### R1 范式：独立生成器 + 孤立过滤器

- 生成侧：Planner → 9 类 reviewer；V3 ledger 再按 unit × dimension 生成 cell，每个未闭合 cell 单独调一次 reviewer，没发现问题就带 "adversarial retry — look harder" 再调一次（`orchestrator.py:2907-2931`）。这直接激励模型编造。
- 过滤侧：calibrator 系统提示"默认立场：这些发现是错误的"（`calibrator.py:1460`），看的是**同一份 diff**，没有工具；triage 每批 6 条同文件；gate 每条 2 步 / 4000 token。三层 LLM 过滤加起来占 30% token，但都不引入生成阶段没有的信息，只是让同一个模型对同一份 diff 再表态。
- 后果：重复无法合并（它们分别看到每条），真问题被"默认为错"的对抗立场删掉（keycloak#36880：3 条进 gate，全部删除），解析失败即丢弃（`calibrator.py:1425-1439`）。

### R2 预算按角色分配，不按价值

security 是唯一 agentic reviewer（`reviewforge.yaml:99-101`），planner 指南写明"必须派发，如果代码涉及……网络请求"（`reviewer_catalog.py:171-174`），每 PR 平均 25 次调用，工具循环每步重发全部历史（`reviewers.py:311`），成本随步数平方增长。correctness 被注释明确锁在单发模式（"实测 correctness/testing 工具循环成本接近翻倍且总体召回不稳定"），并且被强制把最多 32 个生产文件塞进一个任务（`planner.py:563-597`），diff 上限 36k 字符（`prompt.py:20`），输出上限 6 条（`reviewer_catalog.py:239`）。一个大 PR 的全部正确性审查就是一次 ≤36k 字符、≤6 条结论的单发调用。结果是：能找到 bug 的 agent 没有工具也没有预算，有工具的 agent 找不到 bug。

另外 performance / doc / dependency / accessibility 四个 reviewer 没有任何 SKILL.md（`prompt.py:538-545` 返回 None），10 个 style 类 skill 因 `style_reviewer` 被禁用而从未注入——skills 目录 48KB 里有效的不到一半。

### R3 验证层不获取新证据

floor-v2 文档第 3 节已经指出"多 Agent 不等于独立证据"。代码事实更具体：calibrator 的 adversary 和 evidence_verifier 的 refuter 收到与 reviewer **完全相同**的 diff（`evidence_verifier.py:676,684`），无工具；gate 只有 2 步。所谓"分层验证"是同一模型、同一输入、不同立场的重复投票。

### R4 上下文靠"发现"而不是"交付"

FN 的共性是需要 diff 外的代码。当前架构里 reviewer 拿到的是 Impact Manifest（≤3500 字符）：changed symbols、calls、references 和 candidate_tests 的**名字和路径**，没有任何源码文本（`context_engine.py:453`）。要看调用方或父类的代码必须自己调 `read_file`，而只有 security 有工具循环。即便有工具，`search_code` 走的是 GitHub `/search/code`，索引的是**默认分支而非 PR head**，且只返回路径（`tools/github_api.py:103-112`）。Reviewer 还**看不到 PR 标题和描述**——它们只给 planner（`prompt.py:456-466, 590-600`），reviewer 不知道作者想做什么。

而 `symbol_extractor.py`（1739 行）、`semantic_diff.py`、`wiki_compiler.py` 已经能确定性地算出被改符号的 callers/imports/candidate tests 和符号契约——这些信息只以"清单"形式存在，**没有以代码形式送到 correctness reviewer 面前**。

### R5 优化闭环失真

confidence 无区分力却驱动多个开关；10 PR 调参 50 PR 回归；中文输出对英文裁判；发布层规则层层叠加（6 个 dedup 层、4 个 LLM 过滤层）却合并率 <1%。每次改动都在症状层，每次都要"revert"。

## 3. 和 Qodo 的结构对照

| | ReviewForge | Qodo（从候选形态反推） |
|---|---|---|
| 生成上下文 | 9 reviewer × N cell，各看片段 | 单一上下文看完整 PR |
| 输出数量 | 0 或 5–9（双峰） | 几乎总是 1–3 |
| 去重 | 事后规则 | 一次生成天然无重复 |
| 跨文件事实 | 自行发现（多数 reviewer 无权限） | 评论中直接引用 |
| 评论形态 | 370 字符，中文 | 1380 字符，issue/context/fix focus，英文 |

Qodo 不是模型更强，是把"排序 + 硬上限 + 单上下文"做进了结构。我们 recall 略高说明生成能力不差，差的是**选择**。

## 4. 目标架构：Shared-state Hypothesis Pipeline

核心变化一句话：**每个 PR 只有一份共享的假设账本（Hypothesis Ledger）；所有 LLM 调用要么向账本提交新假设，要么为账本里的某个假设回答一个具体的开放问题；发布是对账本做一次全局排序选择。**

```
Webhook / CLI
  └─ 1. Change Model（确定性）
       PR intent + SemanticChangeSet + Context Pack
       （callers / callees / base class / siblings / tests / schema，绑定 head_sha）
  └─ 2. Hypothesis Generation（1–2 次全 PR 上下文调用）
       输入：完整 diff + Context Pack + 已有假设
       输出：Hypothesis{identity, claim, mechanism, open_question, refutation_condition}
       规则：attach-or-new —— 同 identity 只能补充证据，不能新建
  └─ 3. Investigation（每个假设一个 agentic 调查，预算按 risk × novelty）
       目标：回答 open_question，产出 EvidenceObservation（tool/query/sha/result）
       结果：confirmed / refuted / unknown（三值，unknown 不可发布也不可当 clean）
  └─ 4. Selection & Authoring（一次全局调用）
       输入：所有 confirmed 假设 + 证据
       动作：按根因合并、按 severity × evidence × actionability 排序、软上限 top-k
       输出：仓库语言的评论，每条附证据引用与"同类位置列表"
  └─ 确定性下限：detectors、坐标校验、RunHealth、ReviewResult 三值协议（保留 floor-v2 P0）
```

### 4.1 Change Model + Context Pack（交付上下文，而不是让 agent 去找）

对每个 SemanticUnit，确定性地拉取并打包：

- 被改函数的 callers（`search_code` 精确符号）、callees 的签名；
- 类的父类/接口定义（解决 sentry#80168 一类）；
- 同一文件里同类兄弟方法的对应片段（解决 keycloak#36880 feature flag 不一致、grafana#90045 sibling metrics）；
- 被改符号相关的测试；
- 被改的锁/字段在同文件的所有使用点（解决 grafana#97529、#90939）。

限额：每 unit ≤ N 个片段、每片段 ≤ M 行，按 risk 排序截断。全部带 `repo/sha/path/line`。这一步不调模型，是模型无关下限的一部分。`symbol_extractor.py`、`semantic_diff.py`、`context_engine.py` 可以复用。

### 4.2 假设生成（少量、全局、有身份）

- 一次调用看**整个 PR**（diff + Context Pack），要求输出假设列表，每条带：
  - `identity`：`(unit_id, mechanism_tag)`，mechanism 从有限词表选（wrong-arg / missing-await / lock-scope / contract-mismatch / null-path …）；
  - `claim`（是什么）、`trigger`（怎样触发）、`open_question`（"要确认此事需要知道什么"）、`refutation`（"什么事实能推翻它"）；
  - `sites[]`：同一根因涉及的所有位置（一条假设，多个行号——从源头杜绝 6 条 log.Error）。
- 第二次调用（可选、不同 lens 或不同模型）看到第一次的账本，只能 attach 或 new。
- 专项 lens（security / i18n / a11y）不是常驻 reviewer，而是由 Change Model 的风险信号触发，且同样写入同一账本。
- 不要 "look harder" 重试；没有假设就是没有假设，进入 NO_ISSUE（带检查边界）。

### 4.3 调查（唯一的 agentic 阶段，唯一的过滤阶段）

- 每个假设一个调查员，任务是回答 `open_question`，不是"判断这条 finding 对不对"。
- 调查员有工具，且 Context Pack 已预载；预算 = f(severity, novelty, 已有证据)。
- 输出三值 + observations。unknown 不发布、不算 clean、可重试；provider 错误永远不变成 refuted。
- 这一层取代 calibrator / escalation / triage / gate 四层。少一层，但每一次调用都在**获取新信息**。
- 模型无关：换模型改变调查质量，不改变协议；同模型也比现在强，因为它读了 caller/父类。

### 4.4 选择与写作（全局，一次）

- 输入所有 confirmed 假设，做根因聚类（identity 相同直接合并；跨 identity 由这次调用判断）。
- 排序：severity × evidence_strength × actionability，nit 类降权。
- 软上限：默认 ≤5 条，high-risk 可溢出；余下的以折叠摘要形式附在 review body 里（不丢信息，也不摊薄精度）。
- 语言按仓库/配置（基准用英文）；形态对齐 Qodo：issue / why / where(all sites) / fix。
- 这一步看到全部候选，是消除"跨 reviewer、跨文件重复"的正确位置。

### 4.5 保留的确定性下限

floor-v2 的 P0 组件（ReviewerCatalog、ReviewResult 三值、RunHealth、telemetry、tool probe）都保留，它们解决"真相与健康"问题。但要明确：**它们不改变质量差距**。质量差距由 4.1–4.4 解决。

### 4.6 预期效果与理由

| 问题 | 机制 |
|---|---|
| ~50 条重复 FP | identity + sites[] 在生成时合并；Selection 全局看 |
| 零评论 PR / 跨文件 FN | Context Pack 主动交付 caller/父类/兄弟；调查员有工具 |
| security 45% token | lens 按风险触发，预算按假设分配 |
| confidence 噪声 | 不再用 confidence 做开关；用三值 + 证据强度 |
| 过滤层删真问题 | 调查回答具体问题，而非"默认错误"投票 |
| Token | 估算：Context Pack 一次 + 生成 2 次（各 ~30k）+ 调查 5–10 次（各 ~10–20k）+ 选择 1 次 ≈ 150–250k / PR，低于现在 331k |

## 5. 迁移与验证

1. **先修测量**（一周内可完成）：输出语言可配（基准用英文）；固定 10 PR 为 dev、40 PR 为 holdout；报告 paired per-PR delta 和 P10/worst，不看单次均值。
2. **Shadow 跑 4.1 + 4.2**：只生成假设账本，不发布；离线用裁判对比"账本里有没有 golden"——这直接测量生成阶段召回，把召回问题和选择问题分开。
3. **接 4.4 到现有候选**：即使暂时不改生成，先用一次全局 Selection 调用替换 gate 之后的发布集合，看 FP 中重复部分是否消失。这是最便宜的验证。
4. **接 4.3 替换 calibrator/escalation/triage/gate**，用 kill switch 保留旧路径。
5. 消融顺序：同一模型跑 {旧架构, 新架构}；通过后再做多模型组合。

## 6. 对 architecture-floor-v2 提案的定位

floor-v2 的分析（R3 独立证据、三值协议、RunHealth、pinned workspace）都正确，且已部分落地。但它的 P0/P1 主要修"运行是否可信"，不修"为什么发 6 条同样的评论、为什么 correctness 看不到父类"。两者不冲突：floor-v2 是地板的钢筋，本文是楼层结构。建议顺序：先做第 5 节 1–3 步（成本低、直接针对 F1 差距），再并行推进 floor-v2 的 P1。

## 7. Owner 决策（已于 2026-09-01 定案）

三项决策已按推荐值确定，实施细节见 [hypothesis-pipeline-spec.md](hypothesis-pipeline-spec.md) §2。原问题保留如下以备追溯。

### 原提问

1. 是否接受"默认 ≤5 条 + 折叠摘要"的发布形态（这改变产品面貌）。
2. 是否把 security/performance/testing 从常驻 reviewer 降级为风险触发的 lens。
3. 基准输出语言：为对齐裁判改英文，还是保留中文并接受测量偏差。
