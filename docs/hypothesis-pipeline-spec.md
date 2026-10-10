# Hypothesis Pipeline 实施规格 v1

> 状态：已批准的目标架构，交付给执行模型实施。
> 依据：[architecture-diagnosis-20260901.md](architecture-diagnosis-20260901.md)。
> 基线：`main@892de6a`。所有路径相对 `backend/src/reviewforge/`。
> 本文是执行的唯一事实源；与旧文档冲突时以本文为准。

---

## 0. 给执行模型的须知

**你在做什么。** 把审查核心从"9 个 reviewer 各自产候选 → 4 层 LLM 过滤"替换为"确定性上下文包 → 全 PR 假设生成 → 逐假设调查 → 一次全局选择"。旧路径保留在 kill switch 后面，直到新路径通过 holdout 基准。

**必须遵守。**
1. 每个 Phase 独立可合并、独立可回退。不要跨 Phase 合并 PR。
2. 新代码放在新模块里；改动旧模块只允许"加分支"，不允许改旧分支行为。`REVIEWFORGE_PIPELINE=legacy` 时所有测试结果必须与改动前完全一致。
3. 所有 LLM 调用经 `ModelRouter`，agent 名字见 §4.9；不得直接构造 `ChatOpenAI`。
4. 所有 LLM 输出走 JSON schema 校验；解析失败 → 一次修复重试 → 仍失败则该次调用结果为 `unknown`，**绝不**变成"无问题"或"已拒绝"。
5. 不用 `confidence` 做任何分支条件。
6. 每个模块先写 `tests/test_<module>.py` 的确定性测试（用 `engine/mock_llm.py`），再接入 orchestrator。
7. 提交前跑：`cd backend && uv run ruff check . && uv run ruff format --check . && uv run reviewforge spec-check && uv run pytest -q`。
8. 不要"顺手"重构、删除或优化本文没列出的东西。退役清单在 §8，只在 Phase 4 执行。
9. 不确定就停下来在 PR 描述里写问题，不要自己填空白。

**验收怎么看。** 每个 Phase 末尾有"验收"小节。单元测试通过只是下限；Phase 2 起要跑 §7 的基准协议并附上配对结果。

---

## 1. 范围与非目标

范围：`engine/` 审查决策核心、`tools/` 仓库访问、`core/config.py` 开关、telemetry 事件、基准脚本的语言参数。

非目标：GitHub webhook / 控制台 / 数据库 schema（除新增表）/ 部署流程 / 前端。floor-v2 已落地的 `ReviewResult`、`RunHealth`、`reviewer_catalog`、`evaluation_telemetry` 保留并复用。

---

## 2. 已定决策

| 决策 | 取值 |
|---|---|
| 发布形态 | 默认最多 5 条行内评论；超出部分以折叠 `<details>` 摘要附在 review body；`error` 级且证据强度 `strong` 的可溢出至 8 条 |
| 专项 reviewer | security / localization / accessibility / dependency / testing / performance / doc 不再作为常驻 reviewer；改为 §4.5 的 lens，由 Change Model 风险信号触发，写入同一账本 |
| 输出语言 | 新增配置 `review.output_language`（`auto` / `en` / `zh-CN`）。`auto` = 按 PR 描述与代码注释主要语言判断，判不出取 `en`。基准跑 `en` |
| 模型角色 | 复用现有 5 角色：hypothesis→`deep_review`，investigator→`verifier`，editor→`publication_gate`，lens→`fast_review`。不新增控制台角色 |

---

## 3. 总体流程与开关

```
REVIEWFORGE_PIPELINE = legacy | shadow | hypothesis     （默认 legacy）
```

- `legacy`：现有路径，行为不变。
- `shadow`：现有路径发布；新路径完整执行到 §4.7 但不发布，结果落库 + telemetry，用于离线对比。
- `hypothesis`：新路径发布；现有路径不执行。

新路径顺序（`engine/pipeline_v4.py::run_hypothesis_pipeline`）：

```
1  PRHeadWorkspace.build(state)                      确定性     §4.1
2  compile_semantic_changeset(state)                 确定性     已有 semantic_diff.py
3  ContextPack.build(changeset, workspace)           确定性     §4.2
4  detectors (phase0.scan_changed_files)             确定性     已有；结果作为 seeded hypotheses 进账本
5  HypothesisGenerator.run(pass=1)                   LLM ×1     §4.4
6  lenses = select_lenses(changeset); 每个 lens 一次  LLM ×0–3   §4.5
7  Investigator.run(hypothesis) for each open one    LLM ×N     §4.6（并发 4）
8  Editor.run(ledger)                                LLM ×1     §4.7
9  publish（复用 _post_comments 的坐标校验与投递）    确定性
10 RunHealth.build + telemetry                       确定性     已有
```

每一步的失败语义在各节"失败"小节定义。总原则：步骤 1–3 失败 → run `failed`（不能审）；5–8 单次失败 → 对应对象 `unknown`，run `partial`。

---

## 4. 模块规格

### 4.1 `tools/workspace.py` — `PRHeadWorkspace`

**职责。** 为一次 run 提供绑定 `head_sha` 的只读仓库快照，支持文件读取和符号级搜索。替代 `search_code` 走默认分支的问题。

**实现。** 部署镜像无 git。用 GitHub `GET /repos/{head_repo}/tarball/{head_sha}` 下载到 `tempfile.mkdtemp()`，解压后计算 manifest。fork PR 用 `state.head_repo`。

