# ReviewForge 交接文档

> 最后更新：2026-06-29，由 Claude (session agent) 整理

---

## 一、项目概述

ReviewForge 是一个 AI 多 Agent 代码审查系统。监听 GitHub PR webhook，通过 Planner-Reviewer-Calibrator 三层架构自动审查代码，将确认的发现以 review comment 形式发回 GitHub。

**仓库**：`https://github.com/Wayne0607/ReviewForge.git`
**分支**：`main`（当前干净，无未提交改动）
**版本**：`0.2.0`

---

## 二、技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python 3.11+, FastAPI, LangChain, aiosqlite, Pydantic |
| LLM | 小米 MiMo TokenPlan（`mimo-v2.5-pro`），通过 OpenAI 兼容 API |
| 前端 | React + Vite + TypeScript + Tailwind + Recharts |
| 数据库 | SQLite（异步，WAL 模式） |
| 部署 | Docker / systemd + nginx（阿里云） |
| CI/CD | GitHub Actions（lint → test → deploy） |

---

## 三、架构管线

```
GitHub PR Webhook
       ↓
  Planner（单次 LLM，读 diff，输出 task proposals）
       ↓  + 确定性正则检测（安全模式、性能模式等）
  Scheduler（串行 loop 检测 + asyncio.gather 并行）
       ↓
  Reviewers（7 个维度，支持单次/agentic 双模式）
       ↓
  DynamicCalibrator（3 轮：安全类自动确认 → 对抗验证 → 条件裁决）
       ↓  （可选）CrossPRAnalyzer
  Commenter（格式化 → 发 GitHub review comment）
       ↓
  持久化（SQLite: runs/findings/metrics/tokens/code_graph）
```

---

## 四、目录结构

```
reviewforge/
├── backend/src/reviewforge/
│   ├── core/           # SpecRegistry, StateStore, EventBus, Database, Config, Auth, LoopDetector
│   ├── engine/         # Orchestrator, Planner, 7 Reviewers, DynamicCalibrator, ModelRouter, TokenTracker, Prompt
│   ├── tools/          # ToolGateway (4层门控), GitHubClient, MockGitHubClient
│   ├── skills/         # 3 个 SKILL.md (security_rules, python_best_practices, react_patterns)
│   ├── api/            # Webhook (签名验证), Dashboard (reviews/metrics/tokens/hotspots)
│   ├── plugins/        # 示例插件（默认关闭）
│   ├── utils/          # cache_utils.py（蜜罐，见"已知问题"）
│   └── static/         # 构建后的前端资源
├── backend/tests/      # 25 个测试（specs/state/loop_detector/agentic_loop）
├── backend/scripts/    # eval_agentic.py (A/B 评测), probe_tool_calling.py
├── examples/fixtures/  # 13 个安全评测 fixture + labels.json
├── frontend/           # React dashboard 源码
├── docs/               # architecture.md, agentic_eval.md
├── scripts/            # setup-server.sh, deploy.sh
├── deploy/             # nginx/ 和 systemd/（占位，由 setup-server.sh 生成）
└── .github/workflows/  # deploy.yml (lint → test → SSH deploy)
```

---

## 五、关键配置

### 5.1 环境变量（.env）

```bash
# GitHub
GITHUB_TOKEN=ghp_xxx              # GitHub PAT（读 PR + 发评论）
GITHUB_WEBHOOK_SECRET=xxx         # Webhook 签名验证密钥

# LLM
LLM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
LLM_API_KEY=xxx                   # MiMo TokenPlan API key
REVIEWFORGE_MODEL=MiMo

# API 鉴权
REVIEWFORGE_API_TOKEN=xxx         # Dashboard API Bearer token

# 可选
REVIEWFORGE_MOCK=1                # Mock 模式（本地测试）
REVIEWFORGE_AGENTIC_REVIEWERS=security_reviewer  # 开启 agentic 模式的 reviewer
REVIEWFORGE_MAX_CONCURRENT_REVIEWS=3
REVIEWFORGE_ENABLE_PLUGINS=0
REVIEWFORGE_CORS_ORIGINS=http://localhost:5173
```

### 5.2 reviewforge.yaml

