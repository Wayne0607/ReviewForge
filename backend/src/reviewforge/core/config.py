"""Configuration — YAML-based config with env var overrides.

Config priority: environment variables > reviewforge.yaml > defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_VALID_EVIDENCE_MODES = frozenset({"off", "shadow", "enforce"})
_VALID_PUBLICATION_POLICY_MODES = _VALID_EVIDENCE_MODES
_VALID_OUTPUT_LANGUAGES = frozenset({"auto", "en", "zh-CN"})
_VALID_PIPELINE_MODES = frozenset({"legacy", "shadow", "hypothesis"})


def _normalize_int(value: Any, default: int, minimum: int = 1) -> int:
    """Parse an int from *value*, clamping to *minimum*. Returns *default* on failure."""
    try:
        result = int(value)
    except (ValueError, TypeError):
        return default
    return max(result, minimum)


def _normalize_float(value: Any, default: float, minimum: float = 0.0) -> float:
    """Parse a float from *value*, clamping to *minimum*. Returns *default* on failure."""
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    return max(result, minimum)


def _normalize_evidence_mode(value: Any) -> str:
    """Return a valid evidence mode, falling back to ``shadow``."""
    if isinstance(value, str) and value.strip().lower() in _VALID_EVIDENCE_MODES:
        return value.strip().lower()
    return "shadow"


def _normalize_publication_policy_mode(value: Any) -> str:
    """Return a valid publication-policy mode, falling back to ``off``."""
    if isinstance(value, str) and value.strip().lower() in _VALID_PUBLICATION_POLICY_MODES:
        return value.strip().lower()
    return "off"


def _normalize_output_language(value: Any, default: str = "zh-CN") -> str:
    """Return one of the supported output languages.

    The legacy reviewer path has historically emitted Simplified Chinese, so
    its default is deliberately different from the new pipeline's ``auto``
    default.  Invalid values retain the current config value instead of
    silently switching an existing deployment to another language.
    """

    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lower() == "zh-cn":
            return "zh-CN"
        if candidate in {"auto", "en"}:
            return candidate
    if default == "auto":
        return "auto"
    if default == "en":
        return "en"
    return "zh-CN"


def _normalize_pipeline_mode(value: Any, default: str = "legacy") -> str:
    if isinstance(value, str) and value.strip().lower() in _VALID_PIPELINE_MODES:
        return value.strip().lower()
    return default if default in _VALID_PIPELINE_MODES else "legacy"


def _publication_policy_from_dict(data: dict[str, Any]) -> PublicationPolicyConfigYAML:
    cfg = PublicationPolicyConfigYAML()
    if not isinstance(data, dict):
        return cfg
    if "enabled" in data:
        cfg.enabled = _parse_bool(data["enabled"])
    if "mode" in data:
        cfg.mode = _normalize_publication_policy_mode(data["mode"])
    if "budget_enabled" in data:
        cfg.budget_enabled = _parse_bool(data["budget_enabled"])
    if "max_comments" in data:
        try:
            cfg.max_comments = max(1, int(data["max_comments"]))
        except (TypeError, ValueError):
            pass
    if "high_risk_overflow" in data:
        try:
            cfg.high_risk_overflow = max(0, int(data["high_risk_overflow"]))
        except (TypeError, ValueError):
            pass
    return cfg


def _parse_bool(value: Any) -> bool:
    """Parse a bool from YAML or env, handling string representations."""
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "")
    return bool(value)


@dataclass
class PublicationPolicyConfigYAML:
    """YAML shape for the publication policy block.

    Mirrors ``reviewforge.engine.publication_policy.PublicationPolicyConfig``;
    kept here to avoid coupling the YAML loader to the engine module.
    """

    enabled: bool = False
    mode: str = "off"
    budget_enabled: bool = True
    max_comments: int = 4
    high_risk_overflow: int = 1


@dataclass
class V3Config:
    """V3 coverage-driven pipeline configuration."""

    enabled: bool = False
    coverage_min_risk_score: float = 0.15
    coverage_max_cells_per_round: int = 24
    coverage_max_attempts: int = 2
    evidence_mode: str = "shadow"
    evidence_max_candidates: int = 20


@dataclass
class PipelineV4Config:
    """Hypothesis-pipeline limits and kill switch.

    ``legacy`` is deliberately the library and YAML default until the holdout
    gate in T11 passes.
    """

    mode: str = "legacy"
    output_language: str = "auto"
    workspace_max_bytes: int = 200_000_000
    context_pack_max_slices: int = 12
    context_pack_max_chars: int = 40_000
    generator_max_input_chars: int = 120_000
    generator_max_hypotheses: int = 12
    max_lenses: int = 3
    investigator_concurrency: int = 4
    investigator_max_hypotheses_per_pr: int = 12
    publish_max_inline: int = 5
    publish_max_inline_overflow: int = 8


def _pipeline_v4_from_dict(data: dict[str, Any]) -> PipelineV4Config:
    cfg = PipelineV4Config()
    if not isinstance(data, dict):
        return cfg
    if "mode" in data:
        cfg.mode = _normalize_pipeline_mode(data["mode"], cfg.mode)
    if "output_language" in data:
        cfg.output_language = _normalize_output_language(data["output_language"], cfg.output_language)
    for name in (
        "workspace_max_bytes",
        "context_pack_max_slices",
        "context_pack_max_chars",
        "generator_max_input_chars",
        "generator_max_hypotheses",
        "max_lenses",
        "investigator_concurrency",
        "investigator_max_hypotheses_per_pr",
        "publish_max_inline",
        "publish_max_inline_overflow",
    ):
        if name in data:
            setattr(cfg, name, _normalize_int(data[name], getattr(cfg, name), 0 if name.endswith("overflow") else 1))
    return cfg


@dataclass
class ModelProfile:
    """A named model configuration for multi-model routing."""

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 4096


@dataclass
class RoleOverride:
    """Per-role LLM override for the 5 fixed functional roles.

    Empty fields fall back to the global LLMConfig.  Used by the
    single-admin console so each role can point at a different endpoint,
    key, or model without changing the global config.
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""


