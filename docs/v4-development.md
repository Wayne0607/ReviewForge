# ReviewForge v4 开发记录

更新时间：2026-10-10。开发分支：`dev`。生产分支：`main@00c667556c88241d72d96f24430c7c16b4add24e`。

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

2026-10-09 第二轮已完成的可靠性修正：

- 全部 v4 角色接入真实 token 用量与数据库记录；调查同时约束步数与累计 token，预算耗尽保留 unknown。
- 每次生成 upsert、调查完成即保存完整账本快照，包含 head SHA、digest、no_issue/unresolved；一个调查尚未完成时，另一个已完成调查仍可恢复。
- workspace 不可用与主路径异常写入 failed，清理快照；数据库最终状态写入失败会向上传播。
- API fallback 采用异步读取；provider 错误保留可重试标记。
- 旧代码并不存在任务卡提到的 outbox。新增 v4 专用 `v4_review_outbox`，先冻结完整发布负载再投递；远端接受但本地回执丢失时，恢复通过隐藏标记核对同一 head 的已提交 review。网络/5xx 或无有效回执时不会盲目重复 POST。
- 投递状态不明且远端暂未找到标记时保持 partial，继续只读核对。这种保守策略防重复，但若进程在落库 sending 后、发出请求前崩溃，不能自动证明“尚未发送”；不宣称跨 GitHub/SQLite 的事务性 exactly-once。
- shadow 的编辑结果存入 `shadow_publications`；空评论、空摘要、无 unknown 时不创建空 review。坐标拒绝与回执错误进入 RunHealth。
- 真实 keycloak 大仓库的采样栈发现 RESOURCE 上下文反复遍历目录和匹配 glob。只读 PR 快照新增文件列表、glob 选择、grep 结果缓存；不改变搜索范围、顺序、命中上限与内容。

新增 `scripts/benchmarks` 中可追踪的只读 runner 与严格裁判，修复旧脚本的过期配置接口、fork head/tarball 接线和 v4 评论导出；在 HTTP 层拦截全部 GitHub 写操作。每轮保留模型、模式、代码/脚本/工作集标识、LLM 输入/响应、账本、评论、事件与 token。裁判的匹配提示、0.7 门槛及一对一去重算法延用现有严格裁判，并保留回归测试。

用户批准首轮开发集统一使用服务器现有 `deepseek-v4-flash`，英文输出；这不是原 MiniMax-M3 最终验收。初次诊断试跑发现预算上限输出不能解析，正在采集原始 finish_reason 和 reasoning 用量；诊断尝试不计作质量结果。正式配对必须使用相同模型参数与同一 PR head，生产服务、配置、数据库和 main 不参与改动。

尚未完成的关键项：

第二轮 Windows/Python 3.12 全量测试：`1432 passed, 1 skipped, 6 warnings`；新增 Linux benchmark bootstrap 测试在 Windows 因 `fcntl` 跳过，由 dev CI 验证。两项严格裁判回归通过。ruff / format / spec-check 通过，未变更 main 部署。

Linux CI 随后发现 benchmark bootstrap 测试的进程环境变量未恢复；修正测试隔离后，`169c795` 的 dev CI 成功（Linux 1434 项测试通过）。首个固定提交试跑：上下文包含 58 units / 168 slices；生成/专项阶段 partial，未形成有效配对。并发尝试遇到 provider `429 rpm exhausted`，结果不用于 F1 宣称。诊断还发现新生成器没有显式传 SPEC 的 8192 输出上限，以及 token wrapper 的私有调用绕过了外层回调；补齐预算参数和 provider 层 trace，并采用跨进程限速后再跑。

随后原始回执确认两个可复现问题：生成器/lens 示例使用 `file.py:functionName`，与真实 `su_…` ID 不一致，模型产生的候选因未知 ID 被丢弃；两次调用 `finish_reason=length`，8192 个 completion tokens 全为 reasoning，正文为空。修正 ID 示例并显式列出每块允许的 ID；生成输入按实际渲染长度复核、过大块继续拆分，单 unit 无法容纳则 unresolved。取消 400 行静默截断，Context 总字符预算采用全包水位分配，专项 no_issue 不覆盖其它阶段的 unresolved。

同一服务商的探测证明 `thinking.type=disabled` 可用（reasoning tokens=0）。后续开发对照统一采用 DeepSeek 非思考模式和跨进程 30 秒限速；与默认思考模式诊断结果分开，不能混算或冒充原 MiniMax-M3 验收。该参数格式参考 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)，实际可用性以此服务器探测回执验证。

`96827a3` 的非思考单 PR 诊断仍为 partial：两个生成回执分别开始了 29 / 28 条假设，重复同一 unit/机制/anchor，耗尽 8192 输出预算；代码只在消费阶段截取最多 12 条，未把配置上限交付给模型。补齐 generator/lens 的动态数量约束及简洁、同身份合并要求。原格式修复未收到原响应而重复生成；改为仅交付原响应修复格式，缺事实或截断要求返回 null，不能用空数组冒充成功。仍保留一次修复、RIGHT 原文校验、severity 溢出处理，不增加候选过滤门槛。

本轮 Windows 全量测试：`1438 passed, 1 skipped, 6 warnings`，新增 3 项生成契约回归。此前 `96827a3` Linux dev CI 为 1438 项通过。本轮限流回执包含 `inference exceeds tpm/rpm limit`，30 秒间隔不足；下次开发诊断及正式配对均显式用 60 秒请求间隔，与前一轮结果分开保存。

`ac634db` 的 60 秒间隔单 PR 诊断已得到两个正常闭合的生成响应（finish_reason=stop，5706 / 3599 输出 tokens，各 12 条候选），不再因重复输出耗尽预算。专项响应也包含了语言不匹配候选，仍需调查及严格裁判，不能据此宣称召回提高。该提交 Linux dev CI 成功。

调查输入复核发现两项原规格实现偏差：原先交付整个文件 diff 而非该语义单元的 hunks；工具给模型 6000 字符但 observation 只保存 1200，未告知可引用边界。新增共用的 before/after hunk 选择函数，初始调查保留对应 unit 与全部附加 site 的相关 hunks，完整文件变化仍可用 read_diff 获取；较长结果显式分隔可引用 excerpt 和额外上下文，提示窄窗口重新取证。未放宽 grounded 校验或增加调查预算。

新增 hunk/证据边界回归后，Windows 全量测试 `1440 passed, 1 skipped, 6 warnings`；ruff / format 均通过。

新增只读 `context_snapshot.py`，记录固定 PR head 的语义单元、原始 ContextSlice、渲染与截断方向；不调用模型，重复渲染检查一致性。用于 SPEC 指定的三个大仓库实例抽查。

### 19:12 服务器连通性异常与次日恢复

在生产同机的隔离源码目录并行启动三个上下文抽查后，SSH 握手及 HTTP 开始超时。TCP 80 连接仍能建立，但 HEAD 请求无响应；SSH 未进入认证阶段。不能据此判定数据库/凭据故障，也不能在没有主机数据时确认 OOM。最近一次生产服务核对为 active、SHA 为上述 main；异常之后尚不能复核其运行状态。