```python
@dataclass(frozen=True)
class WorkspaceInfo:
    repo: str; head_repo: str; head_sha: str
    root: Path                      # 解压根目录
    file_count: int; byte_size: int
    digest: str                     # sha256(sorted(path:size:mtime))
    truncated: bool                 # 超过上限时为 True
    source: str                     # "tarball" | "api-fallback"

class PRHeadWorkspace:
    async def build(cls, state: StateStore, github: GitHubClient, *, max_bytes: int) -> PRHeadWorkspace
    def read(self, path: str, start: int | None = None, end: int | None = None) -> str | None
    def exists(self, path: str) -> bool
    def grep(self, pattern: str, *, globs: list[str] | None, max_hits: int, context: int = 0, diverse: bool = False) -> list[GrepHit]
    def find_symbol_definitions(self, symbol: str, *, language: str) -> list[SymbolHit]   # 用 symbol_extractor.extract_definitions 逐文件扫（带缓存）
    def find_callers(self, symbol: str, *, language: str, max_hits: int, diverse: bool = False) -> list[GrepHit]  # 排除定义行；可按文件轮转
    def cleanup(self) -> None
```

**限额。** `workspace.max_bytes` 默认 200 MB；超限时只解压 `files_changed` 所在顶层目录 + 常见源码目录（`src`, `lib`, `app`, `pkg`, `internal`, `packages`），`truncated=True`。下载失败 → `source="api-fallback"`：`read` 走 `get_file_content(ref=head_sha)`，`grep`/`find_*` 返回空并在 telemetry 记 `workspace.degraded`。

**失败。** tarball 与 API 都失败 → run `failed`，原因 `workspace-unavailable`。

**接入。** `tools/gateway.py` 的 `_read_file` / `_search_code` 在 `REVIEWFORGE_PIPELINE != legacy` 时优先走 workspace；legacy 不变。

**测试。** 用 `tests/fixtures/workspace_repo.tar.gz`（构造 3 种语言、含定义/调用/测试的迷你仓库）覆盖：读文件、找定义、找 caller、超限截断、fallback。

### 4.2 `engine/context_pack.py` — `ContextPack`

**职责。** 对每个 `SemanticUnit` 确定性地收集 diff 之外、判断正确性所需的代码片段，作为 LLM 的输入。这是"交付上下文而非发现上下文"的实现。

```python
@dataclass
class ContextSlice:
    kind: str            # caller | callee | base_class | interface | sibling | test | lock_usage | field_usage | schema | config
    path: str; start_line: int; end_line: int
    symbol: str
    text: str            # 原文，≤ max_lines
    reason: str          # 为什么选它，例如 "calls Foo.bar at line 88"
    sha: str

@dataclass
class UnitContext:
    unit_id: str
    slices: list[ContextSlice]
    truncated_kinds: list[str]     # 因限额被砍掉的 kind
    pr_intent: str                 # 见下

@dataclass
class ContextPack:
    units: dict[str, UnitContext]
    pr_intent: str                 # PR title + body（≤2000 字符）+ linked issues 标题
    workspace_digest: str
    def render_for_unit(self, unit_id: str, *, max_chars: int) -> str
    def render_all(self, *, max_chars: int) -> str      # 按 unit.risk_score 降序水位填充
```

**收集规则（每 unit，按序执行，命中即取）。**

| kind | 来源 | 规则 |
|---|---|---|
| `caller` | `workspace.find_callers(unit.symbol)` | 取最多 `max_callers`（默认 4）处，每处取调用行 ±12 行；Java 方法使用固定源码的 package / 所属类限定，避免普通 `run` 调用混入 |
| `callee` | `unit.calls`（已有）| 对 diff 新增行中的每个被调符号，`find_symbol_definitions` 取定义签名 + 前 8 行（含 docstring）；Java 保留 receiver，并按源码的 import、所属类、可见参数/字段限定目标，不回退到全库同名方法 |
| `base_class` / `interface` | `symbol_extractor.extract_definitions` 解析 unit 所在类的 extends/implements | 取父类/接口定义的方法签名列表（去掉方法体），Python/Java/TS/Go/Ruby 各写一个 regex 提取器 |
| `sibling` | 同文件、同类、同前缀（`getX`/`setX`、`Create/Get/Update/Delete`）的其它方法 | 取最多 3 个，每个取头 20 行。目的：暴露"其它方法用 V2 flag，这个用 V1"这类不一致 |
| `lock_usage` / `field_usage` | diff 新增行中出现的 `mu`/`Mutex`/`lock`/`sync.` 或类字段名 | 同文件所有使用点 ±3 行 |
| `test` | `unit.candidate_tests` | 取测试文件中引用 `unit.symbol` 的函数体，最多 2 个 |
| `schema` / `config` | unit.kind 为 RESOURCE 时 | 同名 schema/迁移/配置的相关段 |

**限额。** 每 unit ≤ `context_pack.max_slices`（默认 12）、每 slice ≤ 60 行、全包渲染 ≤ `context_pack.max_chars`（默认 40 000）。超限按 `unit.risk_score` 降序保留，被砍的 kind 写入 `truncated_kinds`（生成阶段会把它作为"未检查项"告诉模型）。

Java 的限定检索是源码导航，不是完整类型/继承或反射分析；解析不了接收者、不存在已定位的声明或缺少静态调用命中时，对应方向记为未检查，不能把无命中当作反证。v4 Impact Manifest 保留 column/receiver/receiver_type，但定位重新检查固定源码中的可见声明，不相信其它方法的同名变量类型；legacy Manifest 仍用原三字段。Java workspace 的 `find_definition` / `find_callers` 接受 `类.方法` 或 `包.类.方法`，短类限定允许包歧义，但不能匹配嵌套类的另一个 owner。纯名称查询的原行为不变。

被调 Java 方法涉及可变 static 字段时，callee 的 reason 可附 `State navigation`：检查该方法及一层同类 helper，列出相关字段声明和同类方法引用位置，每条索引 ≤600 字符、最多 4 字段/每字段 6 方法，遗漏明确标示。它用来发现 configure/init/reset 等替代入口，不声明写入事实、调用顺序或完整性；仍计入原渲染预算，不新建工具、Observation 或 verdict。调查需用既有工具窄读取证。

有这类索引的 unit 在生成、lens 和调查阶段共用 `state_contracts.md`：定位替代 producer，证明目标输入的值绑定及实际入口顺序，不能仅凭初始化名称、注册注解或一次 configure 调用推断时序。指南只按该 unit 的已编译 reason 选择，不从源码正文中的同名文本触发；生成分块计入其字符，调查收尾保留同份规则，无关 unit 不额外交付。它是取证方法，不能作为 Observation，不改变既有证明门槛或预算。

