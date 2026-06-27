# ReviewForge

AI 多 Agent 代码审查系统。监听 GitHub PR，通过 Planner-Reviewer-Verifier 三层架构自动审查代码。

## 架构

```
GitHub PR Webhook
       ↓
  Planner (LLM 决策) → 分析 diff，决定派哪些 Reviewer
       ↓
  Scheduler (优先级队列) → 并发调度
       ↓
  Reviewers (无状态 Agent)
  ├─ SecurityReviewer   → 安全漏洞检测
  ├─ PerformanceReviewer → 性能问题检测
  └─ StyleReviewer      → 代码风格检查
       ↓
  Verifier (LLM 验证) → 去除误报
       ↓
  Commenter → 格式化输出，发 GitHub review comment
```

## 核心设计

- **Spec-Driven**: 所有 Agent 和 Tool 通过声明式 Spec 注册，新增审查维度零代码改动
- **Conductor Single-Shot**: Planner 每轮一次 LLM 调用，保证可观测、可恢复、成本可控
- **State Store (Lattice)**: 共享状态中心化存储，Agent 间深拷贝隔离
- **Tool Gateway**: 四层安全门控（权限 → Schema → 策略 → 执行）
- **Loop Detection**: 两阶段救援（rescue → stall），防无限循环
- **渐进式知识加载**: Skill 元数据注册时加载，完整内容按需注入

## 快速开始

### 1. 安装

```bash
cd backend
pip install uv  # 或 curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入 GitHub Token 和 LLM API Key
```

### 3. 运行

```bash
uv run reviewforge serve          # 启动 API 服务
uv run reviewforge spec-check     # 校验 Spec 完整性
uv run pytest -q                  # 运行测试
```

### 4. 配置 GitHub Webhook

1. 进入 GitHub 仓库 → Settings → Webhooks → Add webhook
2. Payload URL: `http://YOUR_SERVER/webhook/github`
3. Content type: `application/json`
4. Secret: 与 `.env` 中 `GITHUB_WEBHOOK_SECRET` 一致
5. Events: 选择 `Pull requests`

## 技术栈

- **后端**: Python 3.11+ / FastAPI / LangChain
- **LLM**: 小米 MiMo TokenPlan (OpenAI 兼容 API)
- **GitHub API**: httpx + REST API v3
- **部署**: Nginx + systemd + GitHub Actions CI/CD

## 项目结构

```
reviewforge/
├── backend/src/reviewforge/
│   ├── core/         # Spec Registry, State Store, Events, Loop Detection
│   ├── engine/       # Orchestrator, Planner, Reviewers, Verifier, Prompt
│   ├── tools/        # Tool Gateway, GitHub API Client
│   ├── skills/       # 审查规则集（渐进式加载）
│   └── api/          # FastAPI Webhook + Routes
├── docs/             # 架构文档
├── scripts/          # 部署脚本
└── .github/workflows/ # CI/CD
```

## License

MIT
