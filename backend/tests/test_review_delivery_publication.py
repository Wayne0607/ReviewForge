import json
import time

import httpx
import pytest

from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.publication import FindingPublisher
from reviewforge.review_delivery.store import DeliveryStore
from reviewforge.tools.github_api import GitHubAPIError, GitHubClient

HEAD, BASE = "a" * 40, "b" * 40


class Github:
    BASE_URL = "https://api.github.com"

    def __init__(self):
        self.head, self.comments, self.calls, self.fail = HEAD, [], [], None

    async def get_pr_info(self, repo, number):
        return {"number": number, "state": "open", "base": {"repo": {"full_name": repo}}, "head": {"sha": self.head}}

    async def list_delivery_issue_comments(self, repo, number):
        return list(self.comments)

    async def write_delivery_issue_comment(self, repo, number, body, *, remote_id=None):
        self.calls.append((remote_id, body))
        comment = {
            "id": remote_id or len(self.comments) + 10,
            "body": body,
            "user": {"id": 123},
            "issue_url": f"{self.BASE_URL}/repos/{repo}/issues/{number}",
        }
        if remote_id and self.fail != "absent":
            self.comments = [comment if c["id"] == remote_id else c for c in self.comments]
        elif self.fail != "absent":
            self.comments.append(comment)
        if self.fail:
            raise ConnectionError("response lost")
        return comment


def setup(tmp_path):
    store = DeliveryStore(tmp_path / "store.sqlite", Binding("run", "owner/repo", 3, BASE, HEAD, {}, {}))
    store.create_session("lead", "lead")
    store.create_session("verifier", "verifier")
    source = store.put_source("original", "head", b"code", {"path": "src/a.py", "revision": HEAD, "start_line": 4})
    store.record_delivery("verifier", [source["id"]])
    return store, source["id"]


def accepted(store, source, title):
    finding = store.create_finding({"title": title, "impact": "loses data"}, [source], "lead")
    task = store.create_verification(finding["id"], "verifier")
    store.record_verdict(task["id"], "accept", [source], reason="independently confirmed")
    return finding["id"]


async def test_two_independent_findings_and_blocked_finding(tmp_path):
    store, source = setup(tmp_path)
    a, b = accepted(store, source, "first"), accepted(store, source, "second")
    blocked = store.create_finding({"title": "uncertain"}, [source], "lead")
    task = store.create_verification(blocked["id"], "verifier")
    store.record_verdict(
        task["id"],
        "needs_evidence",
        [source],
        missing_fact="policy",
        attempted=["repo"],
        recovery_condition="policy supplied",
    )
    github = Github()
    publisher = FindingPublisher(store, github, 123)
    assert [r["status"] for r in await publisher.drain()] == ["delivered", "delivered"]
    assert len(github.comments) == 2
    assert "first" in github.comments[0]["body"] and "second" in github.comments[1]["body"]
    assert f"/blob/{HEAD}/src/a.py#L4" in github.comments[0]["body"]
    await publisher.publish(a)
    await publisher.publish(b)
    assert len(github.calls) == 2
    store.close()


