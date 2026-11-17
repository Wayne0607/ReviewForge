import hashlib
import json
from types import SimpleNamespace

import pytest

from reviewforge.core.state import StateStore
from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.evidence import check_text, manifest_digest
from reviewforge.review_delivery.runtime import DeliveryRuntime
from reviewforge.review_delivery.store import DeliveryStore
from reviewforge.review_v42.adapter import PinnedToolGateway


class Snapshot:
    source = "tarball"
    truncated = False
    head_sha = "head"

    def __init__(self, files):
        self.files = files

    def list_paths(self):
        return list(self.files)

    def read_bytes(self, path):
        return self.files[path]


def gateway(files):
    return PinnedToolGateway(
        Snapshot(files),
        StateStore(repo="org/repo", head_sha="head"),
        run=SimpleNamespace(metadata={"source_line_policy": "git-lf-1"}),
    )


def setup(tmp_path, files):
    store = DeliveryStore(
        tmp_path / "review.db",
        Binding(
            "run",
            "org/repo",
            1,
            "base",
            "head",
            {"head": manifest_digest({p: hashlib.sha256(b).hexdigest() for p, b in files.items()})},
            {},
        ),
    )
    lead, verifier = store.create_session("lead"), store.create_session("verifier")
    source = store.put_source("original", "head", b"trigger", {"revision": "head", "path": "trigger.py"})
    finding = store.create_finding({"title": "Missing catalog key"}, [source["id"]], lead["id"])
    task = store.create_verification(finding["id"], verifier["id"])
    runtime = DeliveryRuntime(store, gateway(files), None, None)
    return store, runtime, finding, task


async def invoke(runtime, task, name, args, invocation):
    return await runtime._tool("verifier", task["session_id"], task["id"], name, args, invocation)


def verdict(proof, trigger=None):
    return {
        "verdict": "accept",
        "reason": "Authoritative catalog has no key",
        "source_ids": [proof, *([trigger] if trigger else [])],
        "absence_claims": [{"fact": "key absent", "scope": {"side": "head", "glob": "*"}, "source_ids": [proof]}],
    }


async def test_literal_check_finds_key_outside_excerpt_and_inside_search_skipped_json():
    tool = gateway({"locales/en/common.json": b'{\n"first": 1,\n"target": 2\n}\n'})
    partial = await tool("read_source", {"path": "locales/en/common.json", "end_line": 2})
    assert "target" not in partial["content"]
    checked = await check_text(tool, {"glob": "locales/en/*.json", "literal": '"target"'})
    proof = json.loads(checked["content"])
    assert proof["complete"] and proof["matches"] == [{"path": "locales/en/common.json", "line": 3}]
    assert proof["files"][0]["sha256"]


async def test_narrow_search_miss_is_not_absence_in_the_whole_canonical_directory():
    tool = gateway({"en/common.json": b"{}\n", "en/extra.json": b'{"target": "present"}\n'})
    searched = await tool("search_code", {"query": "target", "glob": "en/common.json"})
    assert json.loads(searched["content"])["matches"] == []
    checked = json.loads((await check_text(tool, {"glob": "en/*.json", "literal": '"target"'}))["content"])
    assert len(checked["files"]) == 2 and checked["matches"][0]["path"] == "en/extra.json"


@pytest.mark.parametrize("files,pattern", [({"a.bin": b"\x00binary"}, "*"), ({"a.txt": b"ok"}, "missing/*")])
async def test_unreadable_and_empty_scopes_are_not_absence_proofs(tmp_path, files, pattern):
    store, runtime, finding, task = setup(tmp_path, files)
    result, ids, _ = await invoke(runtime, task, "check_text", {"glob": pattern, "literal": "target"}, "check")
    store.record_delivery(task["session_id"], ids)
    assert not json.loads(result["content"])["complete"]
    with pytest.raises(ValueError, match="incomplete"):
        await invoke(runtime, task, "submit_verdict", verdict(ids[0]), "judge")
    assert not store.list_verdicts() and not store.list_outbox()
    assert store.get_finding(finding["id"])["revision"] == 1