# The five fixed functional roles.  Centralised here so model_router,
# llm_settings, admin API and frontend all reference the same set.
ROLE_NAMES: tuple[str, ...] = (
    "planner",
    "fast_review",
    "deep_review",
    "verifier",
    "publication_gate",
)


@dataclass
class LLMConfig:
    base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    api_key: str = ""
    model: str = "mimo-v2.5-pro"
    temperature_planner: float = 0.0
    temperature_reviewer: float = 0.1
    temperature_verifier: float = 0.0
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    # Per-role overrides keyed by one of ROLE_NAMES.  Missing roles fall
    # back to base_url/api_key/model at the global level.  Legacy fast/
    # accurate profile routing remains available for compatibility.
    role_overrides: dict[str, RoleOverride] = field(default_factory=dict)


@dataclass
class ReviewerConfig:
    name: str = ""
    type: str = ""  # security / performance / style
    enabled: bool = True
    max_steps: int = 8
    max_findings: int = 20
    confidence_threshold: float = 0.5


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class GitHubConfig:
    token: str = ""
    webhook_secret: str = ""


def _v3_from_dict(data: dict[str, Any]) -> V3Config:
    """Build a V3Config from a YAML dict, silently normalizing bad values."""
    cfg = V3Config()
    if "enabled" in data:
        cfg.enabled = _parse_bool(data["enabled"])
    if "coverage_min_risk_score" in data:
        cfg.coverage_min_risk_score = _normalize_float(data["coverage_min_risk_score"], 0.15, 0.0)
    if "coverage_max_cells_per_round" in data:
        cfg.coverage_max_cells_per_round = _normalize_int(data["coverage_max_cells_per_round"], 24, 1)
    if "coverage_max_attempts" in data:
        cfg.coverage_max_attempts = _normalize_int(data["coverage_max_attempts"], 2, 1)
    if "evidence_mode" in data:
        cfg.evidence_mode = _normalize_evidence_mode(data["evidence_mode"])
    if "evidence_max_candidates" in data:
        cfg.evidence_max_candidates = _normalize_int(data["evidence_max_candidates"], 20, 1)
    return cfg


