"""Mock GitHub client for testing without real API calls."""

from __future__ import annotations

from typing import Any


class MockGitHubClient:
    """Returns deterministic responses for testing."""

    def __init__(self) -> None:
        self.posted_comments: list[dict[str, Any]] = []

    async def close(self) -> None:
        pass

    async def get_file_diff(self, repo: str, pr_number: int, file_path: str) -> str:
        return f"@@ -1,3 +1,5 @@\n+import os\n+import pickle\n def test():\n     pass"

    async def get_file_content(self, repo: str, ref: str, file_path: str) -> str:
        return "# test file\nprint('hello')\n"

    async def search_code(self, repo: str, pattern: str, file_glob: str = "") -> str:
        return f"- {file_glob or 'src/'}:match.py"

    async def post_review_comment(
        self, repo: str, pr_number: int, commit_sha: str,
        file_path: str, line: int, body: str,
    ) -> dict[str, Any]:
        comment = {"file": file_path, "line": line, "body": body}
        self.posted_comments.append(comment)
        return {"id": len(self.posted_comments), "body": body}

    async def get_pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return [
            {"filename": "test.py", "additions": 5, "deletions": 2, "patch": "+import os\n+import pickle"},
        ]

    async def get_pr_info(self, repo: str, pr_number: int) -> dict[str, Any]:
        return {"number": pr_number, "title": "test PR", "state": "open"}