**失败。** workspace 降级时 pack 只含 `pr_intent` 与 diff 内可得信息，`truncated_kinds=["all"]`；不阻断 run。

**测试。** 用 4.1 的 fixture 仓库，断言每种 kind 至少一个用例能取到正确片段；断言限额与排序确定性（同输入两次渲染字节相同）。

### 4.3 `engine/hypothesis.py` — 假设账本

```python
class Mechanism(StrEnum):
    WRONG_ARGUMENT = "wrong-argument"         # 传错参数/变量/字段
    WRONG_OPERATOR = "wrong-operator"         # && vs ||, === on objects, off-by-one
    NULL_PATH = "null-path"                   # 未处理 null/empty/missing key
    CONTRACT_MISMATCH = "contract-mismatch"   # 调用方/被调方/父类/schema 约定不一致
    MISSING_AWAIT = "missing-await"           # fire-and-forget、未等待
    LOCK_SCOPE = "lock-scope"                 # 锁范围/竞态/双检
    STATE_LEAK = "state-leak"                 # 共享可变默认值、缓存污染、资源未释放
    ERROR_PATH = "error-path"                 # 异常吞掉/错误分支跳过清理
    REGRESSION_REMOVED = "regression-removed" # 删掉了原有保护/字段/日志
    SECURITY_SINK = "security-sink"           # 注入/SSRF/XSS/权限绕过
    I18N = "i18n"; A11Y = "a11y"; PERF = "perf"; TEST_GAP = "test-gap"; DOC = "doc"

class HypothesisStatus(StrEnum):
    OPEN = "open"; CONFIRMED = "confirmed"; REFUTED = "refuted"; UNKNOWN = "unknown"

@dataclass
class Site:
    path: str; line: int; excerpt: str          # excerpt 必须是 diff RIGHT 侧的原文子串

@dataclass
class Observation:                                # 与 floor-v2 §4.3 EvidenceObservation 对齐
    id: str; tool: str; query: str
    path: str; line_range: tuple[int, int] | None
    sha: str; result_digest: str; excerpt: str    # excerpt ≤ 1200 字符
    status: str                                   # success | not_found | error

@dataclass
class Hypothesis:
    id: str                                       # "h_" + 8 hex
    identity: str                                 # f"{unit_id}::{mechanism}::{anchor_symbol}"
    unit_id: str; mechanism: Mechanism
    claim: str                                    # 一句话：什么错了
    trigger: str                                  # 怎样触发（输入/时序/环境）
    impact: str                                   # 后果
    open_question: str                            # 要确认此事必须知道的一个具体事实
    refutation: str                               # 什么事实能推翻它
    sites: list[Site]                             # ≥1
    severity: str                                 # error | warning | info（生成时给出，调查后可改）
    source: str                                   # generator | lens:<name> | detector:<rule>
    status: HypothesisStatus = OPEN
    evidence_strength: str = "none"               # none | weak | strong（调查后）
    observations: list[Observation] = []
    verdict_reason: str = ""
    attempts: int = 0

@dataclass
class HypothesisLedger:
    run_id: str; head_sha: str; workspace_digest: str
    items: dict[str, Hypothesis]                  # key = identity
    no_issue_units: dict[str, str]                # unit_id -> 检查边界说明
    def upsert(self, h: Hypothesis) -> tuple[Hypothesis, bool]   # 同 identity: 合并 sites/取更高 severity，返回 (merged, created)
    def open(self) -> list[Hypothesis]
    def to_dict / from_dict
```

**持久化。** 新表 `hypotheses(run_id, identity, json, updated_at)` + `observations(run_id, hypothesis_id, json)`，`core/database.py` 增加 append-only 写入。`StateStore` 增加 `ledger: HypothesisLedger | None` 字段，resume 时从表重建。

**identity 规则（关键）。** `anchor_symbol` = site 所在的最内层函数/方法名（用 `symbol_extractor._find_enclosing_function`），没有则用 `unit.symbol`。同一 unit、同一 mechanism、同一 anchor → 同一假设，只增加 sites。跨 unit 的重复（例如 6 处 `log.Error`）在生成提示里要求模型用一条假设 + 多个 sites 表达；仍漏网的由 §4.7 合并。

### 4.4 `engine/hypothesis_generator.py` — 假设生成

**调用。** agent 名 `hypothesis_generator`，角色 `deep_review`，temperature 0.1，`max_tokens` 8192。每 PR 1 次；若 PR diff + pack 超过 `generator.max_input_chars`（默认 120 000），按 unit 分块（每块 ≤ 上限，按 risk 排序），块间共享账本（后块提示里附前块已生成假设的 identity + claim 列表）。

**输入渲染（顺序固定）。**
1. 系统提示（§5.1）。
2. `## PR intent`：`pack.pr_intent`。
3. `## Changes`：本块各文件的 before/after diff hunk，每个 hunk 只呈现一次；可评论行就地标注 `RIGHT行号 | 原代码`，保留新增/删除标记与源码顺序，不再复制一整份右侧代码。之后逐 unit 保留独立 ID、符号和可用 RIGHT 行号。分块预算按共享 hunk 的实际渲染长度计量，同一 hunk 不能因多个 unit 重复入账。被删除的 guard/lock 等左侧代码与各 unit 的右侧锚点均不可省略，跨块仍需要各自携带相关完整变更。
4. `## Context`：按 `pack.render_all()` 的原风险顺序和全局字符预算选出的视图；每个 slice 以 `### {kind} {path}:{start}-{end} — {reason}` 开头。同一块内 path / 行范围 / SHA / 已交付文本均相同的片段可引用前一个 unit 的 source，保留当前 kind/reason/header；引用不能增加字符数或恢复预算外正文，不能跨块引用。独立 investigator 仍使用原 `render_for_unit()`。
5. `## Unchecked`：`truncated_kinds` 汇总——告诉模型哪些上下文没给到，这些方向只能提 `open_question` 不能下结论。
6. `## Existing hypotheses`（分块或 lens 时）：identity / status / claim 列表；OPEN 不是已证事实。当前 unit 已被同一 trigger/impact/待核实事实覆盖时，在 checked 中引用已有 identity，不能换 unit 或表述重复建候选。
7. `## Required assessments`：本块 unit ID 清单与数量，要求每个 ID 返回 hypothesis 或带 checked 边界的 no_issue（包括测试与 fixture）；不得由上下文或旧账本代替本轮返回。清单计入既有输入字符上限，省略仍进入 unresolved，不做补找重试。

