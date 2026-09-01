"""Immutable built-in reviewer metadata.

The catalog owns the names and routing metadata that must agree across the
planner, registry, scheduler, model router, prompt builder, reviewer factory,
and coverage-closure pipeline.  Runtime/plugin reviewers remain outside this
built-in catalog and continue to use their explicit specs.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType


def normalize_planner_reviewer_name(value: str) -> str:
    """Normalize an untrusted planner reviewer label before alias lookup."""

    return value.strip().lower().replace(" ", "_").replace("-", "_")


@dataclass(frozen=True, slots=True)
class ReviewerDefinition:
    """One immutable built-in reviewer definition."""

    name: str
    reviewer_type: str
    description: str
    allowed_tools: tuple[str, ...]
    max_steps: int
    planner_aliases: tuple[str, ...]
    planner_guidance: str
    planner_enabled: bool
    priority: int
    max_findings: int
    model_role: str
    model_profile: str
    broad_dimensions: tuple[str, ...]
    closure_dimensions: tuple[str, ...] = ()
    registry_model_profile: str = "reviewer"


class ReviewerCatalog(Mapping[str, ReviewerDefinition]):
    """Validated, immutable lookup for all built-in reviewer definitions."""

    def __init__(self, definitions: Iterable[ReviewerDefinition]) -> None:
        ordered = tuple(definitions)
        by_name: dict[str, ReviewerDefinition] = {}
        by_type: dict[str, str] = {}
        aliases: dict[str, str] = {}
        closure_reviewers: dict[str, str] = {}

        for definition in ordered:
            if definition.name in by_name:
                raise ValueError(f"Duplicate canonical reviewer name: {definition.name}")
            if not definition.name.endswith("_reviewer"):
                raise ValueError(f"Canonical reviewer name must end in '_reviewer': {definition.name}")
            if definition.reviewer_type in by_type:
                raise ValueError(f"Duplicate reviewer prompt type: {definition.reviewer_type}")
            if definition.max_steps < 1:
                raise ValueError(f"Reviewer max_steps must be positive: {definition.name}")
            if definition.priority < 0:
                raise ValueError(f"Reviewer priority cannot be negative: {definition.name}")
            if definition.planner_enabled and not definition.planner_guidance.strip():
                raise ValueError(f"Planner-enabled reviewer needs guidance: {definition.name}")
            if definition.max_findings < 1:
                raise ValueError(f"Reviewer max_findings must be positive: {definition.name}")

            by_name[definition.name] = definition
            by_type[definition.reviewer_type] = definition.name

            if definition.planner_enabled:
                planner_names = (definition.name, *definition.planner_aliases)
                for alias in planner_names:
                    normalized = normalize_planner_reviewer_name(alias)
                    previous = aliases.get(normalized)
                    if previous is not None and previous != definition.name:
                        raise ValueError(
                            f"Planner alias {normalized!r} maps to both {previous!r} and {definition.name!r}"
                        )
                    aliases[normalized] = definition.name

            for dimension in definition.closure_dimensions:
                previous = closure_reviewers.get(dimension)
                if previous is not None:
                    raise ValueError(
                        f"Closure dimension {dimension!r} maps to both {previous!r} and {definition.name!r}"
                    )
                closure_reviewers[dimension] = definition.name

        self._ordered = ordered
        self._by_name = MappingProxyType(by_name)
        self._by_type = MappingProxyType(by_type)
        self._planner_aliases = MappingProxyType(aliases)
        self._closure_reviewers = MappingProxyType(closure_reviewers)

    def __getitem__(self, name: str) -> ReviewerDefinition:
        return self._by_name[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    @property
    def definitions(self) -> tuple[ReviewerDefinition, ...]:
        return self._ordered

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    @property
    def priorities(self) -> Mapping[str, int]:
        return MappingProxyType({item.name: item.priority for item in self._ordered})

    @property
    def model_roles(self) -> Mapping[str, str]:
        return MappingProxyType({item.name: item.model_role for item in self._ordered})

    @property
    def model_profiles(self) -> Mapping[str, str]:
        return MappingProxyType({item.name: item.model_profile for item in self._ordered})

    def canonical_name_for_type(self, reviewer_type: str) -> str:
        """Return the canonical agent name for a prompt reviewer type."""

        return self._by_type[reviewer_type]

    def resolve_planner_name(self, value: str) -> str | None:
        """Resolve an enabled Planner alias to its canonical reviewer name."""

        return self._planner_aliases.get(normalize_planner_reviewer_name(value))

    def planner_definitions(self) -> tuple[ReviewerDefinition, ...]:
        return tuple(item for item in self._ordered if item.planner_enabled)

    def reviewer_for_closure_dimension(self, dimension: str) -> str:
        """Return the unique built-in reviewer responsible for closing a dimension."""

        return self._closure_reviewers[dimension]

    def assert_factory_keys(self, keys: Set[str] | Iterable[str]) -> None:
        """Fail fast when the built-in reviewer factory drifts from the catalog."""

        actual = frozenset(keys)
        expected = self.names
        if actual == expected:
            return
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            f"Reviewer factory/catalog mismatch: missing={missing or 'none'}, unexpected={unexpected or 'none'}"
        )


_READ_CONTEXT = ("read_diff", "read_file", "search_code", "get_change_context")
_READ_CHANGED_CONTEXT = ("read_diff", "read_file", "get_change_context")


REVIEWER_CATALOG = ReviewerCatalog(
    (
        ReviewerDefinition(
            name="security_reviewer",
            reviewer_type="security",
            description="Reviews code for security vulnerabilities",
            allowed_tools=_READ_CONTEXT,
            max_steps=10,
            planner_aliases=("security",),
            planner_guidance=(
                "- **Security Reviewer（必须派发，如果代码涉及以下任何一项）**：命令/SQL 注入、"
                "不安全反序列化、硬编码凭据、路径遍历、未验证用户输入、网络请求或加密操作"
            ),
            planner_enabled=True,
            priority=100,
            max_findings=15,
            model_role="deep_review",
            model_profile="accurate",
            broad_dimensions=("security",),
            closure_dimensions=("security",),
        ),
        ReviewerDefinition(
            name="performance_reviewer",
            reviewer_type="performance",
            description="Reviews code for performance issues",
            allowed_tools=_READ_CONTEXT,
            max_steps=8,
            planner_aliases=("performance",),
            planner_guidance=(
                "- Performance Reviewer：diff 显示无界工作、泄漏、N+1、高阶热路径、事件循环阻塞，"
                "或在重复路径上用线性遍历替代常数时间操作时派发；仅局部少一次分配不必派发"
            ),
            planner_enabled=True,
            priority=70,
            max_findings=10,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=("performance",),
            closure_dimensions=("performance",),
        ),
        ReviewerDefinition(
            name="style_reviewer",
            reviewer_type="style",
            description="Reviews code for readability and style issues",
            allowed_tools=_READ_CHANGED_CONTEXT,
            max_steps=6,
            planner_aliases=(),
            planner_guidance="",
            planner_enabled=False,
            priority=20,
            max_findings=5,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=(),
        ),
        ReviewerDefinition(
            name="correctness_reviewer",
            reviewer_type="correctness",
            description="Reviews changed behavior for concrete correctness and contract failures",
            allowed_tools=_READ_CONTEXT,
            max_steps=6,
            # Stable-baseline compatibility: style/readability proposals are
            # intentionally folded into correctness instead of dispatching the
            # disabled style reviewer.
            planner_aliases=(
                "correctness",
                "style",
                "style_reviewer",
                "architecture",
                "readability",
            ),
            planner_guidance=(
                "- Correctness Reviewer：对源代码变更默认派发，只查错误变量/调用、分支、状态、返回值、"
                "契约、并发和生命周期导致的可观察错误；不查命名、可读性、重构偏好或微优化"
            ),
            planner_enabled=True,
            priority=25,
            max_findings=6,
            model_role="deep_review",
            model_profile="accurate",
            broad_dimensions=("correctness",),
            closure_dimensions=(
                "correctness",
                "contract",
                "error-handling",
                "compatibility",
                "cross-PR",
            ),
        ),
        ReviewerDefinition(
            name="localization_reviewer",
            reviewer_type="localization",
            description="Reviews locale resources for language, placeholder, and encoding defects",
            allowed_tools=_READ_CHANGED_CONTEXT,
            max_steps=4,
            planner_aliases=("localization", "localisation", "i18n", "l10n"),
            planner_guidance=(
                "- Localization Reviewer：修改 locale 资源文件（.po/.properties/.arb/i18n JSON 等）时派发；"
                "只审查可验证的语言、占位符、标签、转义或编码缺陷"
            ),
            planner_enabled=True,
            priority=45,
            max_findings=6,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=("localization",),
            closure_dimensions=("localization",),
        ),
        ReviewerDefinition(
            name="testing_reviewer",
            reviewer_type="testing",
            description="Reviews code for testing issues — missing tests, poor coverage, edge cases",
            allowed_tools=_READ_CONTEXT,
            max_steps=6,
            planner_aliases=("testing", "test"),
            planner_guidance=("- Testing Reviewer：只有测试断言/测试文件被修改或安全修复删除了既有保护时派发"),
            planner_enabled=True,
            priority=40,
            max_findings=6,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=("testing",),
            closure_dimensions=("testing",),
        ),
        ReviewerDefinition(
            name="doc_reviewer",
            reviewer_type="documentation",
            description="Reviews code for documentation gaps — missing docstrings, type hints, comments",
            allowed_tools=_READ_CHANGED_CONTEXT,
            max_steps=5,
            planner_aliases=("documentation", "documentation_reviewer", "doc"),
            planner_guidance=("- Documentation Reviewer：只有文档文件被修改且可能与行为契约矛盾时派发"),
            planner_enabled=True,
            priority=30,
            max_findings=4,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=(),
        ),
        ReviewerDefinition(
            name="dependency_reviewer",
            reviewer_type="dependency",
            description="Reviews code for dependency risks — new deps, version locks, vulnerabilities",
            allowed_tools=_READ_CONTEXT,
            max_steps=6,
            planner_aliases=("dependency", "deps"),
            planner_guidance=("- Dependency Reviewer：修改了依赖文件（requirements.txt, pyproject.toml 等）时派发"),
            planner_enabled=True,
            priority=80,
            max_findings=10,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=(),
        ),
        ReviewerDefinition(
            name="accessibility_reviewer",
            reviewer_type="accessibility",
            description="Reviews code for accessibility issues — missing alt, aria labels, keyboard nav",
            allowed_tools=_READ_CHANGED_CONTEXT,
            max_steps=6,
            planner_aliases=("accessibility", "a11y"),
            planner_guidance=(
                "- Accessibility Reviewer：仅为自定义交互、键盘/焦点管理、ARIA 契约、媒体或动画等复杂语义派发；"
                "普通 img 的 missing-alt 已由确定性扫描覆盖"
            ),
            planner_enabled=True,
            priority=50,
            max_findings=6,
            model_role="fast_review",
            model_profile="fast",
            broad_dimensions=(),
        ),
    )
)
