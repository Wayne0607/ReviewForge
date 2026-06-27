# ReviewForge 架构设计

## 设计哲学

ReviewForge 采用 AI-Native 架构，所有能力通过声明式 Spec 注册，Agent 间通过共享状态间接通信，决策层保证可观测可恢复。

## 核心概念

### Spec Registry

所有 Agent、Tool、Skill 在 `SpecRegistry` 中声明。运行时不可使用未注册的能力。

```python
registry.register_agent(AgentSpec(
    name="security_reviewer",
    role="executor",
    allowed_tools=["read_diff", "read_file", "search_code"],
    model_profile="reviewer",
    max_steps=10,
))
```

新增审查维度只需：
1. 注册 AgentSpec
2. 写对应的 Skill 文件
3. 不改任何已有代码

### State Store (Lattice)

所有共享状态在内存 KV 存储中，Agent 读取时获得深拷贝，不能互相污染。

三个域：
- `findings`: 审查发现（candidate → confirmed / false_positive → reported）
- `tasks`: 任务记录（pending → claimed → completed / failed）
- `notes`: Agent 间反馈（一次性消息，消费后删除）

### Conductor-Operative 分离

- **Planner (Conductor)**: 单次 LLM 调用，读 diff summary，输出 task proposals
- **Reviewer (Operative)**: 无状态单任务 Agent，执行审查，输出 findings
- **Verifier (Auditor)**: 纯推理，去误报
- **Commenter (Analyst)**: 格式化输出

Agent 之间不直接通信，全部通过 State Store 协调。

### Tool Gateway

每次工具调用经过四层门控：
1. 权限检查：Agent 的 `allowed_tools` 是否包含该工具
2. Schema 校验：输入参数是否符合 JSON Schema
3. 策略检查：业务规则（如 Reviewer 不能直接发评论）
4. 执行：分发到对应的 handler

### Loop Detection

签名 = `{reviewer}:{file_hash}`，滑动窗口大小 3。

- **Stage 1 (rescue)**: 连续 3 次相同签名 → 排空重复任务，给 Planner 发 hint
- **Stage 2 (stall)**: rescue 后再次 3 次 → 停止，防止无限循环

### 渐进式知识加载

Skill 有三级披露：
1. **注册时**: 只加载 name + description（~50 tokens/skill）
2. **选中时**: 注入完整 SKILL.md 到 prompt
3. **执行时**: 按需读取 references/ 下的深层文件

## 数据流

```
GitHub PR Event
    ↓
Webhook Handler → 创建 StateStore，设置 PR 上下文
    ↓
Orchestrator.run(state)
    ↓
Phase 1: Planner.plan(state)
  - 读取 state.diff_summary
  - 输出 TaskProposals[]
  - 写入 state.tasks
    ↓
Phase 2: 遍历 pending tasks
  - Loop Detector 检查签名
  - Reviewer.execute(task, state)
  - 输出 Finding[] → 写入 state.findings
    ↓
Phase 3: Verifier.verify(state)
  - 读取 candidate findings
  - 输出 confirmed / false_positive
  - 更新 finding.status
    ↓
Phase 4: Commenter.post_comments(state)
  - 读取 confirmed findings
  - 调用 post_comment tool
  - 更新 finding.status → reported
```

## 扩展点

| 扩展 | 方式 | 改动范围 |
|---|---|---|
| 新增 Reviewer | 注册 AgentSpec + 写 Skill + 实现 Reviewer 类 | 3 个文件，零已有代码改动 |
| 新增 Tool | 注册 ToolSpec + 实现 handler | 2 个文件 |
| 新增 Skill | 创建 SKILL.md 目录 | 1 个目录 |
| 更换 LLM | 修改环境变量 | 0 个代码改动 |