**输出 schema（严格）。**
```json
{"hypotheses":[{"unit_id":"...","mechanism":"wrong-argument","anchor_symbol":"updateDevice",
  "claim":"...","trigger":"...","impact":"...","open_question":"...","refutation":"...",
  "severity":"error","sites":[{"path":"...","line":93,"excerpt":"return ErrDeviceLimitReached"}]}],
 "no_issue_units":[{"unit_id":"...","checked":"..."}]}
```
校验：`sites[].excerpt` 必须是该 path 的 diff RIGHT 侧某行的子串（≥12 字符），否则该 site 丢弃；无有效 site 的假设丢弃并计 `generator.dropped_unanchored`。`mechanism` 必须在枚举内。每次调用最多接受 `generator.max_hypotheses`（默认 12）条，超出按 severity 保留并记 telemetry。

**失败。** 解析失败一次修复重试；再失败 → 该块 `unknown`，其 unit 进入 `ledger.no_issue_units` 不允许，而是记为 `unresolved_units`，run `partial`。

**禁止。** 不做"没发现就再来一次"的重试。没有假设就是没有。

### 4.5 `engine/lenses.py` — 专项 lens

**触发（确定性）。**

| lens | 触发条件（任一） | 角色 |
|---|---|---|
| `security` | 任一 unit 有 `security-sensitive*` risk signal；或 detectors 命中；或 diff 新增行匹配 `engine/detectors/security.py` 的 source/sink 正则 | `fast_review` |
| `localization` | 改动了 `*.properties` / `*.po` / `messages_*.json` / `locale/` 路径 | `fast_review` |
| `accessibility` | 改动了 `*.tsx/*.jsx/*.vue/*.svelte` 且新增行含 `<button`/`<input`/`aria-`/`role=` | `fast_review` |
| `concurrency` | 新增行含 `go func`/`Mutex`/`RLock`/`asyncio.gather`/`Promise.all`/`threading`/`forEach(async` | `fast_review` |
| `dependency` | 改动了 lockfile / manifest（复用 planner 的 `_detect_patterns` 路径规则） | `fast_review` |

**执行。** 每个触发的 lens 一次调用，输入 = 与 §4.4 相同的渲染但只含触发它的 units + 该 lens 的 SKILL.md（复用 `skills/security_rules` 等）+ `## Existing hypotheses`。输出 schema 同 §4.4，`source="lens:<name>"`。写入同一账本，`upsert` 合并。

**限额。** 每 PR 最多 3 个 lens（按触发 unit 的 risk 之和排序）。

### 4.6 `engine/investigator.py` — 调查

**职责。** 对每个 `OPEN` 假设回答它的 `open_question`，核实本次改动如何使 trigger 可达或产生新的后果，产出三值结论与 observations。head 中已有某个现象不代表本 PR 引入；旧代码也可能因新调用/数据流成为回归，不能只按旧侧是否存在某段表达式判定。这是唯一有工具的 LLM 阶段，也是唯一的过滤阶段。

**调用。** agent 名 `investigator`，角色 `verifier`，temperature 0。并发 `investigator.concurrency`（默认 4）。

**预算分配。**
```
budget_steps = base(severity) + bonus
  base: error 6, warning 4, info 2
  bonus: +2 若 refutation 涉及 diff 外文件；+2 若 open_question 提到 caller/父类/schema
  上限 8；token 上限 = steps × 4000
```
每 PR 调查总预算 `investigator.max_hypotheses_per_pr`（默认 12）。先按 severity 分层；层内按 mechanism 轮转取一条，组内仍按 sites 数量降序 → identity 排序，mechanism 的轮转顺序由其首条的原排序确定。避免同一广泛机制挤占全部调查名额，不改变严重级别优先或增加预算。超出的标 `unknown`，reason `budget-exhausted`；不按类别过滤、自动推翻或确认。

预算包含结论调用，不能把全部额度用于工具循环。每轮先预留结论输入及输出（输出预留为总预算的四分之一，最多 4000 tokens），不足以继续工具调用时提前结束调查。结论输入重用原始假设/上下文及代码保存的 Observation excerpts，不重放完整工具结果或模型探索文字。估算计入工具参数、schema 与非 ASCII 文本，并用已测 provider input usage 校正遗漏的开销；实际 usage 仍如实记账，超支不可写已确认结论。

**工具（通过 gateway，绑定 workspace）。** `read_file(path, start?, end?)`、`grep(pattern, glob, max_hits)`、`find_definition(symbol)`、`find_callers(symbol)`、`read_diff(path, start?, end?)`。diff 行窗口选择与指定 RIGHT 行相交的完整 before/after hunk，保留删除行与上下文；省略窗口保留原完整 diff 行为，未命中为空/`not_found`，不得用作反证。每次工具结果 ≤ 6000 字符，同一 (tool,args) ≤ 2 次。每次工具调用自动记录一条 `Observation`（tool/query/path/sha/digest/excerpt/status）——**observation 由代码写，不由模型写**。

v4 的 `read_file` 两个边界都省略时，优先使用该文件最近一个已保存的成功 grep/caller/definition 命中行；其次用 compiler 关联假设的 site，或该 unit 自己的 Context slice 起始行。默认请求该行前 3 / 后 8 行，把相关正文交付到现有 1200 字符保存区，避免整份文件的版权头占满证据。显式任一边界仍按原请求读取，无已知位置仍读原文件；工具描述和返回标签明确窗口，query/line_range 记录实际请求参数。只用已保存的正向命中，不从 not_found/error 或不可引用的 Additional context 猜位置。每个调查员独立计算窗口，不增加工具调用、预算或证据保存长度；workspace/legacy 的读取 API 不变。