本次 context 进程 PID 为 286190 / 286191 / 286192；已取消本机待连接的 v3 启动 SSH，避免恢复时再启动任务。已尝试 SSH 校验 cmdline 后停止这三个进程，但握手持续超时；请求用户通过云控制台/VNC 执行停止。当前不新增任何评测，优先恢复主机、读取负载/内存/OOM 日志、核对生产服务。并行且未设置主机资源上限是执行安排失误；后续所有同机评测须使用有总量上限的独立资源组，抽查串行。

2026-10-10 用户转述云平台提示“实例资源耗尽”，随后确认实例正在重启。内存/CPU/磁盘的具体告警与主机日志仍待恢复后核实；重启后的旧 PID 不可直接用于停止操作，必须重新核对进程身份。此次中断的诊断不能作为完整配对评测成绩。

新增评测专用启动器 `scripts/benchmarks/launch_isolated.py`：同机独占锁、生产服务/内存与磁盘余量检查、拒绝遗留评测；使用有内存/CPU/时长/进程数上限的 systemd 独立资源组。子进程在导入评测代码前读取实际 cgroup v2 限制，未生效则拒绝运行；中断停止本次服务并记录 failed。没有不设上限的降级路径。磁盘只做启动前余量检查，尚不是硬配额。这些保护仍需在恢复后的主机上通过小型限额探针验证，未验证前不启动真实评测。

本轮 Windows 全量测试 `1455 passed, 2 skipped, 6 warnings`；ruff / format 通过。新增资源边界、内核限额拒绝、运行中断/失败记账和跨进程独占锁测试；Linux flock 的两项测试由 dev CI 验证，Windows 跳过。

09:34 恢复核实：SSH 正常、HTTP 200、reviewforge.service active/enabled，生产 SHA 仍为上文 main。实例物理内存约 1.6 GiB，当时 available 约 1.1 GiB；旧评测进程均不存在。前一 boot 的已读取内核日志未发现明确 OOM 记录，尚不确定平台告警具体是哪项资源。初次 502 对应开机时后端尚在初始化，随后自行恢复，未改生产配置或代码。

`bac4959` 的 Linux dev CI 为 `1459 passed, 6 warnings`。09:35–09:36 在服务器上运行两个小型限额探针：实际 cgroup memory.max=64 MiB、memory.swap.max=0、cpu.max=10000/100000；另一个等待任务在 3 秒上限后终止，子进程也退出，记录为 failed，生产服务保持 active。上述限额保护已在该主机验证；探针不下载仓库、不调用模型，不计入质量成绩。

后续大仓库工作转到本机串行执行。评测工具适配 Windows 文件锁与信号处理，不改变模型提示、审查配置或裁判口径。顺便修正两个结果协议缺口：外层 completed 不能掩盖内层 partial；续跑不得混用不同代码/模型/工作集/参数或变化的 PR head。严格裁判要求请求集合全部运行完整后再计分，输入与裁判参数改变也不能复用旧判断，避免把缺失样本静默排除。

本轮 Windows 全量测试 `1469 passed, 1 skipped, 6 warnings`，原来只在 Linux 跑的 3 项 benchmark bootstrap 回归现在也在 Windows 通过；新增跨进程互斥、重启时间戳、混合配置拒绝与不完整样本拒绝回归。ruff / format 和两项严格裁判算法回归通过。

首次本机 context 试启动未进入仓库抽查：模型客户端初始化报缺凭据，而已打开的数据库线程没有释放，进程未正常结束。该尝试已停止，不能计作抽查或质量结果。修正 benchmark bootstrap 的失败清理；context 抽查改用独立的仓库 gateway/数据库运行时，不创建无用的模型客户端。另发现 Windows virtualenv 的 Python redirector 会派生真实解释器，不能只监控父 PID 的内存；新增 Windows Job Object 启动器，子进程导入任务前必须绑定并核对内核限额，整棵工作进程树受总内存/CPU限制和超时约束。

Windows 内核探针已验证：实际工作进程受限、512 MiB 分配在 128 MiB 工作组中被拒绝、3 秒超时终止孙进程及 redirector；7 项限额回归通过。新增 bootstrap 失败释放数据库/HTTP 与 context 不依赖 LLM 凭据的回归也通过。生产主机没有启动真实评测。

本轮 Windows 全量测试 `1478 passed, 1 skipped, 6 warnings`，ruff / format 和严格裁判回归通过。`a5112d5` 的 Linux dev CI 为 `1470 passed, 6 warnings`。恢复后的再次外部检查仍为 HTTP 200。

本轮定位的恢复缺口：原 frozen outbox 在恢复时跳过所有 LLM 阶段；若第一次已发布部分结果，但仍有 OPEN/可重试 UNKNOWN，就无法继续调查并发布新增确认问题。原因是把“冻结首次投递”误当成“全部调查已完成”，与 SPEC §4.8 的账本恢复要求不符。

`311552c` 的 Linux dev CI 为 `1476 passed, 3 skipped, 6 warnings`（Windows Job Object 内核探针在 Linux 跳过）。本机三个真实上下文采集已串行结束：Sentry 获得 15464 文件、22 units 的 tarball 快照；keycloak 与 grafana 因 Windows MAX_PATH 限制退化为 API fallback，代码 slices 为零，因此不能计为通过抽查。工作进程组峰值分别约 178 / 165 / 413 MiB，均在 2 GiB 内核总内存限制内，未在生产主机启动真实评测。

回溯 workspace 的原实现：正常临时目录路径适用于 Linux，却不能覆盖 Windows 上大仓库的合法长路径。新增微型 tarball 回归，先复现“一个无关长路径文件使整个仓库降级”，再仅对 Windows 物理快照根使用扩展绝对路径；逻辑仓库路径、SHA、归档安全校验与降级契约保持原规格。提取、读取、搜索、manifest 和清理共用同一根路径，不依赖修改操作系统注册表。上下文审计显式记录 snapshot 来源、文件数与 slices，并可用 `--require-tarball` 拒绝把降级诊断算成成功；仍保留原始诊断文件。

本轮 Windows 全量测试 `1480 passed, 1 skipped, 6 warnings`；长路径、安全归档、上下文与 bootstrap 的 29 项回归通过，ruff / format 通过。接下来固定本轮提交，重新进行串行、受限的真实 ContextPack 抽查。尚无有效 v3/v4 配对 F1。

补齐恢复发布协议：首次 outbox 保持原负载与原 delivery key；新增 `v4_review_supplements` 追加独立冻结批次。每批记录已表达的 confirmed IDs、聚类键与事实摘要（含 sites），恢复时先逐批核对回执，投递不明则仅核对远端，不继续模型和新写入。核对完成后 OPEN / 可重试 UNKNOWN 继续调查，CONFIRMED / REFUTED 跳过；生成/lens 只在 unresolved 时重试。新增确认事实或同一问题的新位置经确定性模板补发，复用既有 editor fallback 规则，不再次调用 Editor LLM；5 / 8 评论上限按整个 run 累计，同簇补充进摘要。首次冻结记录没有覆盖元数据且仍需续跑时，不能猜测已发表问题，要求在隔离环境新建 run。

