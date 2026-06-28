# ReviewForge

AI 多 Agent 代码审查系统。监听 GitHub PR，通过 Planner-Reviewer-Verifier 三层架构自动审查代码。

## 架构

```
GitHub PR Webhook
       ↓
  Planner (LLM 决策) → 分析 diff，决定派哪些 Reviewer
       ↓
  Reviewers (无状态 Agent，并行执行)
  ├─ SecurityReviewer   → 安全漏洞检测
  ├─ PerformanceReviewer → 性能问题检测
  └─ StyleReviewer      → 代码风格检查
       ↓
  Verifier (LLM 验证) → 去除误报
       ↓
  Commenter → 格式化评论，发到 GitHub PR
```

## 核心设计

- **Spec-Driven**: 所有 Agent 和 Tool 通过声明式 Spec 注册，新增审查维度零代码改动
- **Conductor Single-Shot**: Planner 每轮一次 LLM 调用，保证可观测、可恢复、成本可控
- **State Store**: 共享状态中心化存储，Pydantic schema 校验，Agent 间深拷贝隔离
- **Tool Gateway**: 工具调用经过权限检查和策略验证
- **Loop Detection**: 两阶段救援（rescue → stall），防无限循环
- **渐进式 Skill 加载**: 审查规则集按需加载，最小化 token 消耗
- **Mock/Live 双模式**: 开发测试用 mock 模式，不依赖真实 LLM 和 GitHub API
- **事件日志**: 全流程 JSONL 日志，每次状态变更可审计

## 快速开始

### 方式一：Docker（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/Wayne0607/ReviewForge.git
cd ReviewForge

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 GitHub Token 和 LLM API Key

# 3. 启动
docker-compose up -d

# Mock 模式测试（不需要真实 LLM）
docker-compose --profile mock up reviewforge-mock
```

### 方式二：本地安装

```bash
# 1. 安装依赖
cd backend
pip install -e .

# 2. 配置
cp ../.env.example ../.env
# 编辑 .env

# 3. 校验配置
python -m reviewforge spec-check

# 4. 启动
python -m reviewforge serve

# Mock 模式
python -m reviewforge serve --mock
```

## 配置

### 环境变量（.env）

```bash
# GitHub
GITHUB_TOKEN=ghp_xxxxxxxxxxxx          # Personal Access Token
GITHUB_WEBHOOK_SECRET=your-secret      # Webhook 密钥

# LLM
LLM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
LLM_API_KEY=your-api-key
REVIEWFORGE_MODEL=mimo-v2.5-pro

# 可选
REVIEWFORGE_HOST=127.0.0.1
REVIEWFORGE_PORT=8000
```

### 配置文件（reviewforge.yaml）

```yaml
llm:
  model: "mimo-v2.5-pro"
  temperature_planner: 0.0
  temperature_reviewer: 0.1

reviewers:
  - name: security_reviewer
    type: security
    enabled: true
    max_steps: 10
    confidence_threshold: 0.5

  - name: performance_reviewer
    type: performance
    enabled: true

  - name: style_reviewer
    type: style
    enabled: true

confidence_threshold: 0.5    # 低于此值的发现不发评论
skills_dir: "skills"
```

环境变量优先级高于配置文件。

## 配置 GitHub Webhook

1. 进入仓库 → Settings → Webhooks → Add webhook
2. Payload URL: `http://你的服务器:8000/webhook/github`
3. Content type: `application/json`
4. Secret: 与 `.env` 中 `GITHUB_WEBHOOK_SECRET` 一致
5. Events: 勾选 `Pull requests`

## 自定义审查规则

### 添加新 Skill

在 `skills/` 目录下创建子目录：

```
skills/
  my_custom_rule/
    SKILL.md              # 主文件（自动注入 prompt）
    references/           # 深层参考（按需读取）
      examples.md
      patterns.md
```

SKILL.md 格式：