**输入。** 系统提示（§5.2）+ 假设全文 + 该 unit 的 diff hunk + `pack.render_for_unit(unit_id)` + 已有 observations。额外交付 `pack.pr_intent` 的前 2000 字符；作者意图是理解行为变化的背景，不能作为正确性证据或豁免契约。收尾保留这份背景。

`i18n` 的主 unit 为 resource 时，额外交付该 unit 与 sites 对应 resource 的 path/provenance，逐文件保留已编译的 locale 信息。直接语言/字形违规以声明 locale 和变更文本为本地契约，无需页面引用；格式语法/参数和运行时后果仍需实际消费端及输入契约。生成器/lens 的问题与反证必须决定所述契约，而非无关调用方事实。metadata 不构成 defect evidence，Observation、精确引用及 UNKNOWN 门槛不变。

v4 的格式契约指南由 `verification_guidance.py` 共用，localization 路径判定保持既有 lens 规则。生成器仅在包含相关资源的块中交付指南并计入块预算；localization lens 仍在系统规则中交付一次；调查员对 i18n 或对应 localization resource 的候选交付同一指南，收尾保留。指南解释标准默认值与配置边界，不证明目标资源实际使用了该标准；引用必须证明具体消费调用/初始化，import、邻近文本或未搜索到其它库不足以推翻格式缺陷。共享 legacy SKILL.md 不变，未新增独立模型阶段或发现重试；增加的指南仍受原块预算约束，可能影响分块。

Python 并发契约也由同一模块交付：生成器只对 `.py` 且 diff 含进程/线程库或构造器的块交付并计入预算，concurrency lens 在系统规则中交付一次，调查员对关联 `.py` diff 使用同一选择规则并保留到收尾。规则覆盖 CPython context 工厂的具体类型、已完成子进程的自动回收、仍存活的挂起进程、共享 IPC 与限额生命周期；需绑定实际对象、平台及契约，不能因缺少显式 join/reset 就断言缺陷。原 lens 触发、legacy skill 与调用次数不变，指南不能作为 Observation 或自动 verdict。边界输入假设需有真实调用、配置或支持契约，方法允许传入 0/None/空集合本身不证明业务支持。

生成器、全部 lens 与调查员共享 `defect_scope.md`：沿用排除纯测试/文档建议、风格与命名的既有审查范围，事实回答不足以确认缺陷，必须有真实义务与同一输入下的新后果。测试本身执行错误或掩盖已观察到的失败仍可构成缺陷，缺一次断言本身不构成。调查和收尾保留同一范围说明；它是审查规则，不能充当源码 Observation。只改阶段规则交付，不增加代码侧候选类别过滤；Editor 仍不能丢弃已确认项。

**输出 schema。**
```json
{"answer":"对 open_question 的直接回答",
 "severity":"error|warning|info",      // 可修正
 "additional_sites":[{"path":"...","line":1,"excerpt":"..."}],
 "assessment": {
   "expected":"同一受支持输入的预期行为或契约",
   "actual":"本次变更的实际行为或反证",
   "expected_evidence":["obs_0:e1"],
   "actual_evidence":["obs_1:e1"],
   "comparison":"conflict|compatible|unresolved"
 },
 "reason":"..."}
```
校验：`confirmed`/`refuted` 的 assessment 两组引用均需来自 `status=success` 的 observation。工具回复与收尾输入提供代码从已保存源码生成的 `obs_N:eM` ID，模型复制 ID；代码将其解析为内部 `{observation_id, quote}`，再用原精确匹配校验。源码按完整行分段（目标 400 字符，单行可更长），搜索命中/定义分段不跨结果边界；不增加现有 1200 字符保存区，负向结果不生成 ID。未知、拼造或带多余字符的 ID 为 UNKNOWN/ungrounded-assessment。旧 assessment 原文引用仍兼容并逐条精确校验，不能用有效 ID 掩盖无效旧引用。

新模型响应只写一份 `assessment.comparison`，不重复输出 verdict。两前提及引用通过原校验后，代码将 conflict/compatible/unresolved 分别映射为 CONFIRMED/REFUTED/UNKNOWN；assessment=null、未知或畸形关系保持 UNKNOWN。若响应仍显式携带旧 verdict，按原一致性检查处理，矛盾仍为 UNKNOWN/inconsistent-assessment；显式 UNKNOWN 不被提升，也不改写已有账本或历史响应。减少同一三值判断的重复转抄，不增加确认/推翻条件。

从通过校验的引用派生内部 `evidence_ids/evidence_quote`，模型无需再抄第三份引用。若响应仍显式提供旧的顶层 `evidence_ids/evidence_quote`，继续精确校验，失败为 UNKNOWN/ungrounded；不忽略无效旧引用。`read_file` 从固定 workspace 原始正文按范围切片，保存不含展示行号的源码；path / line_range 为独立元数据，解析后的原文保留缩进、换行、Unicode 和真正的数字前缀，落库/续跑也不 trim quote，不做模糊引用匹配。`refuted` 不能仅基于 `not_found`（"没搜到"不是反证）。

工具回复只交付实际保存的 excerpt 与位置元数据；超过 1200 字符的正文不再以 Additional context 展示，保留缩小 read/search 范围的提示。结果摘要 digest 与既有最大工具读取水位不变，原始 Context 仍作导航而非 Observation 证据；需更深正文时通过既有工具窄读，不增加新工具或调查预算。缺少证明为 UNKNOWN，不能凭“未看到触发路径”推翻；声称值转换失败时需追踪引用的具体输入、运算及结果，不能只看语法推断失败。

