from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Binding:
    """Frozen identity of one review and its immutable source environment."""

    run_id: str
    repo: str
    pr_number: int
    base_sha: str
    head_sha: str
    source_environment: Any
    model_config: Any

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
