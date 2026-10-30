from __future__ import annotations

import asyncio

import pytest

from reviewforge.engine.investigation_evidence import EvidenceScope, SharedEvidenceCache


@pytest.mark.asyncio
async def test_cache_deduplicates_inflight_success_without_sharing_verdicts():
    scope = EvidenceScope("run", "sha", "digest")
    cache = SharedEvidenceCache(scope)
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(name, args):
        calls.append((name, args))
        entered.set()
        await release.wait()
        return "return calendar?.id"

    first = asyncio.create_task(cache.execute("read_file", {"path": "a.ts"}, execute, scope=scope))
    await entered.wait()
    second = asyncio.create_task(cache.execute("read_file", {"path": "a.ts"}, execute, scope=scope))
    await asyncio.sleep(0)
    release.set()
    left, right = await asyncio.gather(first, second)
    cached = await cache.execute("read_file", {"path": "a.ts"}, execute, scope=scope)
    assert len(calls) == 1
    assert left.shared_result_id == right.shared_result_id == cached.shared_result_id
    assert not left.reused and right.reused and cached.reused
    assert cache.entry_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "No results", "No callers found"])
async def test_negative_reads_are_not_retained_as_shared_evidence(response):
    scope = EvidenceScope("run", "sha", "digest")
    cache = SharedEvidenceCache(scope)
    calls = []

    async def execute(name, args):
        calls.append(name)
        return response

    for _ in range(2):
        result = await cache.execute("find_callers", {"symbol": "x"}, execute, scope=scope)
        assert result.shared_result_id == ""
    assert len(calls) == 2 and cache.entry_count == 0


@pytest.mark.asyncio
async def test_failed_read_can_be_attempted_later_and_scope_cannot_leak():
    scope = EvidenceScope("run", "sha", "digest")
    cache = SharedEvidenceCache(scope)
    calls = []

    async def execute(name, args):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("failure")
        return "source"

    with pytest.raises(RuntimeError):
        await cache.execute("read_file", {"path": "a"}, execute, scope=scope)
    assert cache.entry_count == 0
    assert (await cache.execute("read_file", {"path": "a"}, execute, scope=scope)).shared_result_id
    for wrong in (EvidenceScope("other", "sha", "digest"), EvidenceScope("run", "other", "digest")):
        with pytest.raises(ValueError, match="scope"):
            await cache.execute("read_file", {"path": "a"}, execute, scope=wrong)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_limits_retention_but_allows_reads_and_never_accepts_mutation():
    scope = EvidenceScope("run", "sha", "digest")
    cache = SharedEvidenceCache(scope, max_entries=1, max_bytes=100)
    calls = []

    async def execute(name, args):
        calls.append(args)
        return "source"

    for path in ("a", "b", "b"):
        await cache.execute("read_file", {"path": path}, execute, scope=scope)
    assert len(calls) == 3
    assert cache.entry_count == 1 and 6 < cache.byte_count <= 100
    with pytest.raises(ValueError, match="read-only"):
        await cache.execute("write_file", {}, execute, scope=scope)


@pytest.mark.asyncio
async def test_full_result_policy_is_independent_of_retention_and_preserves_old_default():
    scope = EvidenceScope("run", "sha", "digest")
    text = "source\n" * 2000

    async def execute(_name, _args):
        return text

    old = SharedEvidenceCache(scope)
    full = SharedEvidenceCache(scope, max_bytes=1, max_result_chars=None)
    assert (await old.execute("read_file", {}, execute, scope=scope)).text == text[:6000]
    assert (await full.execute("read_file", {}, execute, scope=scope)).text == text
    assert full.entry_count == full.byte_count == 0
    with pytest.raises(ValueError, match="scope"):
        await full.execute("read_file", {}, execute, scope=EvidenceScope("run", "sha", "other"))
    await old.close()
    await full.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_reads_and_releases_retained_results():
    scope = EvidenceScope("run", "sha", "digest")
    cache = SharedEvidenceCache(scope, max_result_chars=None)
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def execute(_name, _args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    waiter = asyncio.create_task(cache.execute("read_file", {}, execute, scope=scope))
    await entered.wait()
    await cache.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert stopped.is_set() and cache.entry_count == cache.byte_count == 0
