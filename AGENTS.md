# ReviewForge Agent 开发指南

## 当前开发状态

- v4 在 `dev` 开发；`main` 保留现有生产版本与自动部署。未经验收不向 `main` 推送 v4。
- 用户已授权统一使用 `dev`，替代旧任务卡的 `hp/<卡号>` 分支约定。
- 新流水线事实源：[hypothesis-pipeline-spec.md](docs/hypothesis-pipeline-spec.md)。进度与待验收项：[v4-development.md](docs/v4-development.md)。
- 默认仍是 `legacy`；`shadow` 与 `hypothesis` 是尚待真实基准验收的 v4 路径。下述 Planner/Reviewer/Verifier/Commenter 描述适用于 legacy。
- 新 v4 运行使用内部协议 9：token 只计量；结果的整份传输记录有界，不再以 600 字符风格门槛拒绝裁决。取证步骤与最多两次持久化结果交付分开，最后一个取证步骤仍能读源码；接受的原生结论及证据摘要冻结后只能重验，不能重猜。路径读取优先实际使用处，完整窗口保存为多个原有大小的 Observation；导航不等于证据或已解析的调用链。旧协议 1–8 保留原语义与冻结计数；发布仍经原独立核验，工作循环跑通不等于质量验收。保留有限请求/步骤、超时、重试与机器资源保护。用户明确批准的付费评测额度是独立外层边界，用原生 `external_request_budget_scope` / `authorized_budget` 或 runner 的 `--token-budget` 保持，不因产品取消 token 上限而自动扩额。历史冻结脚本与原始实录不修改。
- 当前付费评测资金事实源是 `.reviewforge/eval-private/current-v4-grant-lineage-root.txt` 指向的完整子账本：当前 500000 grant 实报 367064、历史未知用量保守预占 129989、无在途，保守剩余 2947；旧独立余额 12797 / 35820 / 2662 / 3673 / 2721 未合并。不得从旧父目录重新复制 62455/19066 剩余额度。`34336aa` 的三案定向回归交付完成，独立来源还原后确认项因缺行为契约被 ProofGate 预检判为 UNKNOWN，零评论；完整空状态 PR/holdout 仍未验收。

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