```yaml
server:
  host: "127.0.0.1"
  port: 8000

llm:
  base_url: "https://token-plan-cn.xiaomimimo.com/v1"
  model: "mimo-v2.5-pro"
  profiles:
    fast:    { model: "mimo-v2.5-pro", temperature: 0.1, max_tokens: 4096 }
    accurate: { model: "mimo-v2.5-pro", temperature: 0.0, max_tokens: 8192 }
```

### 5.3 服务器部署

- **服务器**：阿里云 Ubuntu/Debian
- **部署路径**：`/opt/reviewforge`
- **服务**：systemd `reviewforge.service`，运行在 `127.0.0.1:8000`
- **反代**：nginx 对外
- **部署命令**：`bash scripts/deploy.sh`（CI 自动触发，也可手动）
- **GitHub Secrets**：`SERVER_HOST`, `SERVER_USER`, `SERVER_SSH_KEY`

---

## 六、CLI 命令

```bash
cd backend
python -m reviewforge serve              # 启动 API 服务
python -m reviewforge serve --dev        # 开发模式（热重载）
python -m reviewforge spec-check         # 校验 Spec 完整性
python -m pytest -q                      # 运行测试（25 个）
python scripts/eval_agentic.py --mock    # Mock 模式 A/B 评测
python scripts/eval_agentic.py --real    # 真实 LLM A/B 评测
```

---

## 七、当前状态（已完成）

### 7.1 核心管线 ✅

- **Orchestrator**：4 阶段完整（Plan → Review → Calibrate → Comment），错误处理、DB 持久化、事件发射
- **Planner**：混合式（确定性正则 + LLM），自动检测安全/性能/测试/依赖/可访问性模式
- **7 个 Reviewer**：security, performance, style, testing, documentation, dependency, accessibility
  - 支持单次模式和 agentic 模式（模型驱动的工具循环）
  - agentic 模式有 4 道安全刹车：步数上限、token 预算、输出截断、重复调用防护
- **DynamicCalibrator**：3 轮对抗校准，安全类自动确认
- **CrossPRAnalyzer**：跨 PR 安全分析（符号提取 + 代码图查询 + LLM 确认）

### 7.2 基础设施 ✅

- **SpecRegistry**：声明式注册 agent/tool/skill，带交叉引用校验
- **StateStore**：Pydantic 校验 + 深拷贝隔离
- **Database**：async SQLite，完整 schema（runs/findings/metrics/tokens/code_graph/file_risk）
- **EventBus**：JSONL 日志 + 订阅回调
- **ModelRouter**：多模型路由（fast/accurate/default profiles）
- **TokenTracker**：TrackedChatLLM 包装器，自动记录 token 使用

### 7.3 工具层 ✅

- **ToolGateway**：4 层门控（存在性 → 权限 → 策略 → 执行）
- **GitHubClient**：异步 httpx，支持分页、diff、content、search、comment
- 4 个工具：`read_diff`, `read_file`, `search_code`, `post_comment`

### 7.4 API + 前端 ✅

- **Webhook**：签名校验（fail-closed）、并发控制（Semaphore）
- **Dashboard API**：reviews、metrics、tokens、hotspots、recurring issues
- **前端**：React 5 页面（Dashboard/Reviews/ReviewDetail/Analytics/System）

### 7.5 最近改动（本次 session）

| 改动 | 文件 | 说明 |
|------|------|------|
| reviewer prompt 穷举 | `engine/prompt.py` | security 任务加了"逐条穷举"指令 + 检查清单；findings 格式加了穷举要求 |
| fixture 集 | `examples/fixtures/` | 12 个新 fixture（SQL注入/命令注入/代码注入/硬编码密钥/反序列化/路径遍历/XSS/弱加密/CSRF/混合漏洞/2个干净文件） |
| labels.json | `examples/fixtures/labels.json` | 13 个 fixture 的期望类别标签 |
| eval 脚本重写 | `backend/scripts/eval_agentic.py` | 加了类别归一化（CATEGORY_ALIASES 30+ 映射）、TokenCounter、per-category recall 分解、clean file FPR |
| agentic 测试 | `backend/tests/test_agentic_loop.py` | 9 个新测试：单次/agentic 基线、只读工具、gateway 调用、兜底、名称设置、完整性、JSON 解析 |

---

## 八、已知问题（需后续处理）

### 🔴 高优先级

