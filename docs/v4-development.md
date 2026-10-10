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

还需补齐的运行协议缺口：当前 frozen outbox 会在恢复时跳过所有 LLM 阶段；若第一次已发布部分结果，但仍有 OPEN/可重试 UNKNOWN，就无法继续调查并发布新增确认问题。需实现不修改已发送负载、仅补充未发布假设的恢复协议，并验证丢回执与重复恢复。此项未完成，v4 不可进入生产。

`311552c` 的 Linux dev CI 为 `1476 passed, 3 skipped, 6 warnings`（Windows Job Object 内核探针在 Linux 跳过）。本机三个真实上下文采集已串行结束：Sentry 获得 15464 文件、22 units 的 tarball 快照；keycloak 与 grafana 因 Windows MAX_PATH 限制退化为 API fallback，代码 slices 为零，因此不能计为通过抽查。工作进程组峰值分别约 178 / 165 / 413 MiB，均在 2 GiB 内核总内存限制内，未在生产主机启动真实评测。

回溯 workspace 的原实现：正常临时目录路径适用于 Linux，却不能覆盖 Windows 上大仓库的合法长路径。新增微型 tarball 回归，先复现“一个无关长路径文件使整个仓库降级”，再仅对 Windows 物理快照根使用扩展绝对路径；逻辑仓库路径、SHA、归档安全校验与降级契约保持原规格。提取、读取、搜索、manifest 和清理共用同一根路径，不依赖修改操作系统注册表。上下文审计显式记录 snapshot 来源、文件数与 slices，并可用 `--require-tarball` 拒绝把降级诊断算成成功；仍保留原始诊断文件。

本轮 Windows 全量测试 `1480 passed, 1 skipped, 6 warnings`；长路径、安全归档、上下文与 bootstrap 的 29 项回归通过，ruff / format 通过。接下来固定本轮提交，重新进行串行、受限的真实 ContextPack 抽查。尚无有效 v3/v4 配对 F1。

1. 大型 PR 分块和截断覆盖的真实边界、调查输入与 unit hunk 的一致性。
2. 复核 detector 种子的确认语义与未映射类别，避免未经验证的命中直接成为强证据问题。
3. 完成开发集漏斗诊断、配对指标和 ContextPack 实例抽查，达标后再进入 holdout。

然后按 SPEC 运行：相同模型、英文输出，dev10 上 legacy 对照与 shadow 调查漏斗；抽查 keycloak#36880、grafana#97529、sentry#80168 的实际 ContextPack。达标后才进行 holdout40 两轮配对验收；holdout 不用于调参。记录账本召回、调查误杀、发布遗漏、重复误报与 token 消耗。通过质量与运行可靠性门槛后，才把 v4 推入 `main` 并切换生产默认。
