from __future__ import annotations

import pytest

from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.publication_evidence import protect_publication_finding


def _state(source: str, *, file: str = "app.py") -> StateStore:
    rendered = "\n".join(f"+{line}" for line in source.splitlines())
    count = max(1, len(source.splitlines()))
    patch = f"@@ -0,0 +1,{count} @@\n{rendered}"
    return StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="head",
        files_changed=[file],
        diff_summary=f"--- {file}\n{patch}",
        file_diffs={file: patch},
    )


def _finding(category: str, message: str, line: int) -> Finding:
    return Finding(
        id=f"finding-{line}",
        file="app.py",
        line=line,
        severity="error",
        category=category,
        message=message,
        suggestion="fix the concrete defect",
        confidence=0.9,
        reviewer="correctness_reviewer",
        status="confirmed",
    )


@pytest.mark.parametrize(
    ("source", "finding", "reason_fragment"),
    [
        (
            "def send():\n    with smtplib.SMTP(host, 587) as client:\n        client.login(user, password)\n",
            _finding("smtp-no-tls", "SMTP login sends the password without TLS", 2),
            "smtp-no-tls",
        ),
        (
            "def hash_password(password: str) -> str:\n    return hashlib.md5(password.encode()).hexdigest()\n",
            _finding("weak-password-hashing", "password storage uses unsalted MD5", 2),
            "weak-password-hash",
        ),
        (
            "def verify_password(password, password_hash):\n    return hmac.compare_digest(password, password_hash)\n",
            _finding("broken-auth", "verify_password compares the plaintext password with password_hash", 2),
            "password-verification",
        ),
        (
            "def save(user_id):\n    cursor.execute(f\"SELECT * FROM users WHERE id = '{user_id}'\")\n",
            _finding("sql-injection", "f-string SQL allows injection through user_id", 2),
            "sql-fstring",
        ),
        (
            "def dispatch() -> NotificationResult:\n    result = NotificationResult()\n    return\n",
            _finding("wrong-return-value", "bare return violates the declared return contract", 3),
            "bare-return",
        ),
        (
            'async def load():\n    with open("cache.bin", "rb") as stream:\n        return stream.read()\n',
            _finding("sync-io-in-async", "synchronous open blocks the async event loop", 2),
            "async-sync-io",
        ),
        (
            "async def load():\n    blob = _read_cached_payload(user_id)\n    return blob\n",
            _finding("event-loop-blocking", "synchronous cache read blocks the event loop", 2),
            "async-sync-io",
        ),
    ],
)
def test_direct_changed_source_protects_high_evidence_findings(
    source: str,
    finding: Finding,
    reason_fragment: str,
) -> None:
    decision = protect_publication_finding(finding, _state(source))

    assert decision.protected is True
    assert reason_fragment in decision.reason


def test_smtp_with_starttls_is_not_protected() -> None:
    state = _state(
        "def send():\n"
        "    with smtplib.SMTP(host, 587) as client:\n"
        "        client.starttls()\n"
        "        client.login(user, password)\n"
    )
    finding = _finding("smtp-no-tls", "SMTP login sends the password without TLS", 2)

    assert protect_publication_finding(finding, state).protected is False


def test_nearby_named_password_function_tolerates_an_imprecise_anchor() -> None:
    state = _state(
        "def sign(secret):\n"
        "    return hashlib.md5(secret.encode()).hexdigest()\n"
        "\n"
        "def hash_password(password: str) -> str:\n"
        "    return hashlib.md5(password.encode()).hexdigest()\n"
    )
    finding = _finding("weak-password-hashing", "hash_password uses unsalted MD5", 2)

    assert protect_publication_finding(finding, state).protected is True


def test_unrelated_md5_nearby_does_not_prove_weak_password_hashing() -> None:
    state = _state("def sign(secret):\n    return hashlib.md5(secret.encode()).hexdigest()\n")
    finding = _finding("weak-password-hashing", "password storage uses MD5", 2)

    assert protect_publication_finding(finding, state).protected is False


def test_generic_injection_word_does_not_prove_sql_injection() -> None:
    state = _state("def save(payload):\n    cursor.execute(f\"INSERT INTO inbox VALUES ('{json.dumps(payload)}')\")\n")
    finding = _finding(
        "serialization-injection",
        "json serialization is concatenated into a SQL string",
        2,
    )

    assert protect_publication_finding(finding, state).protected is False


def test_retry_guard_claim_does_not_borrow_bare_return_proof() -> None:
    state = _state(
        "def dispatch(retry_count: int) -> NotificationResult:\n"
        "    if retry_count >= 0:\n"
        "        return NotificationResult()\n"
        "    return\n"
    )
    finding = _finding(
        "wrong-condition",
        "retry_count >= 0 returns early before dispatching",
        2,
    )

    decision = protect_publication_finding(finding, state)

    assert decision.protected is True
    assert decision.dedup_key.endswith("retry-off-by-one:dispatch")


def test_two_sync_apis_in_one_async_function_keep_distinct_evidence_keys() -> None:
    state = _state(
        "async def dispatch(user_id):\n"
        "    prefs = load_user_preferences(user_id)\n"
        "    blob = _read_cached_payload(user_id)\n"
        "    return prefs, blob\n"
    )
    preferences = _finding(
        "sync-io-in-async",
        "load_user_preferences synchronously blocks the async event loop",
        2,
    )
    cache = _finding(
        "event-loop-blocking",
        "_read_cached_payload performs a synchronous cache read",
        3,
    )

    preferences_decision = protect_publication_finding(preferences, state)
    cache_decision = protect_publication_finding(cache, state)

    assert preferences_decision.dedup_key.endswith(":load_user_preferences")
    assert cache_decision.dedup_key.endswith(":_read_cached_payload")
    assert preferences_decision.dedup_key != cache_decision.dedup_key


def test_consensus_requires_high_signal_claim() -> None:
    state = _state("def f():\n    return 1\n")
    state.impact_manifest["publication_evidence"] = {"consensus_ids": ["finding-2"]}
    high_signal = _finding("wrong-return-value", "return contract mismatch", 2)
    style = _finding("style", "the name could be clearer", 2)
    style.id = "finding-2"

    assert protect_publication_finding(high_signal, state).protected is True
    assert protect_publication_finding(style, state).protected is False
