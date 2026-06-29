"""GitHub API client — async wrapper around GitHub REST API."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


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
        """Post a review comment on a specific line."""
        resp = await self._client.post(
            f"/repos/{repo}/pulls/{int(pr_number)}/comments",
            json={
                "body": body,
                "commit_id": commit_sha,
                "path": file_path,
                "line": int(line),
                "side": "RIGHT",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Get list of files changed in a PR (paginated)."""
        return await self._list_pr_files(repo, pr_number)

    async def get_pr_info(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Get PR metadata."""
        resp = await self._client.get(f"/repos/{repo}/pulls/{int(pr_number)}")
        resp.raise_for_status()
        return resp.json()