v4 Investigator 的 `grep` / `find_callers` 使用 workspace 的 `diverse=True`：先选不同文件各自的首条命中，再按文件中的命中序号轮转，序号相同时按 path/line 排序。结果仍不超过请求的 max_hits，选择器最多保存这么多行，找到足够的不同文件后停止；避免一个早排序文件的重复调用占满结果和 1200 字符证据区。原 workspace 默认 path/line 顺序、显式 glob、源码行、位置与引用校验保留；不按“生产/测试”猜测过滤。这仍是有界抽样，长行或超过文件名额的路径可能遗漏，不能把未返回的调用当成不存在。更广的扫描可能增加检索耗时，评测的 CPU/内存/总时限约束继续生效。

事实回答不等于缺陷成立。新调查的 `confirmed/refuted` 还必须分别交付 expected 与 actual 的非空陈述和引用；每条引用必须精确命中其指定的成功 Observation，而不能用一个无关成功读取掩盖另一前提的缺失。同一 Observation 能证明两者时可复用，但不能把实际 throw 当成“必须收集消息”的契约。标准库的预期行为可来自已文档化契约，引用需绑定实际类型/配置/数据流，无需本地包含库源码。`confirmed` 仅接受 `conflict`，`refuted` 仅接受 `compatible`；结构缺失、引用无效、关系与 verdict 矛盾分别降为 UNKNOWN（`incomplete-assessment` / `ungrounded-assessment` / `inconsistent-assessment`）。UNKNOWN 不要求完整 assessment，也不会因附带证明被自动提升。代码核实引用和关系一致性，语义判断仍由调查员承担。

`ContractAssessment` 随假设落入现有 JSON 账本/checkpoint，交付 editor 和失败模板；不增加数据库表或 LLM 阶段。旧 checkpoint 缺少该字段仍可加载，不重开已有 CONFIRMED/REFUTED。证据强度按通过校验的两组引用判断 diff 外来源，而非无关 observation。预算、UNKNOWN 准入和严格裁判保持原值。

**evidence_strength。** `strong` = 引用 ≥1 个 diff 外文件的 observation 或 detectors 命中；`weak` = 仅 diff 内 observation；`none` = unknown。

**失败。** provider 错误 → `unknown`, `retryable=True`；步数耗尽 → 强制一次"只输出 JSON"→ 仍无 → `unknown`。任何情况不得写 `refuted`。

**测试。** mock LLM 脚本化工具序列，断言：ungrounded 降级；not_found-only 降级；observation 自动记录；预算按 severity 计算；并发下账本 upsert 不丢 sites。

### 4.7 `engine/editor.py` — 选择与写作

**调用。** agent 名 `editor`，角色 `publication_gate`，temperature 0，每 PR 1 次。输入 = 所有 `CONFIRMED` 假设（含 sites、observations excerpt、evidence_strength）+ `UNKNOWN` 假设的 claim 列表（仅用于摘要，不可发布）+ `pr_intent` + 输出语言。

**确定性预处理（先于 LLM）。**
1. 同 identity 已合并；再按 `(mechanism, scoped_anchor)` 跨 unit 聚类，同簇合并 sites。已命名符号的 scope 包含主 site 文件路径与 anchor；无符号或 `<module>` 的 scope 使用 unit_id，不把不同文件/资源单元的空 anchor 当作同根因。显式多 site 假设保留全部位置；跨簇仅在证据支持同一处修复时由编辑合并。
2. 排序键：`severity_rank × strength_rank × min(len(sites),3)`，severity error=3/warning=2/info=1，strength strong=3/weak=1。
3. 取前 `publish.max_inline`（默认 5）为 inline 候选；`error` + `strong` 溢出到 `publish.max_inline_overflow`（默认 8）。

**LLM 任务。** 对 inline 候选写评论；对余下 confirmed 写一行摘要；标出它认为应合并的候选对（跨簇同根因）。输出 schema：

修复应保留 PR 的有效新行为，只修已证实的不一致。不能整段恢复旧文本并重新引入有意移除的非法内容；证据不足以确定改法时描述目标契约，不编造 suggestion patch。
```json
{"comments":[{"hypothesis_ids":["h_..."],"path":"...","line":93,
   "title":"≤60 字符","body":"issue / why / where / fix 四段","suggestion_patch":"可选"}],
 "summary_items":[{"hypothesis_id":"h_...","one_line":"..."}],
 "merged":[["h_a","h_b"]]}
```
校验：`path:line` 必须是引用假设的某个 site；`body` 必须包含每个 site 的 `path:line` 列表（多处同因一条评论）；条数 ≤ 上限。

**发布。** inline comments 走现有 `_post_comments` 的坐标校验与投递；`summary_items` + `unknown` 假设的"未能确认"列表进 review body 的 `<details>`。unknown 项措辞固定为"未能在预算内确认"，不写成问题。

**失败。** editor 调用失败 → 用确定性预处理结果直接生成模板评论（title=claim，body=claim+trigger+impact+sites），语言按配置；run 记 `editor.fallback`。不允许因 editor 失败而不发布已确认假设。

### 4.8 `engine/pipeline_v4.py` — 编排

- 新文件，函数 `run_hypothesis_pipeline(orchestrator, state) -> RunHealth`，按 §3 顺序执行；`Orchestrator.run()` 在开头根据 `REVIEWFORGE_PIPELINE` 分派，`legacy` 走原逻辑不变。
- `shadow` 模式：先跑 legacy 到发布完成，再跑新路径到 §4.7（不投递），把 editor 输出写入 `shadow_publications` 表。
- checkpoint：账本每次 upsert 后落库；resume 时 `OPEN` 假设重新调查，`CONFIRMED/REFUTED` 不重做。
- `RunHealth.build` 增加 stage `hypothesis`（生成失败块数）、`investigation`（unknown 数 / 总数）。`completed` 要求 unknown 中 severity=error 的数量为 0，否则 `partial`。

### 4.9 配置与路由

