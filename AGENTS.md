# ReviewForge Agent 开发指南

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