@dataclass
class ReviewForgeConfig:
    """Top-level configuration."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    reviewers: list[ReviewerConfig] = field(default_factory=list)
    # Keep the legacy path's historical Chinese output unless an operator
    # explicitly selects another language.  PipelineV4Config (added by T4)
    # owns the new pipeline's ``auto`` default; this field is the compatibility
    # bridge for existing planner/reviewer calls and benchmark runners.
    output_language: str = "zh-CN"
    skills_dir: str = "skills"
    events_dir: str = ".reviewforge/events"
    confidence_threshold: float = 0.5
    # The versioned project config selects production tool loops. Keep the
    # library default empty so embedding applications opt into their own cost.
    agentic_reviewers: list[str] = field(default_factory=list)
    agentic_default: bool = False  # default OFF — escalate-on-uncertainty replaces full agentic

    # Escalation: auto-verify uncertain findings with agentic tools
    escalation_enabled: bool = True
    escalation_confidence_min: float = 0.4
    escalation_confidence_max: float = 0.7
    escalation_max_steps: int = 3
    escalation_max_tokens: int = 5000

    # Final agentic publication gate. Unlike calibration, this verifier reads
    # the full file and may search repository contracts before a confirmed
    # finding is allowed to become a review comment.
    publication_gate_enabled: bool = False
    publication_gate_max_steps: int = 4
    publication_gate_max_tokens: int = 6000
    publication_gate_concurrency: int = 4
    # Phase 2 (perf/gate-dedup-20260729): collapse same-root-cause findings
    # before triage / gate so the LLM call volume matches the unique defect
    # count instead of the multi-reviewer fan-out.  Default ON; flip to
    # False via env var ``REVIEWFORGE_PUBLICATION_GATE_DEDUP=0`` if a
    # regression appears.
    publication_gate_dedup: bool = True
    publication_triage_enabled: bool = False
    publication_triage_batch_size: int = 6
    publication_triage_concurrency: int = 1
    publication_triage_max_candidates: int = 24
    publication_triage_context_lines: int = 12
    publication_triage_max_tokens: int = 4000
    # Zero-token semantic root-cause families for high-volume security
    # duplicates. Operational kill switch:
    # REVIEWFORGE_ROOT_CAUSE_EXTENDED_FAMILIES=0.
    root_cause_extended_families: bool = True

    # Model-agnostic publication policy (Stage 1). Library default is OFF
    # so embedding applications opt in explicitly. Production enables
    # ``shadow`` first and only switches to ``enforce`` after replay
    # validation. Owned by the engine via ``PublicationPolicy``.
    publication_policy: PublicationPolicyConfigYAML = field(default_factory=PublicationPolicyConfigYAML)

    # Selective second pass for high-risk changed symbols that received no
    # finding in the broad first pass. Disabled by default for embedders;
    # production opts in through reviewforge.yaml.
    coverage_gap_enabled: bool = False
    coverage_gap_min_risk_score: int = 4
    coverage_gap_max_cards: int = 3
    coverage_gap_min_confidence: float = 0.65

    # V3 coverage-driven pipeline
    v3: V3Config = field(default_factory=V3Config)
    pipeline_v4: PipelineV4Config = field(default_factory=PipelineV4Config)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> ReviewForgeConfig:
        """Load config from YAML file, with env var overrides."""
        cfg = cls()

        # 1. Load from YAML if exists
        if config_path:
            path = Path(config_path)
        else:
            path = cls._find_default_config_path()
        config_base = path.parent.resolve() if path.exists() else Path.cwd().resolve()

        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg._apply_dict(data)

        # 2. Environment variable overrides
        cfg._apply_env()
        cfg._normalize_paths(config_base)

        # 3. Set defaults for reviewers if empty
        if not cfg.reviewers:
            cfg.reviewers = [
                ReviewerConfig(name="security_reviewer", type="security", max_steps=10),
                ReviewerConfig(name="performance_reviewer", type="performance", max_steps=8),
                ReviewerConfig(name="correctness_reviewer", type="correctness", max_steps=6),
                ReviewerConfig(name="style_reviewer", type="style", max_steps=6),
                ReviewerConfig(name="testing_reviewer", type="testing", max_steps=6),
                ReviewerConfig(name="doc_reviewer", type="documentation", max_steps=5),
                ReviewerConfig(name="dependency_reviewer", type="dependency", max_steps=6),
                ReviewerConfig(name="accessibility_reviewer", type="accessibility", max_steps=6),
            ]

        return cfg

    @staticmethod
    def _find_default_config_path() -> Path:
        """Find reviewforge.yaml from cwd or its parents, falling back to cwd."""
        cwd = Path.cwd().resolve()
        for base in (cwd, *cwd.parents):
            candidate = base / "reviewforge.yaml"
            if candidate.exists():
                return candidate
        return cwd / "reviewforge.yaml"

    def _normalize_paths(self, config_base: Path) -> None:
        """Resolve relative runtime paths so commands work from repo root or backend/."""
        package_skills = Path(__file__).resolve().parent.parent / "skills"

        skills = Path(self.skills_dir)
        if not skills.is_absolute():
            candidates = [
                config_base / skills,
                Path.cwd().resolve() / skills,
                package_skills,
            ]
            self.skills_dir = str(next((p for p in candidates if p.exists()), candidates[0]))

        events = Path(self.events_dir)
        if not events.is_absolute():
            self.events_dir = str(config_base / events)

    def _apply_dict(self, data: dict[str, Any]) -> None:
        """Apply values from a dict."""
        if "llm" in data:
            for k, v in data["llm"].items():
                if k == "profiles" and isinstance(v, dict):
                    self.llm.profiles = {name: ModelProfile(**p) if isinstance(p, dict) else p for name, p in v.items()}
                elif k == "role_overrides" and isinstance(v, dict):
                    self.llm.role_overrides = {
                        name: RoleOverride(
                            base_url=str(raw.get("base_url") or ""),
                            api_key=str(raw.get("api_key") or ""),
                            model=str(raw.get("model") or ""),
                        )
                        for name, raw in v.items()
                        if name in ROLE_NAMES and isinstance(raw, dict)
                    }
                elif hasattr(self.llm, k):
                    setattr(self.llm, k, v)
        if "server" in data:
            for k, v in data["server"].items():
                if hasattr(self.server, k):
                    setattr(self.server, k, v)
        if "github" in data:
            for k, v in data["github"].items():
                if hasattr(self.github, k):
                    setattr(self.github, k, v)
        if "reviewers" in data:
            self.reviewers = [ReviewerConfig(**r) for r in data["reviewers"]]
        if "output_language" in data:
            self.output_language = _normalize_output_language(data["output_language"], self.output_language)
        # The architecture spec names this setting ``review.output_language``
        # while the current top-level config has no ReviewConfig object.  Read
        # the namespaced form as a compatibility alias without changing the
        # existing config shape.
        review = data.get("review")
        if isinstance(review, dict) and "output_language" in review:
            self.output_language = _normalize_output_language(review["output_language"], self.output_language)
        if "skills_dir" in data:
            self.skills_dir = data["skills_dir"]
        if "events_dir" in data:
            self.events_dir = data["events_dir"]
        if "confidence_threshold" in data:
            self.confidence_threshold = data["confidence_threshold"]
        if "agentic_reviewers" in data and isinstance(data["agentic_reviewers"], list):
            self.agentic_reviewers = [str(name).strip() for name in data["agentic_reviewers"] if str(name).strip()]
        if "agentic_default" in data:
            value = data["agentic_default"]
            self.agentic_default = (
                value.strip().lower() not in ("0", "false", "no", "") if isinstance(value, str) else bool(value)
            )
        if "escalation" in data:
            esc = data["escalation"]
            _esc_types = {
                "enabled": bool,
                "confidence_min": float,
                "confidence_max": float,
                "max_steps": int,
                "max_tokens": int,
            }
            for k, v in esc.items():
                attr = f"escalation_{k}"
                if hasattr(self, attr):
                    expected = _esc_types.get(k)
                    if expected:
                        try:
                            v = expected(v)
                        except (ValueError, TypeError):
                            pass
                    setattr(self, attr, v)
        if "publication_gate" in data:
            gate = data["publication_gate"] or {}
            _gate_types = {
                "enabled": bool,
                "max_steps": int,
                "max_tokens": int,
                "concurrency": int,
                "dedup": bool,
            }
            for key, value in gate.items():
                attr = f"publication_gate_{key}"
                if not hasattr(self, attr):
                    continue
                expected = _gate_types.get(key)
                if expected:
                    try:
                        value = (
                            value.strip().lower() not in ("0", "false", "no", "")
                            if expected is bool and isinstance(value, str)
                            else expected(value)
                        )
                    except (ValueError, TypeError):
                        continue
                setattr(self, attr, value)
        if "publication_triage" in data:
            triage = data["publication_triage"] or {}
            triage_types = {
                "enabled": bool,
                "batch_size": int,
                "concurrency": int,
                "max_candidates": int,
                "context_lines": int,
                "max_tokens": int,
            }
            for key, value in triage.items():
                attr = f"publication_triage_{key}"
                if not hasattr(self, attr):
                    continue
                expected = triage_types.get(key)
                if expected:
                    try:
                        value = (
                            value.strip().lower() not in ("0", "false", "no", "")
                            if expected is bool and isinstance(value, str)
                            else expected(value)
                        )
                    except (ValueError, TypeError):
                        continue
                setattr(self, attr, value)
        if "coverage_gap" in data:
            gap = data["coverage_gap"] or {}
            _gap_types = {
                "enabled": bool,
                "min_risk_score": int,
                "max_cards": int,
                "min_confidence": float,
            }
            for key, value in gap.items():
                attr = f"coverage_gap_{key}"
                if not hasattr(self, attr):
                    continue
                expected = _gap_types.get(key)
                if expected:
                    try:
                        value = (
                            value.strip().lower() not in ("0", "false", "no", "")
                            if expected is bool and isinstance(value, str)
                            else expected(value)
                        )
                    except (ValueError, TypeError):
                        pass
                setattr(self, attr, value)
        if "v3" in data:
            v3 = data["v3"]
            if isinstance(v3, dict):
                self.v3 = _v3_from_dict(v3)
        if "pipeline_v4" in data:
            self.pipeline_v4 = _pipeline_v4_from_dict(data["pipeline_v4"])
        if "publication_policy" in data:
            self.publication_policy = _publication_policy_from_dict(data["publication_policy"])

    def _apply_env(self) -> None:
        """Environment variables override config file."""
        self.github.token = os.environ.get("GITHUB_TOKEN", self.github.token)
        self.github.webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", self.github.webhook_secret)
        self.llm.base_url = os.environ.get("LLM_BASE_URL", self.llm.base_url)
        self.llm.api_key = os.environ.get("LLM_API_KEY", self.llm.api_key)
        self.llm.model = os.environ.get("REVIEWFORGE_MODEL", self.llm.model)
        output_language = os.environ.get("REVIEWFORGE_OUTPUT_LANGUAGE")
        if output_language is not None:
            self.output_language = _normalize_output_language(output_language, self.output_language)
            self.pipeline_v4.output_language = _normalize_output_language(
                output_language, self.pipeline_v4.output_language
            )
        pipeline_mode = os.environ.get("REVIEWFORGE_PIPELINE")
        if pipeline_mode is not None:
            self.pipeline_v4.mode = _normalize_pipeline_mode(pipeline_mode, self.pipeline_v4.mode)
        self.server.host = os.environ.get("REVIEWFORGE_HOST", self.server.host)
        port = os.environ.get("REVIEWFORGE_PORT")
        if port:
            self.server.port = int(port)
        # W1: agentic reviewers (comma-separated allowlist)
        agentic = os.environ.get("REVIEWFORGE_AGENTIC_REVIEWERS", "")
        if agentic:
            self.agentic_reviewers = [r.strip() for r in agentic.split(",") if r.strip()]
        # #1: agentic tool loop is the default for all reviewers (when no explicit allowlist)
        default_flag = os.environ.get("REVIEWFORGE_AGENTIC_DEFAULT")
        if default_flag is not None:
            self.agentic_default = default_flag.strip().lower() not in ("0", "false", "no", "")
        # Escalation env overrides
        esc_flag = os.environ.get("REVIEWFORGE_ESCALATION_ENABLED")
        if esc_flag is not None:
            self.escalation_enabled = esc_flag.strip().lower() not in ("0", "false", "no", "")
        gap_flag = os.environ.get("REVIEWFORGE_COVERAGE_GAP_ENABLED")
        if gap_flag is not None:
            self.coverage_gap_enabled = gap_flag.strip().lower() not in ("0", "false", "no", "")
        # V3 env overrides
        v3_enabled = os.environ.get("REVIEWFORGE_V3_ENABLED")
        if v3_enabled is not None:
            self.v3.enabled = _parse_bool(v3_enabled)
        v3_mode = os.environ.get("REVIEWFORGE_V3_EVIDENCE_MODE")
        if v3_mode is not None:
            self.v3.evidence_mode = _normalize_evidence_mode(v3_mode)
        # Publication-policy env overrides
        pp_enabled = os.environ.get("REVIEWFORGE_PUBLICATION_POLICY_ENABLED")
        if pp_enabled is not None:
            self.publication_policy.enabled = _parse_bool(pp_enabled)
        pp_mode = os.environ.get("REVIEWFORGE_PUBLICATION_POLICY_MODE")
        if pp_mode is not None:
            self.publication_policy.mode = _normalize_publication_policy_mode(pp_mode)
        pp_budget_enabled = os.environ.get("REVIEWFORGE_PUBLICATION_POLICY_BUDGET_ENABLED")
        if pp_budget_enabled is not None:
            self.publication_policy.budget_enabled = _parse_bool(pp_budget_enabled)
        # REVIEWFORGE_PUBLICATION_POLICY_MAX_COMMENTS — top-N budget.  Must
        # be >= 1; bad values fall back to the YAML/default value rather
        # than disabling the policy.
        pp_max_comments = os.environ.get("REVIEWFORGE_PUBLICATION_POLICY_MAX_COMMENTS")
        if pp_max_comments is not None:
            try:
                self.publication_policy.max_comments = max(1, int(pp_max_comments))
            except (TypeError, ValueError):
                pass
        # REVIEWFORGE_PUBLICATION_POLICY_HIGH_RISK_OVERFLOW — bounded
        # detector-error overflow slots.  0 disables overflow; negative
        # values clamp to 0; bad values fall back to the YAML/default.
        pp_overflow = os.environ.get("REVIEWFORGE_PUBLICATION_POLICY_HIGH_RISK_OVERFLOW")
        if pp_overflow is not None:
            try:
                self.publication_policy.high_risk_overflow = max(0, int(pp_overflow))
            except (TypeError, ValueError):
                pass
        # Phase 1 (perf/gate-dedup-20260729): publication_gate & triage env
        # overrides.  These were previously yaml-only; promoting them lets
        # operators tighten the gate without redeploying.  Bad values fall
        # back to the YAML/default rather than disabling the gate.
        gate_enabled = os.environ.get("REVIEWFORGE_PUBLICATION_GATE_ENABLED")
        if gate_enabled is not None:
            self.publication_gate_enabled = gate_enabled.strip().lower() not in ("0", "false", "no", "")
        gate_steps = os.environ.get("REVIEWFORGE_PUBLICATION_GATE_MAX_STEPS")
        if gate_steps is not None:
            try:
                self.publication_gate_max_steps = max(1, int(gate_steps))
            except (TypeError, ValueError):
                pass
        gate_tokens = os.environ.get("REVIEWFORGE_PUBLICATION_GATE_MAX_TOKENS")
        if gate_tokens is not None:
            try:
                self.publication_gate_max_tokens = max(500, int(gate_tokens))
            except (TypeError, ValueError):
                pass
        gate_conc = os.environ.get("REVIEWFORGE_PUBLICATION_GATE_CONCURRENCY")
        if gate_conc is not None:
            try:
                self.publication_gate_concurrency = max(1, int(gate_conc))
            except (TypeError, ValueError):
                pass
        gate_dedup = os.environ.get("REVIEWFORGE_PUBLICATION_GATE_DEDUP")
        if gate_dedup is not None:
            self.publication_gate_dedup = gate_dedup.strip().lower() not in ("0", "false", "no", "")
        triage_enabled = os.environ.get("REVIEWFORGE_PUBLICATION_TRIAGE_ENABLED")
        if triage_enabled is not None:
            self.publication_triage_enabled = triage_enabled.strip().lower() not in ("0", "false", "no", "")
        triage_batch = os.environ.get("REVIEWFORGE_PUBLICATION_TRIAGE_BATCH_SIZE")
        if triage_batch is not None:
            try:
                self.publication_triage_batch_size = max(1, int(triage_batch))
            except (TypeError, ValueError):
                pass
        triage_tokens = os.environ.get("REVIEWFORGE_PUBLICATION_TRIAGE_MAX_TOKENS")
        if triage_tokens is not None:
            try:
                self.publication_triage_max_tokens = max(500, int(triage_tokens))
            except (TypeError, ValueError):
                pass
        root_cause_extended = os.environ.get("REVIEWFORGE_ROOT_CAUSE_EXTENDED_FAMILIES")
        if root_cause_extended is not None:
            self.root_cause_extended_families = root_cause_extended.strip().lower() not in (
                "0",
                "false",
                "no",
                "",
            )
