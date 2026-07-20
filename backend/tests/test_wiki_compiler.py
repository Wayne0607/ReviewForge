"""Tests for the source-grounded repository Wiki Compiler."""

from __future__ import annotations

import pytest

from reviewforge.core.database import Database
from reviewforge.engine.symbol_extractor import extract_definitions
from reviewforge.engine.wiki_compiler import compile_symbol_pages, render_wiki_pages


def test_compile_symbol_page_keeps_contract_facts_and_source_anchors():
    content = """\
def refresh_token(user, cache):
    if user is None:
        raise ValueError("user required")
    cache.invalidate(user.id)
    return {"token": user.token}
"""
    pages = compile_symbol_pages(
        path="src/auth.py",
        language="python",
        content=content,
        changed_symbols=[
            {
                "name": "refresh_token",
                "type": "function",
                "line": 1,
                "start_line": 1,
                "end_line": 5,
            }
        ],
        source_sha="abc123",
    )

    assert len(pages) == 1
    page = pages[0]
    assert page.title == "refresh_token"
    assert page.source_path == "src/auth.py"
    assert page.source_sha == "abc123"
    assert {fact["kind"] for fact in page.content["facts"]} >= {
        "signature",
        "guard",
        "return-or-error",
        "side-effect",
    }
    assert len(page.content_hash) == 64


def test_compile_reference_page_records_focused_call_contract():
    content = """\
def dispatch(user):
    return authorize(user)
"""
    pages = compile_symbol_pages(
        path="src/caller.py",
        language="python",
        content=content,
        changed_symbols=[{"name": "dispatch", "type": "function", "line": 1, "start_line": 1, "end_line": 2}],
        source_sha="head",
        focus_terms=["authorize"],
    )

    assert any(
        fact["kind"] == "related-call" and "authorize(user)" in fact["evidence"] for fact in pages[0].content["facts"]
    )


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        (
            "src/Auth.java",
            "public Token refresh(User user) {\n  if (user == null) throw new Error();\n  return user.token();\n}\n",
            "refresh",
        ),
        (
            "src/auth.ts",
            "export async function refresh(user: User) {\n  if (!user) throw Error();\n  return await load(user);\n}\n",
            "refresh",
        ),
        (
            "src/auth.go",
            'func Refresh(user *User) Token {\n  if user == nil { panic("user") }\n  return user.Token\n}\n',
            "Refresh",
        ),
        (
            "src/auth.rb",
            "def refresh(user)\n  return nil unless user\n  save(user)\nend\n",
            "refresh",
        ),
    ],
)
def test_compiler_uses_multilanguage_symbol_ranges(path, content, symbol):
    definitions = extract_definitions(content, path)
    changed = [
        {
            "name": item.name,
            "type": item.symbol_type,
            "line": item.line,
            "start_line": item.start_line,
            "end_line": item.end_line,
        }
        for item in definitions
        if item.name == symbol
    ]

    pages = compile_symbol_pages(
        path=path,
        language=path.rsplit(".", 1)[-1],
        content=content,
        changed_symbols=changed,
        source_sha="sha",
    )

    assert pages and pages[0].title == symbol
    assert any(fact["kind"] == "return-or-error" for fact in pages[0].content["facts"])


async def test_wiki_storage_is_repo_scoped_and_retrieval_is_ranked(tmp_path):
    db = Database(tmp_path / "wiki.db")
    await db.connect()
    try:
        content = {"facts": [{"kind": "signature", "line": 1, "evidence": "def authorize(user):"}]}
        common = {
            "page_key": "symbol:src/auth.py:authorize",
            "kind": "symbol-contract",
            "title": "authorize",
            "content": content,
            "search_terms": ["authorize", "permission"],
            "source_path": "src/auth.py",
            "source_sha": "sha",
            "source_start": 1,
            "source_end": 3,
            "content_hash": "hash",
        }
        await db.upsert_wiki_page(repo="one/repo", **common)
        await db.upsert_wiki_page(
            repo="one/repo",
            **{**common, "source_sha": "other-sha", "content_hash": "other-hash"},
        )
        await db.upsert_wiki_page(repo="other/repo", **common)

        rows = await db.search_wiki_pages("one/repo", ["authorize"], limit=5, source_sha="sha")

        assert len(rows) == 1
        assert {row["repo"] for row in rows} == {"one/repo"}
        assert rows[0]["source_sha"] == "sha"
        assert rows[0]["content"] == content
        assert rows[0]["retrieval_score"] >= 12
        other_revision = await db.search_wiki_pages(
            "one/repo",
            ["authorize"],
            limit=5,
            source_sha="other-sha",
        )
        assert other_revision[0]["source_sha"] == "other-sha"
    finally:
        await db.close()


def test_render_wiki_pages_respects_budget():
    pages = [
        {
            "title": f"symbol_{index}",
            "kind": "symbol-contract",
            "source_path": f"src/{index}.py",
            "source_sha": "sha",
            "source_start": 1,
            "source_end": 2,
            "content": {"facts": [{"kind": "signature", "line": 1, "evidence": "x" * 100}]},
        }
        for index in range(20)
    ]

    rendered = render_wiki_pages(pages, max_chars=500)

    assert 0 < len(rendered) < len(pages)
