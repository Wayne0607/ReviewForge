"""Protocol 10: completion-driven work, without product quotas.

Historical protocols retain their frozen allowances. Source identity, evidence
validation and host/provider resource protection are independent of this policy.
"""

from __future__ import annotations

from reviewforge.engine.review_session import fingerprint

# DeepSeek Chat Completions API's documented physical output maximum (2026-10-30).
# This is not a per-review allowance: every request may use the full capacity.
# https://api-docs.deepseek.com/api/create-chat-completion/
DEEPSEEK_OUTPUT_CAPACITY = 393_216


def output_capacity(protocol_version: int, historical: int) -> int:
    return DEEPSEEK_OUTPUT_CAPACITY if protocol_version >= 10 else historical


def repeated_state(record: dict, *, state, receipt) -> bool:
    """Detect a repeated failed transition, rather than count model attempts.

    Counters are deliberately absent. New source, acknowledged work, a different
    attempted repair or different feedback permits continuation. Persist hashes
    so a restart cannot conceal a cycle. A cycle remains an explicit unresolved
    result, never a clean review or a fabricated verdict.
    """
    signature = fingerprint({"state": state, "receipt": receipt})
    seen = record.setdefault("transition_fingerprints", [])
    if signature in seen:
        return True
    seen.append(signature)
    return False
