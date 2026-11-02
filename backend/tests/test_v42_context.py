from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import Case, Question, ReviewScope, Run
from reviewforge.review_v42.store import ReviewStore


def test_unrelated_history_does_not_expand_working_set(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run("r", "repo", "base", "head"))
    store.add(ReviewScope("s", "r", "scope"))
    store.add(Question("q", "r", "question", scope_id="s"))
    builder = ContextBuilder(store)
    before = builder.reviewer("s")
    with store._connect() as db:
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO events(kind,object_id,action,payload) VALUES ('Noise','other','noise','{}')", [()] * 10000
        )
        db.commit()
    assert builder.reviewer("s") == before
    assert len(store.history("Noise")) == 10000


def test_verifier_gets_raw_sources_without_reviewer_verdict(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run("r", "repo", "base", "head"))
    text = store.put_evidence(b"actual source", revision="head", side="head", kind="source")
    binary = store.put_evidence(b"\xff", revision="base", side="base", kind="source")
    store.add(
        Case(
            "c",
            "r",
            trigger="input",
            mechanism="operation",
            claims=[
                {
                    "id": "a",
                    "statement": "result",
                    "impact": "effect",
                }
            ],
            evidence_refs=[text.id],
            counter_evidence_refs=[binary.id],
        )
    )
    context = ContextBuilder(store).verifier("c")
    assert context["claims"] == [{"id": "a", "statement": "result", "impact": "effect"}]
    assert context["evidence"][0]["content"] == "actual source"
    assert context["counter_evidence"][0]["status"] == "unavailable_non_utf8"
    assert context["counter_evidence"][0]["content"] is None


def test_reviewer_pr_context_is_an_explicit_metadata_whitelist(tmp_path):
    metadata = {
        "title": "PR title",
        "description": "PR intent",
        "output_language": "zh-CN",
        "secret": "not model input",
        "changed_regions": [{"path": "unrelated"}],
    }
    store = ReviewStore(tmp_path / "state.db", Run("r", "repo", "base", "head", metadata=metadata))
    store.add(ReviewScope("s", "r", "scope"))
    context = ContextBuilder(store).reviewer("s")
    assert context["pr"] == {"title": "PR title", "description": "PR intent", "output_language": "zh-CN"}
    assert "secret" not in str(context)
    assert "changed_regions" not in context["pr"]
