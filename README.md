# ReviewForge

AI 多 Agent 代码审查系统。监听 GitHub PR，通过 Planner-Reviewer-Verifier 三层架构自动审查代码，配合可视化 Dashboard 展示审查趋势和分析。

## 架构

```
GitHub PR Webhook
       ↓
  Planner (LLM 决策 + 确定性模式检测)
       ↓
  Reviewers (无状态 Agent，并行执行)
  ├─ SecurityReviewer      → 安全漏洞检测
  ├─ PerformanceReviewer   → 性能问题检测
  ├─ StyleReviewer         → 代码风格检查
  ├─ TestingReviewer       → 测试覆盖检查
  ├─ DocumentationReviewer → 文档完整性检查
  ├─ DependencyReviewer    → 依赖风险检查
  ├─ AccessibilityReviewer → 可访问性检查
  └─ [Plugin Reviewers]    → 自定义插件审查
       ↓
  Dynamic Calibrator (对抗性校准 + 条件裁决)
       ↓
  Commenter → 格式化评论，发到 GitHub PR
       ↓
  SQLite 持久化 → Dashboard API → React 前端
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
- **多模型路由**: 不同 Agent 使用不同模型配置（Planner 用快模型，Security 用准确模型）
- **插件系统**: 自定义 Reviewer 放入 `plugins/` 目录，自动发现加载
- **跨 PR 分析**: SQLite 持久化 + Dashboard API，支持趋势分析和热点检测

## 快速开始

### 方式一：Docker（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/Wayne0607/ReviewForge.git
cd ReviewForge

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 GitHub Token 和 LLM API Key

# 3. 启动（包含前端构建）
docker-compose up -d

# Mock 模式测试（不需要真实 LLM）
docker-compose --profile mock up reviewforge-mock
```

### 方式二：本地安装

```bash
# 1. 安装后端依赖
cd backend
pip install -e .

# 2. 安装前端依赖并构建
cd ../frontend
npm install
npm run build       # 构建到 backend/src/reviewforge/static/

# 3. 配置
cp ../.env.example ../.env
# 编辑 .env

# 4. 校验配置
cd ../backend
python -m reviewforge spec-check

# 5. 启动
python -m reviewforge serve

# Mock 模式
python -m reviewforge serve --mock
```

### 前端开发模式

```bash
cd frontend
npm run dev         # 启动 Vite 开发服务器 (localhost:5173)
# 自动代理 /api 请求到后端 (localhost:8000)
```

## Dashboard 功能

访问 `http://localhost:8000` 查看可视化面板：

- **总览**: 审查总数、发现数、确认率、分类分布图、趋势折线图
- **审查记录**: 所有 PR 审查历史，支持搜索和筛选
- **审查详情**: 单次审查的 findings 列表，按严重度/状态筛选
- **趋势分析**: 热点文件排行、Reviewer 效率对比、反复出现的问题
- **系统信息**: 注册的 Agents/Tools/Skills、当前配置

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
  temperature_verifier: 0.0

  # 多模型路由 profiles
  profiles:
    fast:
      model: "mimo-v2.5-pro"
      temperature: 0.1       # 快速 Agent（Planner, Style, Testing）
    accurate:
      model: "mimo-v2.5-pro"
      temperature: 0.0       # 准确 Agent（Security, Verifier）

reviewers:
  - name: security_reviewer
    type: security
    max_steps: 10
  - name: performance_reviewer
    type: performance
    max_steps: 8
  - name: style_reviewer
    type: style
    max_steps: 6
  - name: testing_reviewer
    type: testing
    max_steps: 6
  - name: doc_reviewer
    type: documentation
    max_steps: 5
  - name: dependency_reviewer
    type: dependency
    max_steps: 6
  - name: accessibility_reviewer
    type: accessibility
    max_steps: 6

confidence_threshold: 0.5
```

环境变量优先级高于配置文件。

## 插件系统

在 `backend/src/reviewforge/plugins/` 目录下创建 `.py` 文件即可添加自定义 Reviewer：

```python
# plugins/my_reviewer.py
from reviewforge.engine.reviewers import BaseReviewer

class MyReviewer(BaseReviewer):
    plugin_name = "my_custom_reviewer"
    plugin_type = "custom"

    def __init__(self, llm, registry, gateway):
        super().__init__(
            name=self.plugin_name,
            reviewer_type=self.plugin_type,
            llm=llm, registry=registry, gateway=gateway,
            max_steps=6,
        )