新增恢复回归覆盖：重开 SQLite 后继续调查；first payload 不变；两档全局 inline 上限；补发回执丢失后只读核对；重复恢复不调用 LLM、不重新 POST；同一确认身份连续新增两个 sites 均追加可见摘要；旧回执未明确时不启动后续模型。37 项相关回归通过，ruff / format 与严格裁判算法回归通过。全量测试中 1482 项通过，3 个 Windows 启动器内核探针被正在采集的独占锁拒绝（预期的互斥保护）；待串行采集结束后再单独验证这 3 项，不能将本次全量命令报告为全部通过。

真实大仓库重新采集：`74058dc` 首次 keycloak 请求发生空消息异常并保存降级诊断，新增异常类型日志后固定 `18bf939` 重试。后者 keycloak 为 10252 文件 / 68 pack units / 624 slices，grafana 为 16203 文件 / 8 units / 94 slices，均为 tarball。内容抽查发现注释被误认作定义，以及同名声明共用 unit ID、后者在 ContextPack 字典覆盖前者；接下来只在 v4 分支校正声明与唯一单元交付，legacy 的符号/manifest 行为保持原样。此轮抽查尚不能判定全部质量门槛通过。

`18bf939` 三个采集均完成，Sentry 为 15464 文件 / 22 pack units / 248 slices；工作组峰值内存约 317 / 362 / 414 MiB。实际 keycloak 输入是 77 个语义单元，但 pack 字典只保留 68 个；Sentry 是 23 → 22。重复来自同名类/构造函数/重载。另一个直接证据：AuthenticationError.java 的 `subclass of this interface` 被旧 `class\s+(\w+)` 正则识别成名为 `of` 的类，作为 `Map.of` 的 callee 交付了整段无关注释。

新增 v4 专用 `declarations_v4.py`：沿用语言提取与源码范围，但声明名字必须位于代码，而非注释/字符串；普通 unit IDs 保持一致，同名冲突按声明类型与范围区分，完全重复的 manifest 行合并且保留全部 RIGHT 行。ContextPack 遇到重复 ID 明确拒绝，不再静默覆盖。workspace、pack 与新的 v4 manifest 使用相同声明规则；`ContextEngine` 只新增显式 v4 分支，shadow 在 legacy 发布后重新构建 v4 manifest。legacy 的 extractor/compiler、默认 ContextEngine 调用及评测口径未改。

新增 4 项源码/重载/重复行/manifest 回归，验证真实注释误识别、所有行保留、同 head 重编译稳定，以及 legacy 行为不变。串行采集结束后 7 项 Windows 限额探针通过；本轮完整检查为 `1489 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。`8bc641d` Linux CI 为 `1483 passed, 3 skipped, 6 warnings`。下一步固定声明修正的提交，复查实际上下文与开发集对照，质量结论仍待完整评测。

`e92e136` 的 Linux CI 为 `1487 passed, 3 skipped, 6 warnings`。固定该提交复查：keycloak#36880 的 77 个语义单元全部保留，732 段源码上下文；grafana#97529 的 8 个单元全部保留，94 段上下文。两者 head 与 slices 的 SHA 一致，分别渲染 40000 / 37651 字符，进程组峰值约 319 / 361 MiB。Sentry#80168 本次 tarball 下载发生 `ConnectError`，严格抽查记为失败，保留退化诊断；不把零 slices 算作通过，待同提交串行重试。

另按 SPEC §4.3 修正假设身份：旧实现直接采用模型填的 `anchor_symbol`，同一 unit/机制可因任意命名产生重复假设。现在从固定 head 的真实源码用 `_find_enclosing_function` 定位 site 所在的最内层函数；没有函数或源码不可用时回退 `unit.symbol`，不发明资源文件的身份规则。生成器与专项 lens 共用按文件缓存的解析器。新增重复命名、嵌套函数、不可用源码/资源文件回归；完整检查为 `1492 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。生产服务与 main 未改，尚无有效完整配对质量成绩。

`04af96b` dev CI 成功。Sentry 同 `e92e136` 提交的串行重试成功：23 个语义/上下文单元全部保留、260 段源码、40000 字符、进程组峰值约 414 MiB。三仓库审计均来自完整 tarball，所有 slice 的 SHA 与固定 head 相同；失败尝试仍留存，本机审计汇总为 `.reviewforge/benchmarks/local-contexts-e92e136-20261010/audit-summary.json`。这验证上下文交付，尚不等于质量提升。

补齐可执行的 Phase 2 测量入口：严格裁判新增显式 `--ledger-recall`，重用原 claim 池选择，并用相同裁判和一对一匹配分别测 CONFIRMED+OPEN+UNKNOWN、CONFIRMED、REFUTED；生成/确认召回与误杀数单独写入 `ledger_metrics`，不混入发布评论 F1。拒绝 head 不一致或畸形账本；参数与 helper hash 固定续跑身份。同时修正裁判请求失败后静默排除样本的缺口：所有请求成功前只保存 partial 诊断、不输出完整集合质量分，失败请求可在同输入下重试。新增 5 项回归（含失败后恢复），完整检查为 `1497 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。恢复期间核对生产仍 active、HTTP 200、main SHA 未变。

冻结对照前发现旧 benchmark 的 model override 只改了 legacy profiles 的模型名，仍留下 fast/accurate 的不同温度和输出预算；SPEC §7 明确要求清空 profiles。现只在显式 `--model-override` 下清空角色覆盖与旧 profiles，所有角色走同一全局模型；增强现有 bootstrap 回归验证实际路由，无生产路由变更。`fcdac99` 的 dev CI 成功；此修正后完整测试仍为 `1497 passed, 1 skipped, 6 warnings`，ruff / format 通过。旧单 PR 回执继续仅作诊断，不与新协议混成成绩。

`a209043` dev CI 成功。本机首个 dev PR keycloak#37429 已取得完整 tarball（10310 文件），58 units / 168 slices，固定 head 为 `02f48f776f43734d1ac8914d3ad6a7115acdcb33`。实跑发现此前 `llm.max_retries = 0` 在客户端创建后设置，底层 SDK 仍为 2，出现两次连续 429 重试。核对本机 LangChain/SDK 实现并用 MockTransport 重现：原逻辑一次请求实际发出 3 次 HTTP。停止已验证的本机工作进程，启动器完成失败记录与资源组清理，峰值约 333 MiB；两次生成回执、11 个最新唯一假设与中断标记留存，非质量成绩。

根本修正为在构造 provider 客户端前传入重试策略：ModelRouter 新增显式参数，默认仍为 2；仅 benchmark 使用 0，严格裁判的 Anthropic 分支也显式为 0。metadata 记录 `sdk_max_retries: 0`。新增实际 SDK 的 429 回归验证恰好一次 HTTP 与可见失败记录，原默认 OpenAI/Anthropic 行为回归保留为 2。完整测试为 `1498 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。下一次对照使用新的提交和输出目录，不续用此中断诊断。

