import json
from types import SimpleNamespace

import pytest

from reviewforge.review_v42.adapter import PinnedToolGateway, changed_regions


def state(patches):
    return SimpleNamespace(
        repo="a/b", head_sha="head", base_sha="base", linked_issues=[], files_changed=list(patches), file_diffs=patches
    )


class Workspace:
    head_sha = "head"
    digest = "pinned"
    source = "tarball"
    truncated = False

    def __init__(self, content):
        self.content = content

    async def read_async(self, path):
        return self.content.get(path)

    def read_bytes(self, path):
        return self.content[path].encode("utf-8")

    def list_paths(self):
        return list(self.content)


def test_hunk_manifest_marks_truncation_and_keeps_deleted_changes():
    rows = changed_regions(
        state(
            {
                "a.py": "@@ -1,2 +1,2 @@\n a\n-b\n+c",
                "deleted.py": "@@ -1 +0,0 @@\n-old",
                "truncated.py": "@@ -1,20 +1,20 @@\n a",
                "binary": "",
            }
        )
    )
    assert rows[0]["changed_lines"] == [2] and not rows[0]["unsupported"]
    assert rows[1]["changed_lines"] == [] and not rows[1]["unsupported"]
    assert rows[2]["unsupported"] and rows[3]["unsupported"]
    assert len(rows) == 4


async def test_sources_are_exact_and_base_never_becomes_head():
    async def base(*_):
        return b"old\nbase\n"

    gateway = PinnedToolGateway(Workspace({"a.py": "new\nhead\n"}), state({}), SimpleNamespace(get_file_bytes=base))
    head = await gateway("read_source", {"path": "a.py"})
    before = await gateway("read_source", {"path": "a.py", "side": "base", "start_line": 2, "end_line": 2})
    assert head["content"] == "new\nhead\n" and head["revision"] == "head"
    assert before["content"] == "base\n" and before["revision"] == "base" and before["side"] == "base"
    with pytest.raises(ValueError):
        await gateway("read_source", {"path": "../secret"})
    with pytest.raises(ValueError):
        await gateway("read_source", {"path": "a.py", "end_line": 99})
    assert (await gateway("read_source", {"path": "missing"}))["metadata"]["status"] == "unavailable"


async def test_search_pagination_is_navigation_and_full_results_remain_accessible():
    gateway = PinnedToolGateway(Workspace({"a.py": "token\n" * 125, "binary": "\x00token"}), state({}))
    first = await gateway("search_code", {"query": "token"})
    payload = json.loads(first["content"])
    assert len(payload["matches"]) == 100 and payload["next_offset"] == 100
    last = await gateway("search_code", {"query": "token", "offset": 100})
    assert len(json.loads(last["content"])["matches"]) == 25
    assert last["kind"] == "search" and last["metadata"]["navigation_only"]
    assert not last["metadata"]["complete"]  # The unreadable file remains explicit.


async def test_external_issue_keeps_its_own_revision_and_capture_time():
    s = state({})
    s.linked_issues = [
        {
            "html_url": "https://github.com/a/b/issues/2",
            "updated_at": "2026-10-10T00:00:00Z",
            "title": "Contract",
            "body": "Required behavior",
        }
    ]
    s.linked_issue_context = {"captured_at": "2026-11-02T00:00:00Z", "issue_captures": {"2": "2026-10-11T00:00:00Z"}}
    gateway = PinnedToolGateway(Workspace({"a.py": "pass\r\n"}), s)
    result = await gateway("read_knowledge", {"path": "issue:https://github.com/a/b/issues/2"})
    assert result["side"] == "external" and result["revision"] == "2026-10-10T00:00:00Z"
    assert result["metadata"]["captured_at"] == "2026-10-11T00:00:00Z"
    assert result["metadata"]["source_url"] == "https://github.com/a/b/issues/2"
    assert "content" not in result["metadata"]["original_receipt"]
