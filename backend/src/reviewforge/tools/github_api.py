"""GitHub API client — wraps the GitHub REST API for ReviewForge."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GitHubClient:
    """Thin wrapper around GitHub REST API."""

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

    async def close(self) -> None:
        await self._client.aclose()

    def get_file_diff(self, repo: str, pr_number: int, file_path: str) -> str:
        """Get the diff for a specific file in a PR. (Sync wrapper for tool loop.)"""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an async context, use a workaround
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self._get_file_diff_async(repo, pr_number, file_path)
                )
                return future.result(timeout=30)
        return asyncio.run(self._get_file_diff_async(repo, pr_number, file_path))

    async def _get_file_diff_async(self, repo: str, pr_number: int, file_path: str) -> str:
        resp = await self._client.get(
            f"/repos/{repo}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        for f in resp.json():
            if f.get("filename") == file_path or f.get("filename", "").endswith(file_path):
                return f.get("patch", "")
        return f"No diff found for {file_path}"

    def get_file_content(self, repo: str, ref: str, file_path: str) -> str:
        """Get file content at a specific ref."""
        import asyncio
        return asyncio.run(self._get_file_content_async(repo, ref, file_path))

    async def _get_file_content_async(self, repo: str, ref: str, file_path: str) -> str:
        resp = await self._client.get(
            f"/repos/{repo}/contents/{file_path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.v3.raw"},
        )
        resp.raise_for_status()
        return resp.text

    def search_code(self, repo: str, pattern: str, file_glob: str = "") -> str:
        """Search code in a repository."""
        import asyncio
        return asyncio.run(self._search_code_async(repo, pattern, file_glob))

    async def _search_code_async(self, repo: str, pattern: str, file_glob: str = "") -> str:
        query = f"{pattern} repo:{repo}"
        if file_glob:
            query += f" path:{file_glob}"
        resp = await self._client.get("/search/code", params={"q": query, "per_page": 10})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        return "\n".join(f"- {item['path']}" for item in items) or "No results"

    def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        file_path: str,
        line: int,
        body: str,
    ) -> dict[str, Any]:
        """Post a review comment on a specific line."""
        import asyncio
        return asyncio.run(self._post_comment_async(repo, pr_number, commit_sha, file_path, line, body))

    async def _post_comment_async(
        self, repo: str, pr_number: int, commit_sha: str,
        file_path: str, line: int, body: str,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/repos/{repo}/pulls/{pr_number}/comments",
            json={
                "body": body,
                "commit_id": commit_sha,
                "path": file_path,
                "line": line,
                "side": "RIGHT",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get list of files changed in a PR."""
        resp = await self._client.get(
            f"/repos/{repo}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pr_info(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Get PR metadata."""
        resp = await self._client.get(f"/repos/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()