`core/config.py` 新增：
```python
@dataclass
class PipelineV4Config:
    mode: str = "legacy"                       # legacy | shadow | hypothesis ; env REVIEWFORGE_PIPELINE
    output_language: str = "auto"              # env REVIEWFORGE_OUTPUT_LANGUAGE
    workspace_max_bytes: int = 200_000_000
    context_pack_max_slices: int = 12
    context_pack_max_chars: int = 40_000
    generator_max_input_chars: int = 120_000
    generator_max_hypotheses: int = 12
    max_lenses: int = 3
    investigator_concurrency: int = 4
    investigator_max_hypotheses_per_pr: int = 12
    publish_max_inline: int = 5
    publish_max_inline_overflow: int = 8
```
`reviewforge.yaml` 加 `pipeline_v4:` 段，默认值同上。`ROLE_MAP` 加：`hypothesis_generator→deep_review`、`lens_*→fast_review`、`investigator→verifier`、`editor→publication_gate`。`reviewer_catalog` 不改。

**语言。** `prompt.py:_language` 改为读 `output_language`；`auto` 的判定函数放 `engine/language.py`：PR body 与 diff 注释中 CJK 字符占比 > 30% → `zh-CN`，否则 `en`。legacy 模式默认值保持 `zh-CN` 以不改变旧行为。

### 4.10 Telemetry 事件

沿用 `EventBus.emit`，新增事件名与必填字段：

| 事件 | 字段 |
|---|---|
| `workspace.built` | source, file_count, byte_size, truncated, digest, ms |
| `context_pack.built` | units, slices, truncated_units, chars |
| `hypothesis.generated` | pass, source, accepted, dropped_unanchored, dropped_overflow, tokens |
| `lens.selected` | lenses[], reasons[] |
| `investigation.completed` | hypothesis_id, verdict, steps, tokens, observations, strength |
| `investigation.skipped` | hypothesis_id, reason |
| `editor.completed` | inline, summary, merged, fallback |
| `pipeline_v4.completed` | mode, hypotheses_total, confirmed, refuted, unknown, published, tokens_by_agent |

`core/evaluation_telemetry.py` 的 schema 加 `pipeline_v4` 块，`eval/architecture_floor.py` 读取它。

---

## 5. 提示词骨架

提示词放 `engine/prompts_v4/*.md`，用 `{{placeholder}}` 模板，由代码填充；不在 Python 字符串里硬编码。以下是必须包含的段落与措辞约束，执行模型按此写全文。

### 5.1 `generator.md`

- 身份：资深审查者，任务是对**这个 PR 引入的**缺陷提出可验证的假设，不是列出所有可改进之处。
- 输入说明：Changes 是 diff；Context 是我们替你找到的相关代码，**优先基于 Context 判断跨文件一致性**（调用方约定、父类要求、兄弟方法模式、锁范围）；Unchecked 列出没给到的上下文，对这些方向只能提出 open_question。
- 假设质量：每条必须有 trigger（具体输入或时序）、impact（可观察后果）、open_question（**一个**能用工具回答的具体事实问题，例如"`getOrCreateResource` 创建资源时 owner 是否设为 `resourceServer.getClientId()`"）、refutation（什么事实会推翻）。写不出 trigger 和 refutation 的不要提。
- 合并规则：同一机制在多处出现（同一函数被多次误用、同一日志级别误用多行）只写**一条**假设，全部位置放进 sites。
- 排除：风格、命名、注释措辞、"建议加测试/文档"、纯理论风险（没有 trigger）、对被删代码的猜测（除非 Context 显示仍有调用方依赖它）。
- 输出：只输出 JSON，schema 见 §4.4；excerpt 必须逐字来自 diff 右侧。
- 语言：`{{output_language}}`。

### 5.2 `investigator.md`

- 身份：调查员。任务是**回答 open_question**，不是评价假设写得好不好。
- 流程：先读 Context 里已有的片段；不够再用工具，优先 `find_definition` / `find_callers` / `grep` 定位事实，再 `read_file` 取证。每次工具调用前说明要证明或推翻的具体事实。
- 判定：`confirmed` = 你读到的代码使 trigger 成立且 impact 会发生；`refuted` = 你读到的代码使 refutation 成立（例如调用方已做检查、父类有默认实现）；其余为 `unknown`。**"没找到"不是反证。** 不要因为"风格可以更好"而 confirmed。
- 证据：`evidence_quote` 必须逐字来自你读到的工具结果。
- 输出：只输出 JSON，schema 见 §4.6。

### 5.3 `editor.md`

- 身份：审查评论编辑。输入是已经证实的问题清单，你的工作是合并、排序、写清楚。
- 合并：两条描述同一根因（相同修复能同时解决）的合并为一条，sites 合并。
- 每条评论四段：**Issue**（一句话）/ **Why**（引用证据：Context 或 observation 的原文，标明 path:line）/ **Where**（全部 sites）/ **Fix**（具体改法，可给 patch）。
- 不写：客套、免责声明、"建议考虑"。有把握就直说。
- 语言：`{{output_language}}`；代码标识符保持原样。
- 输出：只输出 JSON，schema 见 §4.7。

---

## 6. 分阶段任务

### Phase 0 — 测量修正（不改审查逻辑）

1. `output_language` 配置 + `engine/language.py` + `_language()` 改造；legacy 默认 `zh-CN`。
2. `.reviewforge/benchmarks/martian_runner.py` 增加 `--output-language` 参数透传。
3. 固定拆分：`eval/workloads/dev10.json`（沿用 representative-10 的 10 个 PR）与 `eval/workloads/holdout40.json`（其余 40 个），从 `minimax-m3-final-v3-full50-20260730/workload.json` 生成，提交到仓库。
4. `eval/paired_report.py`：读两个 `judged-strict.json`，输出每 PR 配对差值、win/tie/loss、P10 / worst F1。

验收：legacy + `en` 在 dev10 跑一次，与中文基线做配对（这一步的差值就是"语言混淆"的量化，记入 benchmark.md）。

### Phase 1 — 确定性地基