`017afde` dev CI 成功。新实跑已无 SDK 重试突发，但 60 秒间隔下两个生成块和首个 localization 块返回 429；小块 localization 与调查请求能够成功。生成失败留下 58 个 unresolved units，停止继续消耗额度并保留中断诊断（进程组峰值约 333 MiB）。单请求原始错误核实为 `429003: inference exceeds tpm/rpm limit`，不能仅据文本确定是 TPM 还是 RPM。独立接入探针约 31k 输入 tokens 的长请求被拒绝，约 13k 输入 tokens 的较短请求成功；探针截断输入、输出仅 16 tokens，明确不作为代码审查或质量成绩。

据此增加显式开发消融参数 `--generator-max-input-chars`（0 保持现有配置），接入既有语义单元分块；metadata 固定原始 override 和 effective budget。同时修正专项 lens 未继承 generator 输入与 ContextPack 字符预算的接线缺口，默认值均不变。新增小预算下所有单元、完整变更与 no_issue 覆盖仍保留的回归，增强 bootstrap 验证实际配置；完整测试为 `1499 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。接下来用 50000 字符输入块、65 秒请求间隔进行新的单 PR 开发诊断；不复用旧目录或旧成绩。

`3170802` dev CI 成功。50000/65 秒开发消融仍不稳定：6 次生成请求，3 次成功、3 次 429，记录 52437 tokens；停止时最新账本为 22 个唯一 OPEN 假设、18 个 no_issue units、25 个 unresolved units。已停止验证过的本机工作进程，启动器关闭资源组并写失败记录，峰值约 333 MiB。`.reviewforge/benchmarks/deepseek-dev-pair-3170802-50k-65s-20261010/outcome.json` 明确标记非质量结果，全部原始回执/账本保留。尚未形成有效完整配对，未跑 holdout、未切默认、未发布 v4 到 main。

已询问用户 SenseNova 控制台的实际 TPM/RPM 上限及是否共享 key；不能把通用 `tpm/rpm limit` 文本当成某个已知上限，也不继续盲调输入大小/重试次数。最近生产复核仍 active、HTTP 200、main SHA 不变，生产数据库近一小时没有 token usage 记录；这不能排除该 key 在其它应用的用量或模型服务整体限流。待核实限额后继续真实对照。另有诊断待分析：此前一个语法正确的生成回执覆盖 38 个输入单元中的 34 个，需核对省略的 4 个是否属于明确排除项；不能仅凭返回 JSON 合法就宣称全部单元已审查。

1. 大型 PR 分块和截断覆盖的真实边界、调查输入与 unit hunk 的一致性。
2. 复核 detector 种子的确认语义与未映射类别，避免未经验证的命中直接成为强证据问题。
3. 完成开发集漏斗诊断、配对指标和 ContextPack 实例抽查，达标后再进入 holdout。

然后按 SPEC 运行：相同模型、英文输出，dev10 上 legacy 对照与 shadow 调查漏斗；抽查 keycloak#36880、grafana#97529、sentry#80168 的实际 ContextPack。达标后才进行 holdout40 两轮配对验收；holdout 不用于调参。记录账本召回、调查误杀、发布遗漏、重复误报与 token 消耗。通过质量与运行可靠性门槛后，才把 v4 推入 `main` 并切换生产默认。

## 2026-10-10 开始验收：运行完整性与覆盖检查

用户要求开始验收，冻结 `8db1e4e`（审查代码与 `3170802` 相同），继续采用已授权的 `deepseek-v4-flash`、英文、非思考模式、全局统一路由、SDK 重试 0。计划先 dev10，再按门槛进入 holdout40 两轮；原 SPEC 的 MiniMax-M3 最终验收仍单独待完成。协议与诊断位于 `.reviewforge/benchmarks/v4-acceptance-deepseek-20261010-154929/`，旧中断记录不复用为成绩。

本机启动 keycloak#37429 的 legacy 对照，固定 head `02f48f776f43734d1ac8914d3ad6a7115acdcb33`，请求间隔 65 秒，进程树上限 2 GiB / CPU 10%，不在生产同机运行，也不写 GitHub。必要的 Planner 请求（30459 输入字符）被 HTTP 429 / `429003: inference exceeds tpm/rpm limit` 拒绝；两个较小的 localization 请求成功，共记录 2701 tokens。不能据此判定具体 TPM/RPM 上限。由于必要阶段已失败，停止验证过的工作进程；启动器关闭资源组，峰值约 125 MiB。`outcome.json` 明确记录 0 个有效配对、0 次裁判、未使用 holdout，未输出 P/R/F1。首个启动尝试发生日志文件名冲突、未发模型请求；失败记录保留，第二次启动采用独立记录文件。

离线核对旧生成回执：两个成功响应合计交付 58 个 units，其中一块 38 个只返回 34 个 unit 判断。省略的四个是 `VerifyMessagePropertiesTest.java` 的 `verifyNoChangedAnchors`、`verifyIllegalHtmlTagDetected`、`verifyNoHtmlAllowed`、`verifyDuplicateKeysDetected`，不属于风格排除项。原设计假设是“JSON 解析成功就可清除整个块的 unresolved”，但 prompt 已要求每个无问题 unit 返回带 checked 边界的 `no_issue_units`；真实回执证明两者不能等同。

修正 generator 与共用该实现的 lens：按本次响应的 unit 判断记账，只有明确返回的 unit 才能清除本来源的失败；省略项进入 unresolved 并使 run partial，不能沿用旧 no_issue 冒充本轮检查。保留其它来源的失败；有效候选仍按既有 schema / RIGHT site / 数量上限处理，候选丢弃与 unit 是否返回判断分开计量。明确无问题的响应正常完成，不增加“再找一次”或格式修复调用。两份 prompt 明确每个 Allowed unit_id 的返回约束。

先以回归复现 4 项失败，再修正；新增 7 项覆盖测试（含 generator/lens、省略四个真实方法、明确无问题、恢复不清空漏报失败、空/未知 ID、pipeline health 与严格裁判准入）。离线回执审计 `coverage-ack-audit.json` 保留输入/输出 hash，无新 LLM 调用。完整检查为 `1506 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。生产复核仍 active、HTTP 200、main SHA `00c667556c88241d72d96f24430c7c16b4add24e`。

当前结论：覆盖完整性修正通过本地检查；真实质量验收尚未完成，模型服务限流仍阻止有效配对。默认 legacy、main 部署与阈值均未改，未调整 goldens。下一次真实对照必须冻结修正后的提交、使用新输出目录，并在获得稳定模型额度后运行完整 dev10。

## 2026-10-10 新中转站开发集预检

用户在控制台完成中转站切换后，复核五个角色均使用 `deepseek-v4-flash-0731`，无独立角色端点、模型或密钥覆盖。非思考模式接入探针返回 HTTP 200、实际模型标识与请求一致，记录 10 tokens。生产服务仍 active、根页面 HTTP 200，main SHA `00c667556c88241d72d96f24430c7c16b4add24e` 不变。

冻结审查源码 `987a574`，从 Git 创建独立快照；Windows 归档关闭 autocrlf 转换以保留源文件字节与固定 hash。新输出位于 `.reviewforge/benchmarks/v4-acceptance-new-relay-20261010-173510/`。本轮使用代码默认的 120000 字符生成输入预算、30 秒请求间隔、英文、非思考模式、统一模型路由、SDK 重试 0。旧站 50000/65 秒消融与中断数据不复用为成绩。评测在本机 Job Object 中运行，进程树上限 2 GiB / CPU 10%，保留内存与磁盘余量，不写 GitHub，也不在生产服务器运行审查任务。

