"""Pinned, read-only repository workspace for hypothesis-pipeline runs.

The review pipeline must inspect the same repository state that triggered a
run.  ``PRHeadWorkspace`` materialises a GitHub tarball at the PR head SHA and
offers deterministic, local read/search operations.  When a tarball cannot be
downloaded, the workspace keeps the identity and uses the GitHub contents API
for file reads; repository-wide searches deliberately return no results in
that degraded mode because the API search endpoint is not SHA-scoped.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import logging
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from reviewforge.core.state import StateStore
from reviewforge.engine import symbol_extractor

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 200_000_000
COMMON_SOURCE_DIRS = frozenset({"src", "lib", "app", "pkg", "internal", "packages"})
_MAX_DEFINITION_EXCERPT_LINES = 20


class WorkspaceUnavailable(RuntimeError):  # noqa: N818 - public contract name is used by pipeline callers
    """Raised when a pinned workspace cannot provide a requested file."""

    reason = "workspace-unavailable"

    def __init__(self, message: str = "workspace-unavailable", *, reason: str = "workspace-unavailable") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WorkspaceInfo:
    """Identity and manifest for one immutable repository snapshot."""

    repo: str
    head_repo: str
    head_sha: str
    root: Path
    file_count: int
    byte_size: int
    digest: str
    truncated: bool
    source: str


@dataclass(frozen=True, slots=True)
class GrepHit:
    """One deterministic line match returned by :meth:`PRHeadWorkspace.grep`."""

    path: str
    line: int
    text: str
    context: str = ""
    start_line: int = 0
    end_line: int = 0

    @property
    def excerpt(self) -> str:
        """Return the bounded context, or the matching line if no context exists."""

        return self.context or self.text

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "text": self.text,
            "context": self.context,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True, slots=True)
class SymbolHit:
    """A definition found in the pinned workspace.

    ``name`` and ``file_path`` aliases mirror the existing
    ``symbol_extractor.SymbolInfo`` shape, while ``symbol`` and ``path`` are
    the names used by the hypothesis-pipeline tool contract.
    """

    path: str
    symbol: str
    symbol_type: str
    line: int
    start_line: int
    end_line: int
    excerpt: str

    @property
    def name(self) -> str:
        return self.symbol

    @property
    def file_path(self) -> str:
        return self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_path": self.path,
            "symbol": self.symbol,
            "name": self.symbol,
            "symbol_type": self.symbol_type,
            "line": self.line,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class _ArchiveFile:
    """A safe archive member with its path relative to the repository root."""

    path: str
    member: tarfile.TarInfo


def _normalise_repo_path(path: str) -> str | None:
    """Return a safe POSIX repository path, or ``None`` for an unsafe path."""

    if not isinstance(path, str):
        return None
    value = path.replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    parts = [part for part in PurePosixPath(value).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _language_name(language: str) -> str:
    """Normalise the small set of language aliases accepted by the tools."""

    value = str(language or "").strip().lower().lstrip(".")
    return {
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "py": "python",
        "golang": "go",
    }.get(value, value)


def _manifest(root: Path) -> tuple[int, int, str]:
    """Compute ``(file_count, byte_size, digest)`` for *root*.

    The digest input is deliberately simple and inspectable: sorted
    ``path:size:mtime`` records, one per line.  Archive mtimes are stored at
    second precision by GitHub, so the manifest uses integer seconds as well.
    """

    entries: list[tuple[str, int, int]] = []
    if root.exists():
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(root).as_posix()
            stat = candidate.stat()
            entries.append((relative, stat.st_size, int(stat.st_mtime)))
    entries.sort(key=lambda item: item[0])
    payload = "".join(f"{path}:{size}:{mtime}\n" for path, size, mtime in entries).encode("utf-8")
    return len(entries), sum(item[1] for item in entries), hashlib.sha256(payload).hexdigest()


def _format_lines(lines: list[str], start: int, end: int) -> str:
    """Render an inclusive, one-based range in the format used by old tools."""

    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


def _bounded_range(lines: list[str], start: int, end: int) -> tuple[int, int]:
    first = max(1, int(start))
    last = min(len(lines), int(end))
    if last < first:
        raise ValueError("read_file end_line must be >= start_line")
    return first, last


def _archive_prefix(members: list[tarfile.TarInfo], names: list[str]) -> str:
    """Find the GitHub archive wrapper directory, if present."""

    if not names:
        return ""
    top_levels = {name.split("/", 1)[0] for name in names}
    if len(top_levels) != 1:
        return ""
    candidate = next(iter(top_levels))
    # Do not mistake a one-file archive (``README.md``) for a wrapper.  GitHub
    # archives carry an explicit directory member; accepting a common prefix
    # without one also supports small hand-built test archives.
    has_wrapper = any(member.isdir() and _normalise_repo_path(member.name) == candidate for member in members)
    if not has_wrapper and all("/" in name for name in names):
        has_wrapper = candidate not in {"src", "lib", "app", "pkg", "internal", "packages", "test", "tests"}
    return candidate if has_wrapper else ""


def _relative_member_name(member_name: str, prefix: str) -> str | None:
    normalised = _normalise_repo_path(member_name)
    if normalised is None:
        return None
    if prefix:
        if normalised == prefix:
            return None
        marker = f"{prefix}/"
        if not normalised.startswith(marker):
            return None
        normalised = normalised[len(marker) :]
    return _normalise_repo_path(normalised)


async def _download_tarball(github: Any, repo: str, sha: str) -> bytes:
    """Download a tarball through a public fake hook or GitHubClient transport.

    ``GitHubClient`` predates the workspace contract and intentionally keeps
    its transport private.  Supporting a public ``get_repo_tarball`` hook
    makes the workspace easy to test; the private transport fallback keeps the
    production client compatible without expanding T2's file scope.
    """

    for method_name in ("get_repo_tarball", "download_tarball", "get_tarball"):
        method = getattr(github, method_name, None)
        if callable(method):
            result = method(repo, sha)
            if hasattr(result, "__await__"):
                result = await result
            return _tarball_bytes(result)

    client = getattr(github, "_client", None)
    if client is None or not callable(getattr(client, "get", None)):
        raise WorkspaceUnavailable("GitHub client does not provide a tarball transport")
    response = await client.get(f"/repos/{repo}/tarball/{sha}")
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    return _tarball_bytes(response)


def _tarball_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    content = getattr(value, "content", None)
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    raise WorkspaceUnavailable("GitHub tarball response did not contain bytes")


async def _probe_head_files(github: Any, repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    """Read the changed head files to validate that API fallback is usable.

    A missing/removed path is represented by an exception or ``None`` and does
    not count as a usable fallback.  Successful empty files do count: the API
    response, rather than truthiness of its body, is the evidence that the
    head exists and is readable.
    """

    method = getattr(github, "get_file_content", None)
    if not callable(method):
        return {}
    # The changed-file list is not an API-cost contract.  Normalize and sort
    # it before probing so fallback behavior is deterministic, then stop as
    # soon as one authoritative head file is available.  One successful file
    # is enough to establish that the head API works; probing the rest would
    # multiply requests for large PRs without making the fallback safer.
    candidates = sorted({_normalise_repo_path(raw_path) for raw_path in paths} - {None})
    for relative in candidates:
        try:
            result = method(repo, sha, relative)
            if hasattr(result, "__await__"):
                result = await result
            if result is None:
                continue
            if isinstance(result, bytes):
                content = result.decode("utf-8", errors="replace")
            else:
                content = str(result)
        except Exception as exc:
            # 404 for a deleted path is expected for a deletion-only PR.  It
            # remains a failed probe, just like any other unavailable path.
            logger.debug("Head fallback probe failed for %s@%s:%s: %s", repo, sha, relative, exc)
            continue
        return {relative: content}
    return {}


class PRHeadWorkspace:
    """Read-only snapshot bound to one PR head repository and SHA."""

    def __init__(
        self,
        info: WorkspaceInfo,
        github: Any,
        *,
        fallback_repo: str,
        temp_dir: Path,
        fallback_error: Exception | None = None,
    ) -> None:
        self.info = info
        self._github = github
        self._fallback_repo = fallback_repo
        self._temp_dir = temp_dir
        self._fallback_error = fallback_error
        self._content_cache: dict[str, str] = {}
        self._definition_cache: dict[tuple[str, str], tuple[symbol_extractor.SymbolInfo, ...]] = {}
        self._closed = False

    @classmethod
    async def build(
        cls,
        state: StateStore,
        github: Any,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> PRHeadWorkspace:
        """Build a workspace pinned to ``state.head_repo@state.head_sha``.

        If the tarball request or archive is unusable, changed head files are
        probed immediately through the contents API.  A degraded workspace is
        returned only when at least one probe succeeds; otherwise the build
        fails closed with ``reason=workspace-unavailable``.
        """

        if max_bytes < 0:
            raise ValueError("workspace max_bytes must be non-negative")

        temp_dir = Path(tempfile.mkdtemp(prefix="reviewforge-workspace-"))
        root = temp_dir / "repo"
        root.mkdir(parents=True, exist_ok=True)
        repo = str(state.repo or "")
        head_repo = str(state.head_repo or state.repo or "")
        head_sha = str(state.head_sha or "")

        try:
            archive = await _download_tarball(github, head_repo, head_sha)
            truncated = _extract_archive(
                archive,
                root,
                changed_paths=[str(path) for path in (state.files_changed or [])],
                max_bytes=max_bytes,
            )
        except Exception as exc:
            logger.warning("Unable to build PR head tarball for %s@%s: %s", head_repo, head_sha, exc)
            # A malformed archive can fail after writing a few members.  Do
            # not expose that partial world under the degraded identity.
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True, exist_ok=True)
            fallback_contents = await _probe_head_files(
                github,
                head_repo,
                head_sha,
                [str(path) for path in (state.files_changed or [])],
            )
            if not fallback_contents:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise WorkspaceUnavailable("workspace-unavailable") from exc
            file_count, byte_size, digest = _manifest(root)
            info = WorkspaceInfo(
                repo=repo,
                head_repo=head_repo,
                head_sha=head_sha,
                root=root,
                file_count=file_count,
                byte_size=byte_size,
                digest=digest,
                truncated=False,
                source="api-fallback",
            )
            workspace = cls(
                info,
                github,
                fallback_repo=head_repo,
                temp_dir=temp_dir,
                fallback_error=exc,
            )
            workspace._content_cache.update(fallback_contents)
            return workspace

        file_count, byte_size, digest = _manifest(root)
        info = WorkspaceInfo(
            repo=repo,
            head_repo=head_repo,
            head_sha=head_sha,
            root=root,
            file_count=file_count,
            byte_size=byte_size,
            digest=digest,
            truncated=truncated,
            source="tarball",
        )
        return cls(info, github, fallback_repo=head_repo, temp_dir=temp_dir)

    @property
    def root(self) -> Path:
        return self.info.root

    @property
    def source(self) -> str:
        return self.info.source

    @property
    def digest(self) -> str:
        return self.info.digest

    @property
    def head_sha(self) -> str:
        return self.info.head_sha

    @property
    def truncated(self) -> bool:
        return self.info.truncated

    def read(self, path: str, start: int | None = None, end: int | None = None) -> str | None:
        """Read a file or an inclusive one-based line range from the snapshot."""

        relative = _normalise_repo_path(path)
        if relative is None or self._closed:
            return None

        if self.info.source == "api-fallback":
            content = self._read_from_api_sync(relative)
        else:
            candidate = self._local_path(relative)
            if candidate is None or not candidate.is_file():
                return None
            content = self._read_local(relative, candidate)

        if start is None and end is None:
            return content
        lines = content.splitlines()
        first, last = _bounded_range(lines, start or 1, end or len(lines))
        return _format_lines(lines, first, last)

    async def read_async(self, path: str, start: int | None = None, end: int | None = None) -> str | None:
        """Async counterpart of :meth:`read` for the GitHub API fallback.

        Local snapshot reads remain synchronous and deterministic.  The public
        ``read`` method follows the SPEC signature; callers that may encounter
        ``source=api-fallback`` (notably ``ToolGateway``) use this adapter so
        the existing async ``GitHubClient`` is never driven from a running
        event loop synchronously.
        """

        relative = _normalise_repo_path(path)
        if relative is None or self._closed:
            return None
        if self.info.source == "api-fallback":
            content = await self._read_from_api_async(relative)
        else:
            result = self.read(relative, start=None, end=None)
            if result is None:
                return None
            content = result
        if start is None and end is None:
            return content
        lines = content.splitlines()
        first, last = _bounded_range(lines, start or 1, end or len(lines))
        return _format_lines(lines, first, last)

    def exists(self, path: str) -> bool:
        """Return whether *path* exists in the local snapshot."""

        relative = _normalise_repo_path(path)
        candidate = self._local_path(relative) if relative else None
        return bool(candidate and candidate.is_file())

    def grep(
        self,
        pattern: str,
        *,
        globs: list[str] | None,
        max_hits: int,
        context: int = 0,
    ) -> list[GrepHit]:
        """Search the local snapshot in stable path/line order."""

        if self._closed or self.info.source == "api-fallback" or max_hits <= 0:
            return []
        try:
            matcher = re.compile(str(pattern))
        except re.error:
            return []

        patterns = tuple(str(item).replace("\\", "/") for item in (globs or []) if str(item))
        margin = max(0, int(context))
        hits: list[GrepHit] = []
        for relative, candidate in self._iter_files():
            if patterns and not any(_glob_matches(relative, item) for item in patterns):
                continue
            lines = self._read_local(relative, candidate).splitlines()
            if any("\x00" in line for line in lines):
                continue
            for index, line in enumerate(lines):
                if not matcher.search(line):
                    continue
                first = max(0, index - margin)
                last = min(len(lines), index + margin + 1)
                hits.append(
                    GrepHit(
                        path=relative,
                        line=index + 1,
                        text=line,
                        context=_format_lines(lines, first + 1, last),
                        start_line=first + 1,
                        end_line=last,
                    )
                )
                if len(hits) >= max_hits:
                    return hits
        return hits

    def find_symbol_definitions(self, symbol: str, *, language: str) -> list[SymbolHit]:
        """Find definitions using the language-aware symbol extractor cache."""

        if self._closed or self.info.source == "api-fallback":
            return []
        target = str(symbol)
        wanted_language = _language_name(language)
        hits: list[SymbolHit] = []
        for relative, candidate in self._iter_files():
            detected = _language_name(symbol_extractor.detect_language(relative))
            if wanted_language and detected != wanted_language:
                continue
            key = (relative, detected)
            definitions = self._definition_cache.get(key)
            if definitions is None:
                content = self._read_local(relative, candidate)
                definitions = tuple(symbol_extractor.extract_definitions(content, relative))
                self._definition_cache[key] = definitions
            for definition in definitions:
                if definition.name != target:
                    continue
                start_line = definition.start_line or definition.line
                end_line = definition.end_line or definition.line
                lines = self._read_local(relative, candidate).splitlines()
                excerpt_end = min(len(lines), max(end_line, start_line) + _MAX_DEFINITION_EXCERPT_LINES - 1)
                excerpt = _format_lines(lines, start_line, excerpt_end) if lines and start_line <= excerpt_end else ""
                hits.append(
                    SymbolHit(
                        path=relative,
                        symbol=definition.name,
                        symbol_type=definition.symbol_type,
                        line=definition.line,
                        start_line=start_line,
                        end_line=end_line,
                        excerpt=excerpt,
                    )
                )
        return hits

    def find_callers(self, symbol: str, *, language: str, max_hits: int) -> list[GrepHit]:
        """Find call expressions while excluding definition lines."""

        if self._closed or self.info.source == "api-fallback" or max_hits <= 0:
            return []
        target = str(symbol)
        if not target:
            return []
        try:
            matcher = re.compile(rf"\b{re.escape(target)}\s*\(")
        except re.error:
            return []
        wanted_language = _language_name(language)
        hits: list[GrepHit] = []
        for relative, candidate in self._iter_files():
            detected = _language_name(symbol_extractor.detect_language(relative))
            if wanted_language and detected != wanted_language:
                continue
            content = self._read_local(relative, candidate)
            lines = content.splitlines()
            definition_lines = {
                definition.line
                for definition in self._definitions_for(relative, detected, content)
                if definition.name == target
            }
            for index, line in enumerate(lines):
                line_number = index + 1
                if line_number in definition_lines or not matcher.search(line):
                    continue
                hits.append(GrepHit(path=relative, line=line_number, text=line))
                if len(hits) >= max_hits:
                    return hits
        return hits

    def cleanup(self) -> None:
        """Remove the temporary snapshot; safe to call more than once."""

        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _local_path(self, relative: str | None) -> Path | None:
        if not relative or self._closed:
            return None
        candidate = self.info.root.joinpath(*relative.split("/"))
        try:
            resolved_root = self.info.root.resolve()
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        return candidate

    def _read_local(self, relative: str, candidate: Path) -> str:
        cached = self._content_cache.get(relative)
        if cached is not None:
            return cached
        content = candidate.read_text(encoding="utf-8", errors="replace")
        self._content_cache[relative] = content
        return content

    def _read_from_api_sync(self, relative: str) -> str:
        cached = self._content_cache.get(relative)
        if cached is not None:
            return cached
        method = getattr(self._github, "get_file_content", None)
        if not callable(method):
            raise WorkspaceUnavailable("GitHub client does not provide contents fallback")
        try:
            result = method(self._fallback_repo, self.info.head_sha, relative)
            if hasattr(result, "__await__"):
                raise WorkspaceUnavailable("contents fallback requires read_async")
        except WorkspaceUnavailable:
            raise
        except Exception as exc:
            raise WorkspaceUnavailable(f"contents fallback failed: {exc}") from exc
        return self._cache_api_content(relative, result)

    async def _read_from_api_async(self, relative: str) -> str:
        cached = self._content_cache.get(relative)
        if cached is not None:
            return cached
        method = getattr(self._github, "get_file_content", None)
        if not callable(method):
            raise WorkspaceUnavailable("GitHub client does not provide contents fallback")
        try:
            result = method(self._fallback_repo, self.info.head_sha, relative)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:
            raise WorkspaceUnavailable(f"contents fallback failed: {exc}") from exc
        return self._cache_api_content(relative, result)

    def _cache_api_content(self, relative: str, result: Any) -> str:
        if result is None:
            raise WorkspaceUnavailable("contents fallback returned no content")
        if isinstance(result, bytes):
            content = result.decode("utf-8", errors="replace")
        else:
            content = str(result)
        self._content_cache[relative] = content
        return content

    def _iter_files(self) -> list[tuple[str, Path]]:
        if self._closed or not self.info.root.exists():
            return []
        files: list[tuple[str, Path]] = []
        for candidate in self.info.root.rglob("*"):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(self.info.root).as_posix()
            files.append((relative, candidate))
        files.sort(key=lambda item: item[0])
        return files

    def _definitions_for(
        self,
        relative: str,
        language: str,
        content: str,
    ) -> tuple[symbol_extractor.SymbolInfo, ...]:
        key = (relative, language)
        definitions = self._definition_cache.get(key)
        if definitions is None:
            definitions = tuple(symbol_extractor.extract_definitions(content, relative))
            self._definition_cache[key] = definitions
        return definitions


def _glob_matches(path: str, pattern: str) -> bool:
    """Match both basename globs and repository-relative ``**`` globs."""

    candidate = PurePosixPath(path)
    if candidate.match(pattern):
        return True
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    if "/**/" in pattern and fnmatch.fnmatchcase(path, pattern.replace("/**/", "/")):
        return True
    return False


def _extract_archive(archive: bytes, root: Path, *, changed_paths: list[str], max_bytes: int) -> bool:
    """Safely extract a bounded archive and return whether content was omitted."""

    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise WorkspaceUnavailable(f"invalid repository tarball: {exc}") from exc

    with tar:
        members = tar.getmembers()
        names = [name for member in members if (name := _normalise_repo_path(member.name)) is not None]
        prefix = _archive_prefix(members, names)
        files: dict[str, _ArchiveFile] = {}
        for member in members:
            relative = _relative_member_name(member.name, prefix)
            if relative is None or not member.isfile():
                continue
            if member.size < 0:
                continue
            files.setdefault(relative, _ArchiveFile(relative, member))

        total_size = sum(item.member.size for item in files.values())
        truncated = total_size > max_bytes
        selected = list(files.values())
        if truncated:
            changed = [_normalise_repo_path(path) for path in changed_paths]
            changed = [path for path in changed if path]
            changed_exact = set(changed)
            changed_roots = {path.split("/", 1)[0] for path in changed}

            def priority(item: _ArchiveFile) -> tuple[int, str]:
                first = item.path.split("/", 1)[0]
                if item.path in changed_exact:
                    rank = 0
                elif first in changed_roots:
                    rank = 1
                elif first in COMMON_SOURCE_DIRS:
                    rank = 2
                else:
                    rank = 3
                return rank, item.path

            selected = sorted(selected, key=priority)
            # The bounded mode is intentionally conservative: files outside
            # the changed top-level directories and common source directories
            # are omitted rather than spending the remaining budget on an
            # unrelated tree.
            selected = [item for item in selected if priority(item)[0] <= 2]
            bounded: list[_ArchiveFile] = []
            consumed = 0
            for item in selected:
                size = int(item.member.size)
                if consumed + size > max_bytes:
                    continue
                bounded.append(item)
                consumed += size
            selected = bounded
        else:
            selected.sort(key=lambda item: item.path)

        for item in selected:
            target = root.joinpath(*item.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(item.member)
            if extracted is None:
                continue
            with extracted, target.open("wb") as output:
                shutil.copyfileobj(extracted, output)
            try:
                os.utime(target, (item.member.mtime, item.member.mtime))
                target.chmod(item.member.mode & 0o777)
            except (OSError, ValueError):
                # File metadata is not needed for reading, and some tarballs
                # contain modes/mtimes unsupported by the host filesystem.
                pass
    return truncated
