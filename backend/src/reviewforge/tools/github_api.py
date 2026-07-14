"""GitHub API client — async wrapper around GitHub REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

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
    ) -> dict[str, Any]:
        """Create one COMMENT review containing a bounded set of inline comments."""

        if not comments:
            raise ValueError("A review requires at least one inline comment")
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
        payload = {
            "commit_id": commit_sha,
            "event": "COMMENT",
            "comments": review_comments,
        }
        async with _REVIEW_WRITE_LOCK:
            return await self._post_json_with_retry(
                f"/repos/{repo}/pulls/{int(pr_number)}/reviews",
                payload,
                operation="create pull request review",
            )

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