完成 dev10 首个 PR `keycloak/keycloak#37429` 的 hypothesis 预检，head 固定为 `02f48f776f43734d1ac8914d3ad6a7115acdcb33`。完整仓库 10310 个文件、约 89.3 MB，未截断；ContextPack 包含 58 个 units / 168 个 slices，按现有预算渲染 40000 字符。47 次模型请求全部成功，支持大输入与工具调用；流程耗时约 1768 秒，进程组峰值约 357 MiB。接入与资源隔离正常，先前的旧站 429 不再是本轮阻塞原因。

本轮审查仍为 `partial`，不是通过验收：

- 生成器两个响应共接受 15 条假设，localization lens 接受 13 条，账本合计 28 条；每 PR 调查预算 12 条，16 条以 `budget-exhausted` 留为 UNKNOWN。
- 已调查的 12 条中，2 条 CONFIRMED、8 条 `token-exhausted`、2 条 `ungrounded`；42 次调查请求共记录 175690 tokens。UNKNOWN 总数 26，未将预算耗尽或证据不足当作无问题。
- 38-unit 生成块只明确返回 34 个 unit 判断，漏答仍是 `VerifyMessagePropertiesTest.java` 的四个方法：`verifyNoChangedAnchors`、`verifyIllegalHtmlTagDetected`、`verifyNoHtmlAllowed`、`verifyDuplicateKeysDetected`。原始输入确实包含它们，响应 finish_reason 为 stop，四个 ID 在 hypotheses 与 no_issue_units 中都不存在。完整性修正正确拦住了这种省略。
- 实际输出 1 条行内评论，记录用量 304631 tokens（输入 281038 / 输出 23593）：生成器 70180、lens 54242、调查 175690、editor 4519。超过每 PR 250000 tokens 的目标，费用没有从 token 数推算。

`outcome.json`、`usage-report.json`、原始回执与 `unit-coverage-audit.json` 已保存。严格裁判准入确认该 review 未完成，未调用裁判、未输出 P/R/F1，0 个有效配对。未启动 legacy 对照、未扩量到完整 dev10、未使用 holdout。评测进程正常退出、资源组关闭；生产 main、部署和默认 legacy 未变。

下一步应优先改善生成单元覆盖、候选质量与预算内的调查收敛，再用新输出目录完成预检和 dev10 对照。不能删除 UNKNOWN、把漏答改成 no_issue、提高分数准入宽容度，或用放大调查预算掩盖当前问题。MiniMax-M3 的原规格最终验收仍未完成。

## 2026-10-10 预算内收敛与生成覆盖优化

针对上述预检继续开发。回查 SPEC §4.4/§4.6 与实现：原调查员假设工具循环结束后还留有结论额度，但真实回执显示完整工具历史不断增加输入成本，8 条候选在结论前耗尽 tokens；长度除以四的估算还漏计工具参数/schema，并低估中文指令。原生成器输入已有逐单元信息，但最终响应处没有本块返回清单，模型连续漏掉同一组四个测试方法。Issue 库当前为空，没有可复用的 Owner 分析。

先以回归复现预算耗尽和缺少分块清单，再修正：

- 调查员每轮先预留结论输入及输出，必要时提前切换无工具的结论调用。结论使用原始假设/上下文和代码保存的 Observation excerpts，不重放完整工具结果与探索文字；既有 steps × 4000、每 PR 12 条调查上限与证据门槛不变。
- 输入预测计入工具参数/schema 和非 ASCII 文本，根据 provider 已测 input usage 校正额外开销；实际用量如实记账，超支保留 UNKNOWN。小预算也可先读取事实再收尾，解析失败的结论不重新开放工具。
- generator/lens 输入末尾列出本块全部 ID 与数量，包括测试/fixture；清单仍计入原输入上限。漏答检测、不增加补找重试及严格裁判准入均保留。
- 提示词要求候选以已观察的不一致为起点，比较 before/after，核对负向测试预期、合并相同 trigger/impact/事实问题；调查直接读取已知行窗口，回答问题后停止扩展探索。

新增 6 项调查回归，并强化分块清单只包含本块 ID 的预算检查。覆盖提前收尾、未保存/未找到的证据不能确认、结论 provider 失败可重试、单步预算、工具参数预测及真实 usage 校正。完整本地检查 `1512 passed, 1 skipped, 6 warnings`；ruff / format / spec-check 与严格裁判算法回归通过。将使用新的冻结提交和输出目录，在相同模型与 PR head 上做预检；尚无本轮真实质量分。

冻结 `33deded` 实测同一个 head，结果位于 `.reviewforge/benchmarks/v4-convergence-20261010-234459/`。49 次请求全部成功，58 个单元均返回判断，unresolved 从 4 降至 0；12 条调查全部在预算内返回三值结论，`token-exhausted` / `ungrounded` 均从 8 / 2 降到 0。账本为 6 CONFIRMED / 1 REFUTED / 24 UNKNOWN，其中 19 条未获调查预算、5 条调查后证据不足。实际行内评论 3 条，用量 318883 tokens（GEN 71243 / lens 63362 / INV 173490 / editor 10788），仍超过 250000，耗时 1888.61 秒。候选从 28 增至 31，说明提示词调整尚未解决噪音；不能把更多 CONFIRMED 当作质量更高。内层仍 partial，严格准入拒绝评分，未运行 legacy 或 holdout。

## 2026-10-11 共享变更与上下文呈现

回查 blame 与模块注释：此前 `c50ed3b` / `bc9bc15` 补回 before/after hunks 是为保留被删 guard/lock 的行为变化，这个约束仍成立；但为每个 unit 复制完整 hunk 和全部右侧代码不必要，也让同一根因在多个单位里反复出现。当前预检的离线审计发现 GEN 两个块重复 hunk 33845 字符。只加提示词没有解决该输入结构问题。

改为每块每文件共享 hunk，RIGHT 坐标在代码旁就地标注，各 unit 保留独立 ID/符号/可用行号。坐标仍由既有统一 diff 解析器生成，删除、metadata、畸形未映射行保持原文且不发明坐标；重复代码按 patch index 区分，不按文本匹配。分块按共享渲染后的长度计量，不能在估算时又重复累计。跨块仍各自携带需要的完整变更。

对已按原 40000 字符水位交付的 ContextPack 视图，块内相同 path/范围/SHA/正文的 source 仅交付一次，其它 unit 保留 kind/reason/header 并引用前方片段；不扩大预算、不恢复截断正文、不跨块引用，investigator 的独立上下文不变。既有单元漏答门槛、每次最多 12 条候选、每 PR 12 条调查与 grounding 校验均保留。

