# ReviewForge

AI 多 Agent 代码审查系统。监听 GitHub PR，通过 Planner-Reviewer-Verifier 三层架构自动审查代码。

## Quick Start

```bash
cd backend
uv run reviewforge serve                              # 启动 API 服务
uv run reviewforge test-pr --repo owner/repo --pr 1   # 本地测试审查
uv run pytest -q                                       # 运行测试
```

## 项目结构

```
reviewforge/
├── backend/src/reviewforge/
│   ├── core/         # Spec Registry, State Store, Scheduler, Events
│   ├── engine/       # Orchestrator, Planner, Reviewers, Verifier, Prompt
│   ├── tools/        # Tool Gateway, GitHub API, Code Parser
│   ├── skills/       # 审查规则集（按语言/框架组织）
│   └── api/          # FastAPI Webhook + Routes
├── docs/             # 架构文档
├── scripts/          # 部署脚本
├── deploy/           # nginx + systemd 配置
└── .github/workflows/ # CI/CD
```

## 核心规则

1. **Spec 先行** — 新增 Reviewer/Tool 先在 SpecRegistry 注册，再写实现
2. **无静默 fallback** — 缺少依赖直接报错，不降级
3. **Prompt 动态生成** — 不硬编码 reviewer/tool 名，从 spec 读取
4. **文档同步** — 改架构 → docs/architecture.md；改 API → docs/api.md

## 多语言支持

### Skill 路由

每个 Skill 通过 frontmatter 声明 `languages` 和 `frameworks` 字段：

```yaml
# 语言特定（无框架要求）
languages: [go]

# 语言 + 框架
languages: [typescript, javascript]
frameworks: [vue, nuxt]

# 通用（空列表）
languages: []
```

路由优先级（`Orchestrator._resolve_skill`）：
1. `(language, framework)` — 精确匹配
2. `(language)` — 语言匹配且无框架限制
3. `(framework)` — 框架匹配（语言放宽）
4. `()` — 通用 skill（无语言/框架约束）

### 审查 Mission 语言感知

`_reviewer_mission` 根据 `target_language` 返回语言特定的审查目标：
- **Go**: error handling, goroutine lifecycle, interface design
- **Rust**: ownership, unsafe, error propagation, trait design
- **Java**: try-with-resources, Optional, Stream API
- **Python**: type hints, exception precision, function complexity
- **Ruby**: metaprogramming, block usage, enumerables

### Planner 语言感知模式

`_detect_patterns` 按语言分组安全/依赖模式，只对匹配语言的文件应用对应规则。支持 Python/Go/Java/Rust/Ruby/JavaScript/TypeScript，另有通用模式覆盖硬编码密钥、SQL 注入等。

## 架构设计

### 执行管线

```
GitHub PR Webhook
       ↓
  Planner (单次 LLM 决策，读 diff，输出 task proposals)
       ↓
  Scheduler (优先级队列，并发调度)
       ↓
  Reviewers (无状态，每个审查一个维度)
       ↓
  Verifier (去误报，合并重复)
       ↓
  Commenter (格式化，发 GitHub review)
```

### 关键设计决策

- **Conductor Single-Shot**：Planner 每轮一次 LLM 调用，不是 agentic loop
- **State Store (Lattice 模式)**：所有共享状态在内存 KV 存储，agent 间深拷贝隔离
- **Spec-driven**：新增审查维度只需注册 AgentSpec + 写 Skill，零代码改动
- **渐进式知识加载**：Skill 元数据注册时加载，完整内容按需注入
- **Loop 检测**：两阶段救援（rescue → stall），防无限循环

## CLI 命令

```bash
cd backend
uv run reviewforge serve                              # API 服务（生产）
uv run reviewforge serve --dev                        # 开发模式（热重载）
uv run reviewforge spec-check                         # 校验 Spec 完整性
uv run pytest -q                                      # 测试
uv run ruff check . && uv run ruff format .           # Lint + Format
```

## 开发规范

- **Commit**: Conventional commits，单行（如 `feat(reviewer): add security reviewer`）
- **Commit 粒度**: 每改完一个文件 commit 一次
- **Branch**: `feat/xxx`, `fix/xxx`, `refactor/xxx`
- **汇报**: 改动后说明 → 补哪层验证 → 入口 → 如何验证

## LLM 配置

- Provider: 小米 MiMo TokenPlan
- Base URL: `https://token-plan-cn.xiaomimimo.com/v1`
- Model: 通过环境变量 `REVIEWFORGE_MODEL` 配置

## 部署

```bash
# 服务器初始化
bash scripts/setup-server.sh

# 部署
bash scripts/deploy.sh
```

服务运行在 `127.0.0.1:8000`，Nginx 反代对外。