async def test_partial_original_rejected_then_same_finding_accepts_complete_chunks_and_restarts(tmp_path):
    store, runtime, finding, task = setup(tmp_path, {"en.json": b"first\nsecond\nlast\n"})
    _, first, _ = await invoke(runtime, task, "read_source", {"path": "en.json", "end_line": 1}, "first")
    store.record_delivery(task["session_id"], first)
    with pytest.raises(ValueError, match="scope is incomplete"):
        await invoke(runtime, task, "submit_verdict", verdict(first[0]), "partial")
    assert not store.list_outbox()
    _, remaining, _ = await invoke(runtime, task, "read_source", {"path": "en.json", "start_line": 2}, "rest")
    store.record_delivery(task["session_id"], remaining)
    args = verdict(first[0])
    args["source_ids"] += remaining
    args["absence_claims"][0]["source_ids"] += remaining
    await invoke(runtime, task, "submit_verdict", args, "complete")
    assert store.get_finding(finding["id"])["revision"] == finding["revision"]
    assert len(store.list_verdicts()) == len(store.list_outbox()) == 1
    store.close()
    reopened = DeliveryStore.reopen(tmp_path / "review.db")
    assert reopened.list_verdicts()[0]["absence_claims"] == args["absence_claims"]
    same = reopened.record_verdict(
        task["id"], "accept", args["source_ids"], reason=args["reason"], absence_claims=args["absence_claims"]
    )
    assert same["id"] == reopened.list_verdicts()[0]["id"]
    assert len(reopened.list_outbox()) == 1
    reopened.close()


async def test_closed_check_requires_own_delivery_and_zero_matches(tmp_path):
    store, runtime, _, task = setup(tmp_path, {"en.json": b'"target": 1\n'})
    _, found, _ = await invoke(runtime, task, "check_text", {"glob": "*.json", "literal": "target"}, "found")
    with pytest.raises(ValueError, match="not delivered"):
        await invoke(runtime, task, "submit_verdict", verdict(found[0]), "unread")
    store.record_delivery(task["session_id"], found)
    with pytest.raises(ValueError, match="scope is incomplete"):
        await invoke(runtime, task, "submit_verdict", verdict(found[0]), "wrong")
    _, absent, _ = await invoke(runtime, task, "check_text", {"glob": "*.json", "literal": "missing_key"}, "absent")
    store.record_delivery(task["session_id"], absent)
    await invoke(runtime, task, "submit_verdict", verdict(absent[0]), "right")
    assert len(store.list_verdicts()) == 1


async def test_incomplete_navigation_cannot_prove_absence_or_block_unrelated_positive_claim(tmp_path):
    store, runtime, _, task = setup(tmp_path, {"code.py": b"unsafe()\n"})
    _, source, _ = await invoke(runtime, task, "read_source", {"path": "code.py"}, "original")
    nav = store.put_source("navigation", "head", b"no results", {"complete": False})
    store.record_delivery(task["session_id"], [*source, nav["id"]])
    args = verdict(nav["id"])
    args["source_ids"] = source
    with pytest.raises(ValueError, match="cited"):
        await invoke(runtime, task, "submit_verdict", args, "nav")
    await invoke(
        runtime,
        task,
        "submit_verdict",
        {"verdict": "accept", "source_ids": source, "reason": "unsafe call present", "absence_claims": []},
        "positive",
    )
    assert len(store.list_outbox()) == 1


async def test_verdict_requires_explicit_negative_premise_declaration(tmp_path):
    store, runtime, _, task = setup(tmp_path, {"code.py": b"unsafe()\n"})
    with pytest.raises(ValueError, match="absence_claims"):
        await invoke(
            runtime, task, "submit_verdict", {"verdict": "accept", "source_ids": [], "reason": "assumed"}, "missing"
        )
    assert not store.list_verdicts() and not store.list_outbox()


async def test_full_single_file_cannot_close_a_multi_file_scope(tmp_path):
    store, runtime, finding, task = setup(tmp_path, {"a.json": b"{}\n", "b.json": b"{}\n"})
    _, first, _ = await invoke(runtime, task, "read_source", {"path": "a.json"}, "first")
    store.record_delivery(task["session_id"], first)
    with pytest.raises(ValueError, match="b.json"):
        await invoke(runtime, task, "submit_verdict", verdict(first[0]), "incomplete")
    assert not store.list_verdicts() and not store.list_outbox()
    _, second, _ = await invoke(runtime, task, "read_source", {"path": "b.json"}, "second")
    store.record_delivery(task["session_id"], second)
    args = verdict(first[0])
    args["source_ids"] += second
    args["absence_claims"][0]["source_ids"] += second
    await invoke(runtime, task, "submit_verdict", args, "closed")
    assert store.get_finding(finding["id"])["revision"] == 1
    assert len(store.list_outbox()) == 1


def test_scope_membership_is_bound_and_cannot_be_replaced(tmp_path):
    store, _, _, _ = setup(tmp_path, {"a.py": b"a\n", "b.py": b"b\n"})
    with pytest.raises(ValueError, match="frozen snapshot"):
        store.register_snapshot("head", {"a.py": hashlib.sha256(b"a\n").hexdigest()})
    files = {"a.py": hashlib.sha256(b"a\n").hexdigest(), "b.py": hashlib.sha256(b"b\n").hexdigest()}
    store.register_snapshot("head", files)
    store.db.execute("UPDATE snapshot_manifests SET files=? WHERE side='head'", (json.dumps({"a.py": files["a.py"]}),))
    with pytest.raises(ValueError, match="frozen snapshot"):
        store.snapshot_manifest("head")