| 问题 | 位置 | 说明 |
|------|------|------|
| **Skills 未接入 prompt** | `engine/prompt.py` | `SkillLoader` 实现了三级渐进加载，但 `build_reviewer_prompt` 从未调用。reviewer 拿不到 skill 内容。 |
| **Agentic token 计数为 0** | `engine/reviewers.py` | `bind_tools` 返回原生 LLM 对象，绕过 TrackedChatLLM 包装器。eval 中 agentic 模式 token 全为 0。 |
| **Reviewer prompt 可能仍不够穷举** | `engine/prompt.py` | 虽然加了穷举指令，但真实 LLM（MiMo）recall 仍低。需用真实 LLM 重测 eval 验证 prompt 改动效果。 |
| **uv.lock 缺失** | 项目根目录 | 部署时 `uv sync` 没有 lock 文件会拉到不确定版本。需要 `cd backend && uv lock` 并提交。 |

### 🟡 中优先级

| 问题 | 位置 | 说明 |
|------|------|------|
| **cache_utils.py 蜜罐** | `utils/cache_utils.py` | 包含 eval()、SQL 注入、pickle 反序列化 4 个严重漏洞。**未被任何代码引用**，但会被安全扫描器误报。建议删除或加注释。 |
| **Verifier spec 冗余** | `core/specs.py` | 旧 Verifier 被 DynamicCalibrator 替代，但 spec 仍注册、`build_verifier_prompt` 函数仍存在。 |
| **Plugin 系统空壳** | `plugins/example_reviewer.py` | 只设了 name/type，没有审查逻辑。默认关闭。 |
| **test-pr CLI 不存在** | `cli.py` | CLAUDE.md 写了 `uv run reviewforge test-pr`，但代码里没有。 |
| **docs/api.md 不存在** | `docs/` | CLAUDE.md 说改 API 要更新 docs/api.md，但文件不存在。 |
| **Dockerfile 构建路径** | `Dockerfile` | 可能需要检查前端构建路径是否与实际一致。 |

### 🟢 低优先级

| 问题 | 位置 | 说明 |
|------|------|------|
| Skills 引用的文件不存在 | `skills/security_rules/` | SKILL.md 引用了 `patterns.md`，但 `references/` 目录不存在。 |
| 无集成测试 | `tests/` | 25 个测试都是单元测试，无端到端管线测试。 |
| security_rules SKILL.md | `skills/security_rules/` | 引用了 `patterns.md` 但文件不存在。 |

---

## 九、评测数据

### 9.1 A/B 评测（真实 LLM，MiMo）

来源：`backend/scripts/eval_result.json`

| 指标 | Single-shot | Agentic |
|------|-------------|---------|
| Precision | 0.0 | 1.0 |
| Recall | 0.0 | 0.143 |
| F1 | 0 | 0.25 |
| TP/FP/FN | 0/1/7 | 1/0/6 |
| 延迟 | 49.73s | 39.66s |

**注意**：这个评测只用了 1 个 fixture，n=1 无统计意义。precision 差异是标签字符串假象（两种模式都只找到了同一个 pickle 问题，只是命名不同）。

### 9.2 评测脚本改进（本次 session）

- fixture 从 1 个扩展到 13 个
- 加了类别归一化（`unsafe-import` → `insecure-deserialization`）
- 加了 token 成本测量
- 加了 clean file 误报率（FPR）
- 加了 per-category recall 分解

**下一步**：用真实 LLM 重跑 `python scripts/eval_agentic.py --real`，获取有意义的多文件评测数据。

---

## 十、代码规范

- **Commit**：Conventional commits，单行（如 `feat(reviewer): add security reviewer`）
- **Commit 粒度**：每改完一个文件 commit 一次
- **Branch**：`feat/xxx`, `fix/xxx`, `refactor/xxx`
- **Lint**：`ruff check . && ruff format .`
- **Test**：`python -m pytest -q`（当前 25 个，全通过）

---

## 十一、关键设计决策备忘

| 决策 | 原因 |
|------|------|
| Planner 单次 LLM（非 agentic loop） | 减少延迟，确定性正则兜底 |
| StateStore 深拷贝隔离 | 并发 reviewer 不互相污染 |
| 安全类 finding 跳过 calibrator | 安全是客观事实，对抗验证会误杀 |
| Agentic 默认关闭 | MiMo recall 太低，等更强模型再开 |
| ToolGateway 4 层门控 | 防越权（reviewer 不能发评论）、防注入 |
| DynamicCalibrator 替代旧 Verifier | 3 轮对抗比单次验证更可靠 |
