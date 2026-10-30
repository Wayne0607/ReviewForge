# ReviewForge Agent 开发指南

## 当前开发状态

- v4 在 `dev` 开发；`main` 保留现有生产版本与自动部署。未经验收不向 `main` 推送 v4。
- 用户已授权统一使用 `dev`，替代旧任务卡的 `hp/<卡号>` 分支约定。
- 新流水线事实源：[hypothesis-pipeline-spec.md](docs/hypothesis-pipeline-spec.md)。进度与待验收项：[v4-development.md](docs/v4-development.md)。
- 默认仍是 `legacy`；`shadow` 与 `hypothesis` 是尚待真实基准验收的 v4 路径。下述 Planner/Reviewer/Verifier/Commenter 描述适用于 legacy。
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