离线按捕获输入压缩（不调用模型、不打质量分）保留全部 units / hunks 与相同 RIGHT 坐标正文，五个输入合计节省 80016 字符，记录 `shared-input-audit.json`。这不是实际 token 消耗或质量成绩；下一轮需冻结新提交重新实测。新增测试覆盖共享 hunk/预算、跨块源码、删除 guard、元数据/畸形重复行、空行/尾空格、共享上下文边界、来源差异与截断不复原。

本轮新增 14 个参数化/独立回归；完整本地 `1526 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。检查结束后再建立新的源码快照和输出目录，模型、数据集、预算与资源限制保持一致。

冻结 `78aee14` 的第二轮结果位于 `.reviewforge/benchmarks/v4-shared-inputs-20261011-002149/`。同一 PR head、模型和预算下，38 次请求全部成功；GEN 2 / lens 1 / INV 34 / editor 1 次。58 units 均返回判断，unresolved、token-exhausted、ungrounded 和 error-severity UNKNOWN 均为 0，内层首次 completed，严格裁判准入通过。账本为 5 CONFIRMED / 2 REFUTED / 21 UNKNOWN（16 条预算未调查、5 条证据不足）。用量 230363 tokens（GEN 50453 / lens 44677 / INV 127718 / editor 7515），较上一轮减少约 28%，低于单 PR 250000 目标；耗时 1503.625 秒，较上一轮减少约 20%。候选仍为 28 条，噪音问题未解决。

冻结相同 `78aee14` 裁判严格评分，全部请求完成，裁判额外记录 9097 tokens。一个 PR 的实际发布成绩：ReviewForge 0 TP / 1 FP / 4 FN，P/R/F1 均为 0；历史 Qodo-v2 为 1 TP / 1 FP / 3 FN，P=0.5、R=0.25、F1=0.3333。未把调查候选当作实际发布成绩。confirmed 池中两条真实匹配（立陶宛 loginTotpStep1 与中文 account totpStep1）在发布时丢失。裁判输出的 ledger 辅助召回 3/4、refuted_goldens=1，抽查发现方法名拼写与 matcher 状态错误、templateHelp 与 totpStep1 被跨原因/键配对；保留原始输出及阈值，不以这些辅助匹配宣称漏杀或进步。未运行 legacy 配对、完整 dev10 或 holdout；该单 PR 结果不构成整体验收。

## 2026-10-11 变更归因与发布聚类修正

回查 SPEC §4.6/§4.7、模块注释与 `8466d395`：原编辑器假设 `(mechanism, anchor_symbol)` 足以跨 unit 表示同根因。真实账本五条不同资源问题均为 `(i18n, "")`，被代码强制合成一簇；模型只返回其中一条评论，簇完整性保护正确拒收部分结果，却随后以错误的首条 claim 为模板混合全部 sites。保留簇完整性保护与发布上限，修正“空符号代表同因”的假设；Issue 库仍为空。

- 已命名 anchor 加上主 site 文件 scope，避免不同文件同名函数自动合并；空符号和 `<module>` 使用独立 unit scope。显式多 site 假设保持原位置；模型仍可在证据支持同一处修复时合并跨簇候选。
- 调查工具 `read_diff` 支持 RIGHT 行窗口，保留相交完整 before/after hunk；默认全 diff 兼容。窄读避免目标反证位于长结果的未保存部分。不存在/未命中保留 not_found，不成为反证。
- 调查提示先核对新触发条件/后果，再比较 before/after；旧代码可被新调用或配置触发，不能仅凭代码曾存在就推翻。原始 diff 复核：中文 account 的简体变繁体确实由本 PR 引入，admin templateHelp 则仍为简体且已正确推翻。
- 一条 formatter 候选真实响应的 reason 明确推翻，但 verdict 填 confirmed；改为先写事实与理由、最后选一致的三值结论。仅引用 import/构造/方法名不足以证明 API 能力。这是提示词改进，不能声称已在代码层解决语义矛盾，需新冻结轮次实测。

新增 3 项窄 diff 回归及 7 项编辑聚类/降级回归；六项旧实现的错误合并已先复现，再修正，原同文件命名符号合并和全部 sites 校验继续通过。完整本地 `1536 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过。尚待新的冻结提交真实预检，不提高预算、不改数据集/goldens/裁判阈值，main 仍保留 legacy 生产版本。

冻结 `3bcbca0` 的第三轮结果位于 `.reviewforge/benchmarks/v4-grounded-publication-20261011-010626/`。同一 head / 完整仓库摘要 / 58 units / 模型与限制，47 次请求均成功；GEN 17 + lens 9 条，共 26 条候选，无漏答。调查 4 CONFIRMED / 4 REFUTED / 18 UNKNOWN（14 条超 PR 候选预算、2 条无充分契约证据、2 条 ungrounded）。编辑实际保留 4 条独立评论，空 anchor 不再压成一簇；4 条 error-severity UNKNOWN 使内层 partial，严格裁判拒绝评分。用量回到 270232（GEN 50604 / lens 44082 / INV 168946 / editor 6600），耗时 1858.922 秒，峰值约 358 MiB。这一轮在发布覆盖上验证了修正，在运行完整性和用量上仍失败；没有质量分、legacy 配对或 holdout。

## 2026-10-11 原始源码证据与格式契约

回查 `7db82230` 的 Observation 捕获与 workspace 的 range reader：原设计保存带逐行 `N: ` 前缀的展示文本，隐含假设是模型多行引用也会包含这些展示前缀。两条真实结论的引用是正确的源码子串，却被该表示差异判为 ungrounded。`evidence-layout-audit.json` 保存原始输入/输出 hash，仅离线验证这两条 quote 去掉展示前缀后与已保存源码一致；不修改旧 verdict、完成状态或成绩。

调查员改从固定 workspace 读取正文，再用原范围边界逻辑本地切片。Observation 保存原始正文，path / line_range 为独立元数据；保留缩进、空行、尾空白、真正的数字前缀，不猜测剥除源码中的内容。API fallback 仍走异步固定 SHA 读取，workspace 的原带行号展示 API 和 legacy 均保留。证据仍须精确命中成功 Observation 的已保存 1200 字符，未保存文字和伪造代码继续拒收；工具 6000 字符、调查总预算、候选上限和 UNKNOWN gate 不变。

另两条关键 UNKNOWN 来自无具体消费端冲突的格式语法猜测。上一轮新增“必须有 implementation/test”的提示过于限制标准库契约：库源码不一定包含在目标仓库中。调整为追踪实际消费端，再核对其实现或已文档化的标准契约；import 或类名不能单独证明数据流。依据 [Java SE MessageFormat 官方语法与使用说明](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/text/MessageFormat.html)，明确 choice 子格式与重复 argument index 是受支持的能力，不能将邻接格式项误认为非法嵌套；这个规则不能套用于未核实的前端消费端。指南以 `prompts_v4/localization_contracts.md` 仅注入 v4 localization lens，共享 SKILL.md 未改，保留 legacy 对照边界。

生成器与 lens 要求拒绝“陌生 token 就不支持”及“非法输入有异常就有缺陷”的无事实前提候选，仍完整返回每个 unit 的检查边界；未新增发现重试或代码侧类别过滤。编辑规则允许同一具体错误及修复策略覆盖多处，避免把“不同文件”本身当作反向证据；不同原因的同类问题保持分开。

