"""GitHub API client — async wrapper around GitHub REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from reviewforge.core.change_manifest import valid_path, valid_repo, valid_sha

logger = logging.getLogger(__name__)

MAX_REVIEW_COMMENTS_PER_REQUEST = 40
_REVIEW_MAX_ATTEMPTS = 3
_REVIEW_MAX_RETRY_DELAY = 60.0
_REVIEW_WRITE_LOCK = asyncio.Lock()


class GitHubAPIError(RuntimeError):
    """Structured GitHub write failure used for retry and delivery decisions."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        kind: str = "http",
        retryable: bool = False,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.retryable = retryable
        self.response_body = response_body


def _encode_path(file_path: str) -> str:
    """URL-encode each segment of a file path."""
    return "/".join(quote(seg, safe="") for seg in file_path.split("/"))


class GitHubClient:
    """Async GitHub REST API client."""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        self._sleep = asyncio.sleep

    async def close(self) -> None:
        await self._client.aclose()

    async def _list_pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """B9: Paginated PR file listing."""
        files, page = [], 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo}/pulls/{int(pr_number)}/files",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    async def get_file_diff(self, repo: str, pr_number: int, file_path: str) -> str:
        """Get the diff for a specific file in a PR."""
        files = await self._list_pr_files(repo, pr_number)
        for f in files:
            # B9: 精确匹配，不用 endswith
            if f.get("filename") == file_path:
                return f.get("patch", "")
        return f"No diff found for {file_path}"

    async def get_file_content(self, repo: str, ref: str, file_path: str) -> str:
        """Get file content at a specific ref."""
        resp = await self._client.get(
            f"/repos/{repo}/contents/{_encode_path(file_path)}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.v3.raw"},
        )
        resp.raise_for_status()
        return resp.text

    async def search_code(self, repo: str, pattern: str, file_glob: str = "") -> str:
        """Search code in a repository."""
        query = f"{pattern} repo:{repo}"
        if file_glob:
            query += f" path:{file_glob}"
        resp = await self._client.get("/search/code", params={"q": query, "per_page": 10})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        return "\n".join(f"- {item['path']}" for item in items) or "No results"

    async def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        file_path: str,
        line: int,
        body: str,
    ) -> dict[str, Any]:
        """Post one inline comment (compatibility path)."""
        payload = {
            "body": body,
            "commit_id": commit_sha,
            "path": file_path,
            "line": int(line),
            "side": "RIGHT",
        }
        async with _REVIEW_WRITE_LOCK:
            return await self._post_json_with_retry(
                f"/repos/{repo}/pulls/{int(pr_number)}/comments",
                payload,
                operation="create review comment",
            )

    async def post_review_comments(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        comments: list[dict[str, Any]],
        body: str = "",
        delivery_key: str = "",
        reconcile_only: bool = False,
    ) -> dict[str, Any]:
        """Create one COMMENT review carrying a bounded set of inline comments.

        ``body`` is the optional Markdown review summary rendered into the PR
        review ``<details>``; when only a body is supplied a body-only review is
        created.
        """

        if not comments and not body:
            raise ValueError("A review requires at least one inline comment or a body")
        if len(comments) > MAX_REVIEW_COMMENTS_PER_REQUEST:
            raise ValueError(f"Review comment batch exceeds {MAX_REVIEW_COMMENTS_PER_REQUEST}: {len(comments)}")

        review_comments = [
            {
                "path": str(item.get("path", item.get("file_path", ""))),
                "line": int(item["line"]),
                "side": "RIGHT",
                "body": str(item["body"]),
            }
            for item in comments
        ]
        payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "event": "COMMENT",
        }
        if body:
            payload["body"] = body
        if review_comments:
            payload["comments"] = review_comments
        if delivery_key:
            marker = f"<!-- reviewforge:v4:{delivery_key} -->"
            payload["body"] = f"{body}\n\n{marker}"
            async with _REVIEW_WRITE_LOCK:
                existing = await self.find_submitted_review(repo, pr_number, commit_sha, marker)
                if existing is not None:
                    return existing
                if reconcile_only:
                    raise GitHubAPIError("Review receipt not yet found", kind="delivery_uncertain", retryable=True)
                try:
                    response = await self._client.post(f"/repos/{repo}/pulls/{int(pr_number)}/reviews", json=payload)
                except httpx.TransportError as exc:
                    # Never repeat an ambiguous POST. Recovery performs GETs
                    # using the durable marker; absence is not proof of failure.
                    raise GitHubAPIError("Review response lost", kind="network", retryable=True) from exc
                if not response.is_success:
                    raise _classify_write_error("create pull request review", response)
                try:
                    receipt = response.json()
                except ValueError as exc:
                    raise GitHubAPIError("Invalid review receipt", kind="delivery_uncertain", retryable=True) from exc
                if not isinstance(receipt, dict) or type(receipt.get("id")) is not int or receipt["id"] <= 0:
                    raise GitHubAPIError("Missing review receipt", kind="delivery_uncertain", retryable=True)
                return receipt
        async with _REVIEW_WRITE_LOCK:
            return await self._post_json_with_retry(
                f"/repos/{repo}/pulls/{int(pr_number)}/reviews",
                payload,
                operation="create pull request review",
            )

    async def find_submitted_review(
        self, repo: str, pr_number: int, commit_sha: str, marker: str
    ) -> dict[str, Any] | None:
        page = 1
        while True:
            response = await self._client.get(
                f"/repos/{repo}/pulls/{int(pr_number)}/reviews", params={"per_page": 100, "page": page}
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubAPIError("Invalid review listing", kind="delivery_uncertain", retryable=True)
            for review in batch:
                if (
                    isinstance(review, dict)
                    and review.get("commit_id") == commit_sha
                    and review.get("state") == "COMMENTED"
                    and review.get("submitted_at")
                    and marker in str(review.get("body") or "")
                    and type(review.get("id")) is int
                    and review["id"] > 0
                ):
                    return review
            if len(batch) < 100:
                return None
            page += 1

    async def _post_json_with_retry(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        """POST with bounded retry for rate limits, spam validation, and transport failures."""

        last_error: GitHubAPIError | None = None
        for attempt in range(_REVIEW_MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                response = await self._client.post(url, json=payload)
            except httpx.TransportError as exc:
                last_error = GitHubAPIError(
                    f"GitHub {operation} transport failure: {exc}",
                    kind="network",
                    retryable=True,
                )
            else:
                if response.is_success:
                    if not response.content:
                        return {}
                    try:
                        data = response.json()
                    except json.JSONDecodeError:
                        return {"raw": response.text}
                    return data if isinstance(data, dict) else {"data": data}
                last_error = _classify_write_error(operation, response)

            if not last_error.retryable or attempt + 1 >= _REVIEW_MAX_ATTEMPTS:
                raise last_error

            delay = _retry_delay(response, attempt)
            logger.warning(
                "GitHub %s retry %d/%d after %s (status=%d, delay=%.1fs)",
                operation,
                attempt + 1,
                _REVIEW_MAX_ATTEMPTS - 1,
                last_error.kind,
                last_error.status_code,
                delay,
            )
            await self._sleep(delay)

        raise last_error or GitHubAPIError(f"GitHub {operation} failed")

    async def get_pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get list of files changed in a PR (paginated)."""
        return await self._list_pr_files(repo, pr_number)

    async def get_pr_info(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Get PR metadata."""
        resp = await self._client.get(f"/repos/{repo}/pulls/{int(pr_number)}")
        resp.raise_for_status()
        return resp.json()

    async def get_comparison_base(self, repo: str, base_sha: str, head_repo: str, head_sha: str) -> str:
        """Resolve the merge-base from fixed SHAs, including fork-network heads."""
        if not valid_repo(repo) or not valid_repo(head_repo) or not valid_sha(base_sha) or not valid_sha(head_sha):
            raise ValueError("invalid comparison identity")
        head = head_sha if head_repo == repo else f"{head_repo.split('/')[0]}:{head_sha}"
        response = await self._client.get(
            f"/repos/{repo}/compare/{base_sha}...{quote(head, safe=':')}", params={"per_page": 1}
        )
        response.raise_for_status()
        data = response.json()
        merge_base = data.get("merge_base_commit", {}).get("sha", "")
        if data.get("base_commit", {}).get("sha") != base_sha or not valid_sha(merge_base):
            raise ValueError("comparison identity mismatch")
        return merge_base

    async def get_git_entries(self, repo: str, commit_sha: str, paths: list[str]) -> dict[str, dict[str, str]]:
        """Read exact Git entries with bounded complete-tree navigation.

        A truncated recursive tree is navigation only. Targeted nonrecursive
        subtree reads must be complete; missing entries never prove a rename.
        No cache outlives this lookup and no blob body needs downloading.
        """
        paths = list(dict.fromkeys(paths))
        if (
            not valid_repo(repo)
            or not valid_sha(commit_sha)
            or len(paths) > 16
            or any(not valid_path(path) or len(path.split("/")) > 32 for path in paths)
        ):
            raise ValueError("invalid or excessive Git entry lookup")
        if not paths:
            return {}
        commit = await self._client.get(f"/repos/{repo}/git/commits/{commit_sha}")
        commit.raise_for_status()
        data = commit.json()
        tree_sha = data.get("tree", {}).get("sha", "")
        if data.get("sha") != commit_sha or not valid_sha(tree_sha):
            raise ValueError("Git commit identity mismatch")
        requests = 0
        cache: dict[str, list[dict[str, Any]]] = {}

        async def tree(sha: str, *, recursive: bool = False) -> tuple[list[dict[str, Any]], bool, int]:
            nonlocal requests
            if requests >= 64 or not valid_sha(sha):
                raise ValueError("Git tree lookup limit")
            requests += 1
            response = await self._client.get(
                f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"} if recursive else None
            )
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("Git tree response limit")
            result = response.json()
            entries = result.get("tree")
            if result.get("sha") != sha or not isinstance(entries, list) or len(entries) > 100000:
                raise ValueError("Git tree identity mismatch")
            truncated = result.get("truncated")
            if not isinstance(truncated, bool) or (truncated and not recursive):
                raise ValueError("incomplete Git tree")
            return entries, truncated, len(response.content)

        entries, truncated, _ = await tree(tree_sha, recursive=True)
        found: dict[str, dict[str, Any]] = {}
        if not truncated:
            wanted = set(paths)
            found = {
                entry["path"]: entry for entry in entries if isinstance(entry, dict) and entry.get("path") in wanted
            }
        else:
            # Discard all truncated-tree candidates; each returned fact comes
            # from a complete, SHA-validated exact-path traversal.
            del entries
            cached_bytes = 0
            for path in paths:
                current_sha = tree_sha
                parts = path.split("/")
                for index, part in enumerate(parts):
                    if current_sha in cache:
                        subtree = cache[current_sha]
                    else:
                        subtree, _, size = await tree(current_sha)
                        if cached_bytes + size <= 4 * 1024 * 1024:
                            cache[current_sha] = subtree
                            cached_bytes += size
                    candidates = [entry for entry in subtree if isinstance(entry, dict) and entry.get("path") == part]
                    if len(candidates) != 1:
                        break
                    entry = candidates[0]
                    if index == len(parts) - 1:
                        found[path] = entry
                    elif entry.get("type") == "tree" and valid_sha(entry.get("sha")):
                        current_sha = entry["sha"]
                    else:
                        break
        return {
            path: {"sha": entry["sha"], "mode": entry["mode"], "type": entry["type"]}
            for path, entry in found.items()
            if valid_sha(entry.get("sha")) and isinstance(entry.get("mode"), str) and isinstance(entry.get("type"), str)
        }


def _classify_write_error(operation: str, response: httpx.Response) -> GitHubAPIError:
    """Classify 422 validation separately from spam/secondary-rate responses."""

    status = response.status_code
    body = response.text[:4000]
    lowered = body.lower()
    spam_markers = (
        "spam",
        "abuse",
        "secondary rate limit",
        "temporarily blocked",
        "please wait a few minutes",
    )
    is_spam = any(marker in lowered for marker in spam_markers)
    try:
        response_json = response.json()
    except ValueError:
        response_json = None
    has_validation_errors = isinstance(response_json, dict) and bool(response_json.get("errors"))
    remaining = response.headers.get("X-RateLimit-Remaining", "")
    is_rate_limit = status == 429 or (status == 403 and (is_spam or remaining == "0"))

    if status == 422 and has_validation_errors:
        kind, retryable = "validation", False
    elif status == 422 and is_spam:
        kind, retryable = "spam", True
    elif status == 422:
        kind, retryable = "validation", False
    elif is_rate_limit:
        kind, retryable = "rate_limit", True
    elif status >= 500:
        kind, retryable = "server", True
    else:
        kind, retryable = "http", False

    return GitHubAPIError(
        f"GitHub {operation} failed: HTTP {status} ({kind}): {body}",
        status_code=status,
        kind=kind,
        retryable=retryable,
        response_body=body,
    )


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Honor GitHub retry headers, otherwise use bounded exponential backoff."""

    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(_REVIEW_MAX_RETRY_DELAY, max(0.0, float(retry_after)))
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return min(_REVIEW_MAX_RETRY_DELAY, max(0.0, float(reset) - time.time()))
            except ValueError:
                pass
    return min(_REVIEW_MAX_RETRY_DELAY, float(2**attempt))