async def test_lost_post_restart_and_missing_receipt_are_reconcile_only(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()
    github.fail = "lost"
    publisher = FindingPublisher(store, github, 123)
    with pytest.raises(ConnectionError):
        await publisher.publish(finding)
    store.close()
    store = DeliveryStore(tmp_path / "store.sqlite", Binding("run", "owner/repo", 3, BASE, HEAD, {}, {}))
    assert (await FindingPublisher(store, github, 123).publish(finding))["status"] == "delivered"
    assert len(github.calls) == 1
    store.close()
    other = tmp_path / "other"
    other.mkdir()
    store, source = setup(other)
    finding = accepted(store, source, "absent")
    github = Github()
    github.fail = "absent"
    publisher = FindingPublisher(store, github, 123)
    with pytest.raises(ConnectionError):
        await publisher.publish(finding)
    assert (await publisher.publish(finding))["status"] == "uncertain"
    assert len(github.calls) == 1
    with pytest.raises(ValueError, match="uncertain"):
        store.update_finding(finding, {"title": "new"}, [source])
    store.close()


async def test_copied_marker_update_and_changed_head(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()
    publisher = FindingPublisher(store, github, 123)
    marker = store.get_outbox(finding)["marker"]
    github.comments.append(
        {
            "id": 500,
            "body": marker,
            "user": {"id": 999},
            "issue_url": "https://api.github.com/repos/owner/repo/issues/3",
        }
    )
    result = await publisher.publish(finding)
    assert result["status"] == "delivered" and result["remote_id"] != 500
    original_id = result["remote_id"]
    store.update_finding(
        finding,
        {"title": "updated", "body": "The same defect has a clearer explanation."},
        [source],
        same_defect_reason="clarification",
    )
    task = store.create_verification(finding, "verifier")
    store.record_verdict(task["id"], "accept", [source], reason="independently confirmed")
    github.fail = "lost"
    with pytest.raises(ConnectionError):
        await publisher.publish(finding)
    assert github.calls[-1][0] == original_id
    assert (await publisher.publish(finding))["status"] == "delivered"
    assert len(github.calls) == 2
    store.close()
    other = tmp_path / "other"
    other.mkdir()
    store, source = setup(other)
    finding = accepted(store, source, "stale")
    github = Github()
    github.head = BASE
    result = await FindingPublisher(store, github, 123).publish(finding)
    assert result["status"] == "head_changed" and github.calls == []
    store.close()


async def test_corrupt_receipt_and_malformed_marker_never_repeat_post(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()
    publisher = FindingPublisher(store, github, 123)
    original = github.write_delivery_issue_comment

    async def corrupt(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        return {**receipt, "id": 0}

    github.write_delivery_issue_comment = corrupt
    with pytest.raises(Exception, match="receipt"):
        await publisher.publish(finding)
    assert store.get_outbox(finding)["status"] == "uncertain"
    assert (await publisher.publish(finding))["status"] == "delivered"
    assert len(github.calls) == 1
    store.close()

    other = tmp_path / "other"
    other.mkdir()
    store, source = setup(other)
    finding = accepted(store, source, "second")
    github = Github()
    github.comments.append({"id": 2, "body": store.get_outbox(finding)["marker"], "user": {}})
    with pytest.raises(Exception, match="invalid author"):
        await FindingPublisher(store, github, 123).publish(finding)
    assert not github.calls
    store.close()


async def test_head_change_during_write_keeps_receipt_and_reports_change(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()
    original = github.write_delivery_issue_comment

    async def write_then_change(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        github.head = BASE
        return receipt

    github.write_delivery_issue_comment = write_then_change
    result = await FindingPublisher(store, github, 123).publish(finding)
    assert result["status"] == "head_changed" and result["remote_id"] == github.comments[0]["id"]
    store.close()


async def test_unsupported_claim_is_not_partially_published(tmp_path):
    store, source = setup(tmp_path)
    finding = store.create_finding({"title": "defect", "body": "explanation", "more": ["important"]}, [source], "lead")
    task = store.create_verification(finding["id"], "verifier")
    store.record_verdict(task["id"], "accept", [source], reason="confirmed")
    github = Github()
    results = await FindingPublisher(store, github, 123).drain()
    assert results[0]["status"] == "intended" and "losing fields" in results[0]["error"]
    assert not github.calls
    store.close()


async def test_closed_pull_request_never_writes(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()

    async def closed(repo, number):
        return {"number": number, "state": "closed", "base": {"repo": {"full_name": repo}}, "head": {"sha": HEAD}}

    github.get_pr_info = closed
    result = await FindingPublisher(store, github, 123).drain()
    assert result[0]["status"] == "intended" and "not open" in result[0]["error"]
    assert store.get_outbox(finding)["remote_id"] is None and not github.calls
    store.close()


async def test_patch_outcome_unknown_never_blindly_retries(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()
    publisher = FindingPublisher(store, github, 123)
    old = await publisher.publish(finding)
    store.update_finding(finding, {"title": "first", "body": "clarified"}, [source], same_defect_reason="same issue")
    task = store.create_verification(finding, "verifier")
    store.record_verdict(task["id"], "accept", [source], reason="confirmed again")
    github.fail = "absent"
    with pytest.raises(ConnectionError):
        await publisher.publish(finding)
    result = await publisher.publish(finding)
    assert result["status"] == "uncertain" and result["remote_id"] == old["remote_id"]
    assert len(github.calls) == 2
    store.close()


@pytest.mark.parametrize("lost_patch_response", [False, True])
@pytest.mark.parametrize("closed_and_new_head", [False, True])
@pytest.mark.parametrize("verdict", ["reject", "needs_evidence"])
async def test_correction_patches_same_comment_with_http_transport(
    tmp_path, lost_patch_response, closed_and_new_head, verdict
):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    writes = []
    comments = []
    pr = {"number": 3, "state": "open", "base": {"repo": {"full_name": "owner/repo"}}, "head": {"sha": HEAD}}

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/pulls/3"):
            return httpx.Response(200, json=pr)
        if request.method == "GET" and request.url.path.endswith("/issues/3/comments"):
            return httpx.Response(200, json=comments)
        assert request.method in {"POST", "PATCH"}
        writes.append((request.method, request.url.path))
        body = json.loads(request.content)["body"]
        comment = {
            "id": 42,
            "body": body,
            "user": {"id": 123},
            "issue_url": "https://api.github.com/repos/owner/repo/issues/3",
        }
        comments[:] = [comment]
        if request.method == "PATCH" and lost_patch_response:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=comment)

    github = GitHubClient("test")
    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url=github.BASE_URL, transport=httpx.MockTransport(handler))
    publisher = FindingPublisher(store, github, 123)
    original = await publisher.publish(finding)
    original_body = comments[0]["body"]
    if closed_and_new_head:
        pr["state"] = "closed"
        pr["head"] = {"sha": BASE}
    store.challenge_finding(finding, "counterexample discovered", [source])
    verification = store.create_verification(finding, "verifier")
    await publisher.drain()  # Opening re-verification alone does not publish a notice.
    assert len(writes) == 1
    if verdict == "reject":
        store.record_verdict(verification["id"], "reject", [source], reason="The counterexample rules this out.")
    else:
        store.record_verdict(
            verification["id"],
            "needs_evidence",
            [source],
            missing_fact="The deployment policy",
            attempted=["repository docs", "configuration"],
            recovery_condition="Obtain the policy",
        )
    if lost_patch_response:
        with pytest.raises(GitHubAPIError, match="response lost"):
            await publisher.publish(finding)
        assert store.get_outbox(finding)["status"] == "uncertain"
    result = await publisher.publish(finding)
    assert result["status"] == "delivered" and result["remote_id"] == original["remote_id"] == 42
    assert writes == [
        ("POST", "/repos/owner/repo/issues/3/comments"),
        ("PATCH", "/repos/owner/repo/issues/comments/42"),
    ]
    assert len(comments) == 1
    if verdict == "reject":
        assert "已撤回" in comments[0]["body"] and "counterexample rules this out" in comments[0]["body"]
    else:
        assert "补充核验未完成" in comments[0]["body"] and "原判决暂不能作为当前结论" in comments[0]["body"]
        assert all(
            value in comments[0]["body"] for value in ("deployment policy", "repository docs", "Obtain the policy")
        )
    assert HEAD in comments[0]["body"]
    assert original_body in comments[0]["body"]
    await github.close()
    store.close()


@pytest.mark.parametrize("verdict", ["reject", "needs_evidence"])
async def test_unpublished_non_acceptance_never_creates_comment(tmp_path, verdict):
    store, source = setup(tmp_path)
    finding = store.create_finding({"title": "first", "body": "claim"}, [source], "lead")
    verification = store.create_verification(finding["id"], "verifier")
    if verdict == "reject":
        store.record_verdict(verification["id"], "reject", [source], reason="Not a defect")
    else:
        store.record_verdict(
            verification["id"],
            "needs_evidence",
            [source],
            missing_fact="policy",
            attempted=["repository"],
            recovery_condition="provide policy",
        )
    github = Github()
    publisher = FindingPublisher(store, github, 123)
    assert store.get_outbox(finding["id"]) is None
    assert await publisher.drain() == [] and not github.calls
    store.close()


async def test_retraction_does_not_overwrite_changed_remote_body(tmp_path):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    github = Github()
    publisher = FindingPublisher(store, github, 123)
    await publisher.publish(finding)
    store.challenge_finding(finding, "counterexample", [source])
    verification = store.create_verification(finding, "verifier")
    store.record_verdict(verification["id"], "reject", [source], reason="refuted")
    github.comments[0]["body"] = "changed\n\n" + store.get_outbox(finding)["marker"]
    with pytest.raises(GitHubAPIError, match="body changed"):
        await publisher.publish(finding)
    assert len(github.calls) == 1 and store.get_outbox(finding)["status"] == "intended"
    store.close()


@pytest.mark.parametrize("status", [422, 429])
@pytest.mark.parametrize("update", [False, True])
async def test_definite_http_rejection_is_recoverable_without_reverification(tmp_path, status, update):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    comments, writes = [], []
    reject_next = not update

    def handler(request):
        nonlocal reject_next
        if request.method == "GET" and request.url.path.endswith("/pulls/3"):
            return httpx.Response(
                200,
                json={
                    "number": 3,
                    "state": "open",
                    "base": {"repo": {"full_name": "owner/repo"}},
                    "head": {"sha": HEAD},
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json=comments)
        writes.append(request.method)
        if reject_next:
            reject_next = False
            headers = {"Retry-After": "120"} if status == 429 else {}
            return httpx.Response(status, headers=headers, json={"message": "rejected", "errors": ["blocked"]})
        body = json.loads(request.content)["body"]
        comment = {
            "id": 42,
            "body": body,
            "user": {"id": 123},
            "issue_url": "https://api.github.com/repos/owner/repo/issues/3",
        }
        comments[:] = [comment]
        return httpx.Response(200, json=comment)

    github = GitHubClient("test")
    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url=github.BASE_URL, transport=httpx.MockTransport(handler))
    publisher = FindingPublisher(store, github, 123)
    if update:
        await publisher.publish(finding)
        store.update_finding(
            finding, {"title": "first", "body": "clarified"}, [source], same_defect_reason="same defect"
        )
        task = store.create_verification(finding, "verifier")
        store.record_verdict(task["id"], "accept", [source], reason="confirmed")
        reject_next = True
    verdict_count = len(store.list_verdicts())
    with pytest.raises(GitHubAPIError) as exc:
        await publisher.publish(finding)
    assert exc.value.status_code == status
    current = store.get_outbox(finding)
    assert current["status"] == "intended"
    assert current["remote_id"] == (42 if update else None)
    if status == 429:
        assert current["retry_not_before"] >= time.time() + 110
        previous = list(writes)
        assert (await publisher.publish(finding))["status"] == "intended"
        assert writes == previous
        # Simulate a restart after the persisted cooldown has elapsed.
        store.db.execute(
            "UPDATE delivery_cooldowns SET retry_not_before=? WHERE finding_id=?", (time.time() - 1, finding)
        )
        store.close()
        store = DeliveryStore(tmp_path / "store.sqlite", Binding("run", "owner/repo", 3, BASE, HEAD, {}, {}))
        publisher = FindingPublisher(store, github, 123)
    assert (await publisher.publish(finding))["status"] == "delivered"
    assert len(store.list_verdicts()) == verdict_count and len(comments) == 1
    assert writes == (["POST", "PATCH", "PATCH"] if update else ["POST", "POST"])
    await github.close()
    store.close()


@pytest.mark.parametrize("status", [408, 500])
async def test_ambiguous_http_failure_never_reposts(tmp_path, status):
    store, source = setup(tmp_path)
    finding = accepted(store, source, "first")
    writes = []

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/pulls/3"):
            return httpx.Response(
                200,
                json={
                    "number": 3,
                    "state": "open",
                    "base": {"repo": {"full_name": "owner/repo"}},
                    "head": {"sha": HEAD},
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json=[])
        writes.append(request.method)
        return httpx.Response(status, json={"message": "unknown"})

    github = GitHubClient("test")
    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url=github.BASE_URL, transport=httpx.MockTransport(handler))
    publisher = FindingPublisher(store, github, 123)
    with pytest.raises(GitHubAPIError):
        await publisher.publish(finding)
    assert (await publisher.publish(finding))["status"] == "uncertain"
    assert writes == ["POST"]
    await github.close()
    store.close()
