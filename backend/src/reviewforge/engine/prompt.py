"""Prompt Builder — section-based composable prompt generation.

Each section is a callable that returns str | None.
None means "skip this section". Prompts auto-generate from specs.
"""

from __future__ import annotations

from typing import Any, Callable

from reviewforge.core.specs import SpecRegistry

PromptSection = Callable[[dict[str, Any]], str | None]


def _identity(ctx: dict[str, Any]) -> str:
    role = ctx.get("role", "reviewer")
    identities = {
        "planner": "你是 ReviewForge 的 Planner。你分析 PR diff，决定派哪些专门的 Reviewer 去审查。",
        "reviewer": f"你是 ReviewForge 的 {ctx.get('reviewer_type', '代码')}审查员。你检查代码变更并报告发现的问题。",
        "verifier": "你是 ReviewForge 的 Verifier。你审查候选发现，判断是真实问题还是误报。",
        "commenter": "你是 ReviewForge 的 Commenter。你将确认的发现格式化为清晰、可操作的 GitHub review 评论。",
    }
    return identities.get(role, identities["reviewer"])


def _language(ctx: dict[str, Any]) -> str:
    return "## 语言要求\n\n所有 message、suggestion、reason 字段必须使用中文。category 和 severity 使用英文。代码标识符、路径、API 名称保留英文。"


def _available_tools(ctx: dict[str, Any]) -> str | None:
    registry: SpecRegistry = ctx["registry"]
    agent_name = ctx.get("agent_name", "")
    if not agent_name or agent_name not in registry.agents:
        return None
    agent = registry.agents[agent_name]
    if not agent.allowed_tools:
        return None
    lines = ["## Available Tools\n"]
    for tool_name in agent.allowed_tools:
        tool = registry.tools.get(tool_name)
        if tool:
            lines.append(f"- **{tool_name}**: {tool.description}")
    return "\n".join(lines)


def _output_contract(ctx: dict[str, Any]) -> str | None:
    registry: SpecRegistry = ctx["registry"]
    agent_name = ctx.get("agent_name", "")
    if not agent_name or agent_name not in registry.agents:
        return None
    contract = registry.agents[agent_name].output_contract
    if not contract:
        return None
    return f"## Output Contract\n\nYou MUST respond with valid JSON matching this schema:\n```json\n{contract}\n```"


def _planner_mission(ctx: dict[str, Any]) -> str:
    return """## 任务

分析 PR diff，决定派哪些 Reviewer 去审查。

规则：
- 只派需要的 Reviewer，不要浪费
- **Security Reviewer（必须派发，如果代码涉及以下任何一项）**：
  - os.system / subprocess / eval / exec（命令注入）
  - SQL 查询 / 字符串拼接 SQL（SQL 注入）
  - pickle.loads / yaml.load（反序列化）
  - 硬编码密码、密钥、token
  - open() 用用户输入的路径（路径遍历）
  - 用户输入未经验证就使用
  - 网络请求、加密操作
- Performance Reviewer：涉及循环、数据处理、缓存、数据库查询的文件
- Style Reviewer：始终派发，检查可读性
- 每个 task 要列出具体文件
- 每轮最多 4 个 task"""


def _reviewer_mission(ctx: dict[str, Any]) -> str:
    reviewer_type = ctx.get("reviewer_type", "general")
    missions = {
        "security": """## 任务

审查代码中的安全漏洞：
- SQL 注入、XSS、CSRF、路径遍历
- 硬编码密钥、不安全的默认配置
- 缺少输入验证/清理
- 不安全的加密、弱认证模式
- 依赖漏洞""",
        "performance": """## 任务

审查代码中的性能问题：
- 热路径中的 O(n²) 或更高复杂度
- 缺少缓存机会
- N+1 查询模式
- 不必要的内存分配
- 在 async 上下文中使用阻塞 I/O""",
        "style": """## 任务

审查代码的可读性和可维护性：
- 命名不清晰、魔法数字
- 公共 API 缺少文档字符串
- 过于复杂的函数（>30 行）
- 死代码、未使用的导入
- 与代码库其他部分的模式不一致""",
    }
    return missions.get(reviewer_type, "## 任务\n\n审查代码变更并报告发现。")


def _verifier_mission(ctx: dict[str, Any]) -> str:
    return """## 任务

对每个候选发现，判断：
- **confirmed**：问题是真实且可操作的
- **false_positive**：发现是错误的、不适用的、或噪音太大

严格标准。只确认你有把握的发现。
如果置信度 < 0.6，标记为 false_positive。"""


def _anti_patterns(ctx: dict[str, Any]) -> str:
    return """## 反模式（禁止）

- 不要编造没有代码依据的发现
- 不要报告 PR 未改动的行上的问题
- 不要在不同文件中重复同一个发现
- 不要建议与 PR 目的无关的重构
- 不要在建议中留占位符文本"""


def _findings_format(ctx: dict[str, Any]) -> str:
    return """## 发现格式

每个发现必须包含：
- `file`: diff 中的精确文件路径
- `line`: 变更文件中的精确行号
- `severity`: "info" | "warning" | "error"
- `category`: 简短标签（如 "sql-injection", "n-plus-one", "naming"）
- `message`: 问题是什么（1-2 句话，中文）
- `suggestion`: 如何修复（具体的代码建议，中文）
- `confidence`: 0.0-1.0（你对这个发现的把握程度）"""


def build_planner_prompt(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Build system + user messages for the Planner agent."""
    sections = [_identity, _language, _planner_mission, _available_tools, _output_contract, _anti_patterns]
    system_parts = [s({**ctx, "role": "planner", "agent_name": "planner"}) for s in sections]
    system = "\n\n".join(p for p in system_parts if p)

    user = f"""## PR 上下文

**仓库**: {ctx.get('repo', 'unknown')}
**PR #{ctx.get('pr_number', '?')}**: {ctx.get('pr_title', '')}
**变更文件**: {', '.join(ctx.get('files_changed', []))}

## Diff 摘要

{ctx.get('diff_summary', '无 diff 数据。')}

## 指示

分析 diff 并派发 Reviewer。输出 JSON。"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_reviewer_prompt(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Build system + user messages for a Reviewer agent."""
    reviewer_type = ctx.get("reviewer_type", "general")
    sections = [_identity, _language, _reviewer_mission, _available_tools, _findings_format, _output_contract, _anti_patterns]
    system_parts = [s({**ctx, "role": "reviewer", "agent_name": f"{reviewer_type}_reviewer"}) for s in sections]
    system = "\n\n".join(p for p in system_parts if p)

    files_to_review = ctx.get("files_to_review", [])
    diffs = ctx.get("diffs", {})

    diff_text = ""
    for f in files_to_review:
        diff_text += f"### {f}\n```\n{diffs.get(f, '无 diff 数据。')}\n```\n\n"

    user = f"""## 待审查文件

{diff_text}

## 指示

审查上述变更，以 JSON 格式报告发现。"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_verifier_prompt(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Build system + user messages for the Verifier agent."""
    sections = [_identity, _language, _verifier_mission, _output_contract, _anti_patterns]
    system_parts = [s({**ctx, "role": "verifier", "agent_name": "verifier"}) for s in sections]
    system = "\n\n".join(p for p in system_parts if p)

    findings = ctx.get("candidate_findings", [])
    findings_text = "\n".join(
        f"- [{f['id']}] {f['file']}:{f['line']} ({f['severity']}) {f['message']}"
        for f in findings
    )

    user = f"""## 候选发现

{findings_text}

## 指示

对每个发现，判断是 confirmed 还是 false_positive。输出 JSON。"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