新增 3 项真实 workspace 的多行精确引用、数字前缀/空白保真、未保存内容拒绝回归，以及 1 项 v4 专项上下文隔离回归。已先复现原行号污染，再修正；小预算读证据后收尾的回归继续通过。完整本地 `1540 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判算法回归通过，待新提交独立冻结实测；不复用第三轮 partial 为成绩。

冻结 `27f2bc4` 的第四轮位于 `.reviewforge/benchmarks/v4-source-evidence-20261011-015203/`。生成/lens 共 26 条候选，58 units 无漏答；已保存的多行源码引用可以通过 grounding。但中文 account 的直接字形契约被调查员误要求提供模板引用，仍产生不可重试的 error UNKNOWN。该轮已无法通过严格准入，遂停止剩余调查，Job Object 返回 125，评测锁已释放。取消时记录 35 个成功请求、220345 tokens，账本为 3 CONFIRMED / 1 REFUTED / 16 UNKNOWN / 6 OPEN；无完整 review、发布或成绩，进行中的请求可能有未记录用量。原始记录保留，不拼接前轮结果。

## 2026-10-11 本地资源与运行时验证边界

两问回溯：上一批要求“受支持输入/调用方”是为了排除预期 fail-fast 和陌生 formatter 猜测，这个运行时约束仍成立；但原 localization skill 已把声明 locale 视为本地内容契约，直接语言/字形错误不需要模板调用方证明。Issue 库仍为空。问题在调查输入和问题设计，不能通过放宽 UNKNOWN 或自动确认 i18n 来解决。

调查输入对主 unit 为 resource 的 i18n 候选额外交付相关文件的 path/provenance，保留每个 site 自己的 locale；metadata 本身不可作缺陷证据。直接字形/语言违规比较变更文本与声明契约；格式参数/语法和运行时后果仍要求实际消费端。生成器/lens 的 open_question 与 refutation 必须决定 claim 的契约，避免以无关页面引用为前提。共享 skill 与 legacy 保持原版本，Observation 精确引用、预算及严格裁判不变。

新增 7 项回归覆盖多文件 locale 不串用、无关资源不注入、runtime/nonresource/unmatched 不免检、收尾保留契约，以及没有成功源码 Observation 仍拒绝确认。完整本地 `1547 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 和严格裁判回归通过。生产检查为 service active / HTTP 200，main SHA 仍为 `00c6675`。下一轮须从新的干净提交独立冻结，不使用 holdout，不把取消的第四轮算作完整质量结果。

冻结 `48803c9` 的第五轮位于 `.reviewforge/benchmarks/v4-contract-boundary-20261011-022153/`。35 个请求全部成功，58 units 无漏答，23 条候选 → 4 CONFIRMED / 4 REFUTED / 15 UNKNOWN（11 条 PR 调查预算外、3 条契约证据不足、1 条 warning ungrounded）。error UNKNOWN 为 0，内层 completed，严格裁判准入通过；实际发布 3 条评论，立陶宛 login/account 同错误合并且保留两处位置，中文 account 独立发布。中文调查由 16902 降至 8369 tokens，立陶宛 login 由 18003 降至 5965；全 PR 222319（GEN 52042 / lens 45205 / INV 118954 / editor 6118），1194.375 秒。候选噪音仍包含测试缺口、CVE 猜测与格式猜测，尚无本轮质量分，不能把候选确认数量当作质量成绩。

## 2026-10-11 专项契约知识的阶段交付