```

启动时自动发现并加载。详见 `plugins/example_reviewer.py`。

## 配置 GitHub Webhook

1. 进入仓库 → Settings → Webhooks → Add webhook
2. Payload URL: `http://你的服务器:8000/webhook/github`
3. Content type: `application/json`
4. Secret: 与 `.env` 中 `GITHUB_WEBHOOK_SECRET` 一致
5. Events: 勾选 `Pull requests`

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/v1/specs` | GET | 查看已注册的 Agent/Tool/Skill |
| `/api/v1/config` | GET | 查看当前配置 |
| `/webhook/github` | POST | GitHub Webhook 接收 |
| `/api/v1/dashboard/reviews` | GET | 审查历史列表 |
| `/api/v1/dashboard/reviews/{id}` | GET | 单次审查详情 |
| `/api/v1/dashboard/metrics/summary` | GET | 总体统计 |
| `/api/v1/dashboard/metrics/categories` | GET | 分类分布 |
| `/api/v1/dashboard/metrics/trends` | GET | 周趋势 |
| `/api/v1/dashboard/metrics/hotspots` | GET | 热点文件 |
| `/api/v1/dashboard/metrics/reviewers` | GET | Reviewer 统计 |
| `/api/v1/dashboard/metrics/recurring` | GET | 反复出现的问题 |

## 项目结构

```
reviewforge/
├── backend/
│   ├── src/reviewforge/
│   │   ├── core/                    # 核心基础设施
│   │   │   ├── specs.py             # Spec Registry（能力注册表）
│   │   │   ├── state.py             # State Store（schema 校验 + 深拷贝隔离）
│   │   │   ├── events.py            # EventBus（JSONL 日志 + 事件订阅）
│   │   │   ├── config.py            # 配置系统（YAML + 环境变量 + 多模型 profiles）
│   │   │   ├── database.py          # SQLite 持久化（审查历史 + 指标）
│   │   │   └── loop_detector.py     # 循环检测（两阶段救援）
│   │   │
│   │   ├── engine/                  # Agent 引擎
│   │   │   ├── orchestrator.py      # 主循环编排器（含 DB 持久化）
│   │   │   ├── planner.py           # Planner Agent（LLM + 确定性模式检测）
│   │   │   ├── reviewers.py         # 7 个 Reviewer Agents
│   │   │   ├── calibrator.py        # Dynamic Calibrator（对抗性校准）
│   │   │   ├── model_router.py      # 多模型路由
│   │   │   ├── plugin_loader.py     # 插件发现和加载
│   │   │   ├── prompt.py            # Prompt 构建器（section 模式）
│   │   │   └── mock_llm.py          # Mock LLM（测试用）
│   │   │
│   │   ├── plugins/                 # 自定义 Reviewer 插件目录
│   │   │   └── example_reviewer.py  # 示例插件
│   │   │
│   │   ├── tools/                   # 工具层
│   │   │   ├── gateway.py           # Tool Gateway（权限门控）
│   │   │   ├── github_api.py        # GitHub API 客户端
│   │   │   └── mock_github.py       # Mock GitHub（测试用）
│   │   │
│   │   ├── skills/                  # Skill 系统
│   │   │   ├── loader.py            # 渐进式加载器
│   │   │   ├── security_rules/
│   │   │   ├── python_best_practices/
│   │   │   └── react_patterns/
│   │   │
│   │   ├── api/
│   │   │   ├── webhook.py           # GitHub Webhook 处理
│   │   │   └── dashboard.py         # Dashboard API（审查历史 + 趋势分析）
│   │   │
│   │   ├── static/                  # 前端构建产物（npm run build 生成）
│   │   ├── app.py                   # 应用工厂
│   │   └── cli.py                   # CLI 入口
│   │
│   ├── tests/                       # 单元测试
│   └── pyproject.toml
│
├── frontend/                        # React 前端
│   ├── src/
│   │   ├── api/client.ts            # API 客户端
│   │   ├── components/              # 可复用组件
│   │   ├── pages/                   # 页面（Dashboard, Reviews, Analytics, System）
│   │   └── types/                   # TypeScript 类型
│   ├── package.json
│   └── vite.config.ts
│
├── docs/                            # 架构文档
├── scripts/                         # 部署脚本
├── Dockerfile                       # 多阶段构建（Node + Python）
├── docker-compose.yml
├── reviewforge.yaml                 # 配置文件
└── .env.example                     # 环境变量模板
```

## 技术栈

- **后端**: Python 3.11+ / FastAPI / LangChain / Pydantic / aiosqlite
- **前端**: React 18 / TypeScript / Vite / TailwindCSS / Recharts
- **LLM**: 小米 MiMo TokenPlan（OpenAI 兼容 API）
- **GitHub API**: httpx + REST API v3 + Webhook
- **部署**: Docker 多阶段构建 / Nginx + systemd / GitHub Actions CI/CD

## License

MIT