```markdown
---
name: my_custom_rule
description: 自定义审查规则描述
category: security
reviewer_type: security
references:
  - examples.md
---

# 审查规则

## 任务

审查代码中的 XXX 问题：
- 检查点 1
- 检查点 2

## 判断标准

**真实问题**: 具体描述什么算真实问题
**误报**: 具体描述什么算误报
```

### 添加新 Reviewer

1. 在 `skills/` 下创建对应 Skill 目录
2. 在 `reviewforge.yaml` 的 `reviewers` 中添加配置
3. 在 `backend/src/reviewforge/core/specs.py` 中注册 AgentSpec
4. 在 `backend/src/reviewforge/engine/reviewers.py` 中实现 Reviewer 类

## CLI 命令

```bash
# 启动服务
python -m reviewforge serve                    # 标准模式
python -m reviewforge serve --mock             # Mock 模式
python -m reviewforge serve --dev              # 开发模式（热重载）
python -m reviewforge serve --host 0.0.0.0     # 监听所有地址

# 校验配置
python -m reviewforge spec-check               # 检查 Spec + 配置 + Skills

# 指定配置文件
python -m reviewforge --config my-config.yaml serve
```

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/v1/specs` | GET | 查看已注册的 Agent/Tool/Skill |
| `/api/v1/config` | GET | 查看当前配置 |
| `/webhook/github` | POST | GitHub Webhook 接收 |

## 项目结构

```
reviewforge/
├── backend/
│   ├── src/reviewforge/
│   │   ├── core/                    # 核心基础设施
│   │   │   ├── specs.py             # Spec Registry（能力注册表）
│   │   │   ├── state.py             # State Store（schema 校验 + 深拷贝隔离）
│   │   │   ├── events.py            # EventBus（JSONL 日志 + 事件订阅）
│   │   │   ├── config.py            # 配置系统（YAML + 环境变量）
│   │   │   └── loop_detector.py     # 循环检测（两阶段救援）
│   │   │
│   │   ├── engine/                  # Agent 引擎
│   │   │   ├── orchestrator.py      # 主循环编排器
│   │   │   ├── planner.py           # Planner Agent（单次 LLM 决策）
│   │   │   ├── reviewers.py         # Reviewer Agents（安全/性能/风格）
│   │   │   ├── verifier.py          # Verifier Agent（去误报）
│   │   │   ├── prompt.py            # Prompt 构建器（section 模式）
│   │   │   └── mock_llm.py          # Mock LLM（测试用）
│   │   │
│   │   ├── tools/                   # 工具层
│   │   │   ├── gateway.py           # Tool Gateway（权限门控）
│   │   │   ├── github_api.py        # GitHub API 客户端
│   │   │   └── mock_github.py       # Mock GitHub（测试用）
│   │   │
│   │   ├── skills/                  # Skill 系统
│   │   │   ├── loader.py            # 渐进式加载器
│   │   │   ├── security_rules/      # 安全审查规则
│   │   │   ├── python_best_practices/
│   │   │   └── react_patterns/
│   │   │
│   │   ├── api/
│   │   │   └── webhook.py           # GitHub Webhook 处理
│   │   ├── app.py                   # 应用工厂
│   │   └── cli.py                   # CLI 入口
│   │
│   ├── tests/                       # 单元测试
│   └── pyproject.toml
│
├── skills/                          # 审查规则集（可自定义）
├── docs/                            # 架构文档
├── scripts/                         # 部署脚本
├── Dockerfile
├── docker-compose.yml
├── reviewforge.yaml                 # 配置文件
├── .env.example                     # 环境变量模板
├── CLAUDE.md                        # Claude Code 项目规范
└── AGENTS.md                        # Agent 开发指南
```

## 技术栈

- **后端**: Python 3.11+ / FastAPI / LangChain / Pydantic
- **LLM**: 小米 MiMo TokenPlan（OpenAI 兼容 API）
- **GitHub API**: httpx + REST API v3 + Webhook
- **部署**: Docker / Nginx + systemd / GitHub Actions CI/CD

## License

MIT