第五轮有一条 frontend 格式候选被推翻，理由把 react-i18next 当成单花括号 ICU，且引用只有其它资源文本和 import，没有目标消费配置。`contract-proof-audit.json` 保存原始请求/响应 hash 和诊断，不改原 verdict 或裁判。依据 [i18next 插值文档](https://www.i18next.com/translation-function/interpolation) 与 [react-i18next ICU 设置文档](https://react.i18next.com/misc/using-with-icu-format)，默认双花括号、可配置前后缀、ICU 需另行启用；这些事实不能代替目标仓库的实际配置证明。

回溯 `7db82230` 的调查输入与 `27f2bc4` 的专项规则注入：指南只在 lens 中交付，通用 generator 和最终 investigator 会丢掉这份知识，这是输入/输出不衔接的问题。新增 v4 `verification_guidance.py`，沿用原 localization 路径匹配，不改变 lens 触发；生成器只在相关块交付且计入预算，lens 系统规则交付一次，调查员按 i18n 或相关 resource 交付并在收尾保留。指南说明 Java/i18next/ICU 默认与配置边界，要求追踪加载/转换、实际格式调用及初始化，import 或无匹配搜索不足以建立反证。仍由调查员判断，精确 Observation 校验与 UNKNOWN 门槛不变；共享 legacy skill 未改，未新增模型阶段或发现重试。

新增 6 项上下文交付/隔离回归，强化 lens 指南仅出现一次、真实双花括号文本保真、收尾保留及分块水位计量。完整本地 `1553 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 和严格裁判回归通过；下一次真实运行需要新的源码快照，不能将第五轮当成此批交付修正的测评。

第五轮固定裁判已结束，额外用量 4970 tokens：实际评论为 2 TP / 1 FP / 2 FN，P=0.6667、R=0.5、F1=0.5714；同一 PR 的历史 Qodo-v2 为 1 TP / 1 FP / 3 FN，F1=0.3333。仍只是一个开发集 PR，没有 legacy 配对、完整 dev10 或 holdout。误报是“缺 English 文件时未捕获 RuntimeException”，原证据证明了传播，但没证明失败方式违反约定。漏报一个 anchor 验证问题；另一 golden 是纯方法命名建议，现有规范排除该项，仍按原裁判计 FN，不调整 goldens、阈值或分母。

## 2026-10-11 调查预算覆盖与可回答的问题

回查 `7db82230` 的 severity → sites → identity 排序：有限预算下按位置数和哈希身份排列，同类候选易占满名额。第五轮 23 条候选实际上均只有一个 site，anchor 数量验证候选为 budget-exhausted；此样本的偏置来自同类数量和 identity，而非较多 sites。按同一严重级别内的 mechanism 轮转，组内仍 sites → identity；error 始终优先，12 条上限、闭合候选不重跑、超出 UNKNOWN 均保持。

`allocation-audit.json` 只在内存中回放旧候选排序，无模型调用、原 verdict 不改。新规则仅多选了 normalizeValue 候选，仍未选到 anchor；它证明了机制覆盖变化，不能宣称已修复该漏报。生成器现有跨块输入只提供已有 identity/claim，输出候选必须使用本块 unit，跨 unit 追加 sites 的操作没有完整协议；本批没有通过放松范围校验去实现跨块合并。先明确已有候选的 status 和当前 unit 可用 checked 引用覆盖，保留这个接口限制。

另一个真实输入缺口是 generator/lens 不知道调查工具能力，曾提出外部 CVE 查询，而 Investigator 只有五个固定仓库读取工具。新增共享能力卡，并通过回归核对实际工具名称；外部事实必须已有具体材料，不能凭新依赖猜测漏洞。提示补充同一对象在检查/读取之间的状态推进、基于实际类型的 [Java Matcher 契约](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/regex/Matcher.html)，以及比较同一输入的实际/预期失败结果，避免仅以异常传播判缺陷。未增加工具、执行能力或模型阶段。

新增 5 项并发/严重级别/单机制/零预算/闭合状态预算回归和 1 项工具边界回归；完整本地 `1559 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判回归通过。补充 existing 状态呈现后，相关 63 项回归通过。需新的干净快照实测候选、调查和实际评论；尚不能以离线排序回放推导质量分。

冻结 `4315a6c` 的第六轮位于 `.reviewforge/benchmarks/v4-mechanism-breadth-20261011-031314/`。候选增至 28 条；“anchor Matcher 跨 key 泄漏”被源码反证，但并未提出真实的 anchor 数量验证缺陷。原异常传播候选再次 CONFIRMED，reason 只有“调用方没捕获所以确认”，没有预期错误处理契约。fixture 英文文件存在性候选产生不可重试的 error UNKNOWN/ungrounded，严格准入已不能通过，停止余下请求并保留回执；37 个成功请求记录 256860 tokens，账本为 4 CONFIRMED / 3 REFUTED / 17 UNKNOWN / 4 OPEN，进行中的请求可能有未记录用量。Job Object 返回 125，峰值约 358 MiB，评测锁已释放。本轮不打质量分，不以候选确认数量表示进步，未用 holdout。

## 2026-10-11 调查结论的契约前提

两问回查 `7db82230` / `c50ed3b7` 与 §4.6：最初的精确 quote 校验用于阻止伪造源码，但隐含“一条真实引用足以支持整个 verdict”的假设。连续真实回执证明，真实 throw 或 import 可以被引用，却不证明候选声称的义务。这不是再加一条异常提示就能保证的输入/输出问题；Issue/开放 PR 库仍为空。

在既有调查阶段增加结构化 `ContractAssessment`：先分别写 expected/actual 的事实和各自 Observation 引用，再写二者关系，最后选择 verdict。代码逐条核实指定 Observation 成功、quote 精确保存、关系与 verdict 一致；缺项或无效仍是 UNKNOWN，不转成 no_issue、不补重试。标准契约允许引用实际类型/配置的绑定，同一 Observation 能证明两前提时可复用；语义是否真的支持义务仍需调查员判断，不能宣称代码已证明语义。assessment 经现有 JSON 账本进入 editor/失败模板与续跑事实摘要；旧闭合 checkpoint 保持兼容，不重新调查。

另补交调查员遗漏的 PR intent（最多 2000 字符），明确仅作作者背景，不能免责；编辑修复须保留有效新行为，避免正确的 locale 评论建议恢复旧 HTML。调查和发布预算、主模型、goldens/裁判阈值、legacy 与 main 均不变。先新增回归复现 9 项旧行为失败，再实现；新增 13 项独立/参数化回归覆盖两前提引用、关系矛盾、UNKNOWN 不提升、真实 DB/ledger/editor/失败模板/续跑交付与旧 checkpoint 兼容。缩短提示正文而非扩大预算，单步读取后收尾回归继续通过。完整检查为 `1572 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判回归通过；新冻结实测仍待完成。

冻结 `0c59514` 的第七轮位于 `.reviewforge/benchmarks/v4-contract-assessment-20261011-034038/`。GEN 13 + lens 9 = 22 条候选，58 units 无漏答；立陶宛 account 的两前提分别引用相邻立陶宛原文与新增意大利语，已 CONFIRMED 并落库，契约交付有效。但 English 路径候选再次成为 error UNKNOWN/ungrounded，停止剩余请求：14 个成功请求记录 142901 tokens，账本为 1 CONFIRMED / 1 REFUTED / 11 UNKNOWN / 9 OPEN。Job Object 返回 125、峰值约 358 MiB，锁已释放；无完整评论、质量分或配对，可能有进行中请求的未记录用量。

## 2026-10-11 默认读取的证据窗口

`evidence-input-audit.json` 保存原回执 hash 和诊断，原 verdict 不改。English 路径调查三次读取整份 Java 文件，obs_0/2/3 的 1200 字符保存区主要是版权头/import；实际方法位于展示用 Additional context。顶层搜索引用还压缩了真实缩进，expected/actual 引用了未保存的方法正文。响应的 compatible 与 confirmed 也不一致，但先被旧引用门槛拦住。这证明新契约协议仍需相关原始证据输入，不能通过扩大保存长度或放松匹配解决。

两问回查工具描述“完整内容或窗口”与 `_run_tool` 的头部保存：原接口假设模型会自行改为窄读，真实回执反复违背该假设。现在省略窗口时确定性定位最近保存的正向搜索位置、compiler 关联 site 或本 unit 的 Context slice；显式范围和无位置文件保留原行为。记录实际请求的行窗口，源码仍由固定 head 读取、精确引用仍验证保存的原文，not_found/error/未保存命中不提供定位依据。另从已校验的两组 assessment 引用生成公共引用，移除新模型输出里重复的第三份字段；显式旧引用仍严格检查。

新增 13 项回归覆盖真实长版权头文件、三种正向搜索、相关/无关 Context、负向与未保存结果、显式/未知路径、最新命中切换，以及派生引用和无效旧引用拒收。相关 73 项通过，完整检查为 `1585 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判回归通过。新的独立冻结预检待完成，未运行 holdout。

冻结 `a20a113` 的新尝试位于 `.reviewforge/benchmarks/v4-focused-evidence-20261011-040016/`。GitHub codeload 返回 ConnectError，workspace 退化为 api-fallback（0 本地文件），ContextPack 为 0 slices / 58 truncated units；这一输入与先前完整 tarball 不可公平比较。停止该次尝试，保留原始记录，Job Object 返回 125，5 个成功请求记录 83896 tokens，可能有进行中请求未记录用量，无质量分；下载链路独立 HEAD 探针已恢复 200。另预先声明了 keycloak#36882 与 sentry#93824 两个额外 dev 样本，尚未发模型请求，未用 holdout。

## 2026-10-11 付费调用前的源码预检

回查 benchmark `_run_one` 与 `ToolGateway.workspace_for`：原评测沿用产品允许的 API 降级行为，付费调用前不区分完整/退化源码。这适合产品可用性，但不满足当前固定输入的质量对照。新增显式 `--require-complete-workspace`，仅用于评测：在任何 graph/model 调用之前取得固定 head 的 tarball，校验来源、截断标记和 SHA，并保存预检诊断；不满足则失败且没有模型调用。使用现有 run-scoped workspace cache，后续 v4 复用同一对象、不再次下载；拒收和 graph 异常时释放该 state 的 workspace，legacy 预检缓存也显式清理。默认关闭，产品降级路径保持原行为。开关写入续跑 provenance，避免混用输入协议。

新增 4 项真实 tarball/gateway 回归，验证完整源码复用一次下载、网络降级/截断均先于 graph 拒收、graph 失败清理和诊断保留；相关 11 项通过，完整检查为 `1589 passed, 1 skipped, 6 warnings`，ruff / format / spec-check 与严格裁判回归通过。下一次固定输入预检及后续配对显式开启此开关。