1. §4.1 `PRHeadWorkspace`（含 fixture 仓库与测试）。
2. §4.2 `ContextPack`（含每种 kind 的测试）。
3. §4.3 账本 + 持久化表 + `StateStore.ledger`。
4. §4.9 配置、`ROLE_MAP`、§4.10 事件名（先只发 `workspace.built` / `context_pack.built`）。
5. `pipeline_v4.py` 骨架：`shadow` 模式下只跑 1–3 步并落 telemetry。

验收：dev10 在 `shadow` 下跑通，`context_pack.built` 对每个 PR 都有记录；抽查 keycloak#36880、grafana#97529、sentry#80168 三个 PR 的 pack，确认 `base_class`/`sibling`/`lock_usage` 片段确实包含了 golden 需要的代码（把片段贴进 PR 描述）。

### Phase 2 — 假设生成 + 调查（shadow）

1. §4.4 生成器 + `generator.md` + schema 校验 + 测试。
2. §4.5 lens 触发与执行。
3. §4.6 调查员 + 工具绑定 workspace + observation 自动记录 + 测试。
4. `shadow` 模式跑到 §4.6，账本落库。
5. `eval/ledger_recall.py`：把账本里 `CONFIRMED + OPEN + UNKNOWN` 的 claim 当作候选，用 `martian_judge.py` 同口径判"账本召回"（生成阶段有没有覆盖 golden），并单独报告 `CONFIRMED` 召回与 `REFUTED` 中的 golden 数（= 调查误杀）。

验收（dev10, 同一模型 MiniMax-M3）：账本召回 ≥ legacy 发布召回 + 10pp；REFUTED 中 golden ≤ 2；每 PR token ≤ 250k。不达标不进 Phase 3，先在 dev10 上做提示词/限额消融并记录。

### Phase 3 — 选择与发布（hypothesis 模式）

1. §4.7 editor + `editor.md` + fallback + 测试。
2. 接入 `_post_comments`，review body 的 `<details>` 摘要。
3. `RunHealth` 新 stage；`pipeline_v4.completed` 事件；`evaluation_telemetry` schema。
4. resume 路径测试：中断后 OPEN 重调查、CONFIRMED 不重做、不重复发评论。

验收（先 dev10，再 holdout40，各跑 2 次，同一模型）：
- holdout40 配对：F1 相对 legacy 提升且 P10/worst 不回退；FP 中"同根因重复"用 `eval/duplicate_audit.py`（按 `(path, mechanism)` 簇内 >1 条计重复）≤ 3 条；零评论且含 golden 的 PR ≤ 4 个。
- 与 Qodo 的比较只作为参考行，不作为 gate。

### Phase 4 — 退役与默认切换

1. 默认 `REVIEWFORGE_PIPELINE=hypothesis`；控制台显示模式。
2. 执行 §8 退役清单。
3. 更新 README / benchmark.md / v3-architecture.md，写明新数据流与基准结果（含置信区间与 holdout 声明）。

---

## 7. 评测协议

- 集合：dev10 用于迭代，holdout40 只在 Phase 3 验收时跑；holdout 结果不得用于调参。
- 模型：所有角色同一模型（先 MiniMax-M3），`ModelRouter` 的 role override 与 legacy profiles 清空；从管理端 `GET /api/admin/llm-settings`（`api/admin.py:146`）返回的 effective 配置核对。
- 语言：`en`。
- 裁判：`.reviewforge/benchmarks/martian_judge.py`，SHA 固定，与 `test_martian_judge_strict.py` 一致。
- 报告：`eval/paired_report.py` 输出；每次跑 2 遍取配对均值，并附每 PR 明细。
- 声明：只能说"在该 workload、该模型、该裁判下相对 legacy 的配对差值"，不做普遍优劣宣称。

---

## 8. 退役清单（仅 Phase 4）

| 退役 | 理由 |
|---|---|
| `engine/calibrator.py` 的对抗/judge 轮次 | 被 §4.6 取代；`apply_actionability_gate` / `apply_code_evidence_gate` 迁到 `engine/deterministic_gates.py` 保留 |
| `engine/escalation.py` 的 `EscalationReviewer` 与 `PublicationGateReviewer` | 被 §4.6 取代 |
| `engine/publication_triage.py` | 被 §4.7 取代 |
| `engine/coverage_gap.py` | 默认已关闭且无收益 |
| `engine/cross_pr_analyzer.py` | 50 PR 零产出；historical graph 写入保留为 `core/history_graph.py`，LLM 确认链删除 |
| orchestrator 的 V3 targeted closure（含 "look harder" 重试） | 被 §4.4 取代 |
| `verifier.py` / `root_cause.py` / `publication_policy.py` 的 6 层 dedup | 被 identity + editor 取代；`finding_anchors.py` 坐标校验保留 |
| `reviewer_catalog` 中 performance / doc / accessibility / dependency / testing 常驻 reviewer | 变为 lens；catalog 条目保留但 `planner_enabled=False` |
| `skills/` 下 10 个 style 类 SKILL.md | 从未注入；移到 `skills/archive/` |

退役前提：holdout40 验收通过，且 `legacy` 模式在退役 PR 中被整体删除而非部分删除。

---

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| tarball 下载慢/大 | 限额 + 目录裁剪 + api-fallback；`workspace.built.ms` 进 telemetry，P95 > 60s 时调低上限 |
| 生成器把整个 PR 塞进一个上下文时质量下降 | 分块 + 共享账本；`generator_max_input_chars` 可调；分块数进 telemetry |
| 调查员被 open_question 引导得太窄 | 输出允许 `additional_sites` 与 severity 修正；lens 提供第二视角 |
| ≤5 条上限丢 TP | 溢出规则 + 摘要区；`duplicate_audit` 与 FN 审计一起看 |
| 模型不支持 tool call | 复用 `scripts/probe_tool_calling.py` 的两轮探针；不通过则 investigator 走单发（只用 Context Pack，不用工具），结论最多 `weak` |
| 任一阶段回归 | `REVIEWFORGE_PIPELINE=legacy` 即刻回退；Phase 4 前 legacy 代码不删 |
