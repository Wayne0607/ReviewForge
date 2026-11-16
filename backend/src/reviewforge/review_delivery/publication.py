"""Durable, independent GitHub delivery and correction for findings."""

from __future__ import annotations

import hashlib
import json
import math
from time import time
from urllib.parse import quote

from reviewforge.core.change_manifest import valid_path, valid_repo, valid_sha
from reviewforge.tools.github_api import GitHubAPIError


class FindingPublisher:
    """Publish frozen claims; never repeat an ambiguous remote write."""

    def __init__(self, store, github, bot_user_id: int):
        if type(bot_user_id) is not int or bot_user_id <= 0:
            raise ValueError("a trusted GitHub actor ID is required")
        self.store = store
        self.github = github
        self.bot_user_id = bot_user_id
        binding = store.binding
        if not valid_repo(binding.repo) or type(binding.pr_number) is not int or binding.pr_number <= 0:
            raise ValueError("invalid publication binding")
        if not valid_sha(binding.head_sha) or not valid_sha(binding.base_sha):
            raise ValueError("invalid publication commit")

    def _body(self, item):
        finding = self.store.get_finding(item["finding_id"])
        binding = self.store.binding
        identity = json.dumps(
            [binding.repo, binding.pr_number, binding.head_sha, item["finding_id"]],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        marker = f"<!-- reviewforge-finding:{hashlib.sha256(identity.encode()).hexdigest()} -->"
        if item["marker"] != marker:
            raise ValueError("outbox marker binding mismatch")
        verdict = self.store.get_verdict(item["verdict_id"])
        if verdict["finding_id"] != finding["id"] or verdict["finding_revision"] != item["finding_revision"]:
            raise ValueError("outbox verdict binding mismatch")
        if finding["revision"] != item["finding_revision"]:
            raise ValueError("finding revision changed")
        if verdict["verdict"] in {"reject", "needs_evidence"}:
            previous = item.get("last_delivered_body")
            if (
                type(item.get("remote_id")) is not int
                or item["remote_id"] <= 0
                or not isinstance(previous, str)
                or not previous.endswith(marker)
                or previous.count(marker) != 1
            ):
                raise ValueError("correction is missing its original receipt or body")
            for source_id in verdict["source_ids"]:
                self.store.get_source(source_id)
            if verdict["verdict"] == "needs_evidence":
                missing = verdict.get("missing_fact")
                attempted = verdict.get("attempted")
                recovery = verdict.get("recovery_condition")
                if (
                    not isinstance(missing, str)
                    or not missing.strip()
                    or not isinstance(recovery, str)
                    or not recovery.strip()
                    or not isinstance(attempted, list)
                    or not attempted
                    or any(not isinstance(value, str) or not value.strip() for value in attempted)
                    or any(marker in value for value in [missing, recovery, *attempted])
                ):
                    raise ValueError("missing evidence notice is incomplete or cannot be rendered")
                sources = "\n".join(f"- {value}" for value in attempted)
                return (
                    "**Verification incomplete / 补充核验未完成**\n\n"
                    f"The prior finding for reviewed commit `{binding.head_sha}` cannot currently be treated as "
                    "a confirmed conclusion. 原判决暂不能作为当前结论。\n\n"
                    f"Missing fact / 缺少的事实：{missing}\n\n"
                    f"Sources attempted / 已查来源：\n{sources}\n\n"
                    f"Recovery condition / 恢复条件：{recovery}\n\n---\n\n{previous}"
                )
            reason = verdict.get("reason")
            if not isinstance(reason, str) or not reason.strip() or marker in reason:
                raise ValueError("retraction is missing its reason")
            return (
                f"**Retracted / 已撤回**\n\n"
                f"The finding for reviewed commit `{binding.head_sha}` is no longer valid. "
                f"原审查提交 `{binding.head_sha}` 的以下问题已不成立。\n\n"
                f"{reason}\n\n---\n\n{previous}"
            )
        if finding["revision"] != item["finding_revision"] or finding["claim"] != item["approved_payload"]:
            raise ValueError("approved finding revision changed")
        payload = item["approved_payload"]
        if (
            not isinstance(payload, dict)
            or not payload
            or set(payload) - {"title", "body", "statement", "trigger", "impact"}
        ):
            raise ValueError("approved claim cannot be rendered without losing fields")
        if any(not isinstance(value, str) or not value.strip() for value in payload.values()):
            raise ValueError("approved claim contains an unrenderable field")
        if not any(key in payload for key in ("body", "statement", "trigger", "impact")):
            raise ValueError("approved claim has no description")
        parts = []
        if "title" in payload:
            parts.append(f"**{payload['title']}**")
        parts.extend(payload[key] for key in ("body", "statement", "trigger", "impact") if key in payload)
        claim = "\n\n".join(parts)
        if item["marker"] in claim:
            raise ValueError("claim contains delivery marker")
        links = []
        if (
            verdict["verdict"] != "accept"
            or verdict["finding_id"] != finding["id"]
            or verdict["finding_revision"] != item["finding_revision"]
            or verdict["approved_payload"] != item["approved_payload"]
        ):
            raise ValueError("outbox verdict binding mismatch")
        for source_id in verdict["source_ids"]:
            source = self.store.get_source(source_id)  # verifies persisted content and provenance
            side, metadata = source["snapshot_side"], source["metadata"]
            if source["kind"] != "original" or side not in {"base", "head"} or not isinstance(metadata, dict):
                continue
            path = metadata.get("path")
            sha = binding.base_sha if side == "base" else binding.head_sha
            if not isinstance(path, str) or not valid_path(path) or metadata.get("revision") != sha:
                continue
            encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
            url = f"https://github.com/{binding.repo}/blob/{sha}/{encoded_path}"
            start, end = metadata.get("start_line"), metadata.get("end_line")
            if type(start) is int and start > 0:
                url += f"#L{start}"
                if type(end) is int and end > start:
                    url += f"-L{end}"
            link = f"[{path} ({side})]({url})"
            if link not in links:
                links.append(link)
        if not links:
            raise ValueError("accepted finding has no verified pinned code location")
        return claim + "\n\n" + "\n".join(links) + f"\n\n{item['marker']}"

    async def _head_matches(self, *, correction=False):
        binding = self.store.binding
        pr = await self.github.get_pr_info(binding.repo, binding.pr_number)
        if not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] != binding.pr_number:
            raise GitHubAPIError("PR identity mismatch", kind="delivery_uncertain")
        if pr.get("state") not in {"open", "closed"}:
            raise GitHubAPIError("PR state is unavailable", kind="delivery_uncertain")
        if pr.get("state") != "open" and not correction:
            raise GitHubAPIError("PR is not open or its state is unavailable", kind="delivery_uncertain")
        base, head = pr.get("base"), pr.get("head")
        repo = base.get("repo") if isinstance(base, dict) else None
        if not isinstance(repo, dict) or str(repo.get("full_name", "")).casefold() != binding.repo.casefold():
            raise GitHubAPIError("PR repository mismatch", kind="delivery_uncertain")
        if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
            raise GitHubAPIError("Missing PR head", kind="delivery_uncertain")
        return correction or head["sha"] == binding.head_sha

    def _is_ours(self, comment, marker):
        body = comment.get("body")
        if not isinstance(body, str) or marker not in body:
            return False
        user = comment.get("user")
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise GitHubAPIError("Marker comment has invalid author", kind="delivery_uncertain")
        if user["id"] != self.bot_user_id:
            return False
        binding = self.store.binding
        issue_url = f"{self.github.BASE_URL}/repos/{binding.repo}/issues/{binding.pr_number}"
        if (
            type(comment.get("id")) is not int
            or comment["id"] <= 0
            or str(comment.get("issue_url", "")).casefold() != issue_url.casefold()
        ):
            raise GitHubAPIError("Marker comment has invalid receipt identity", kind="delivery_uncertain")
        return True

    async def _lookup(self, item):
        binding = self.store.binding
        comments = await self.github.list_delivery_issue_comments(binding.repo, binding.pr_number)
        matches = [c for c in comments if self._is_ours(c, item["marker"])]
        if len(matches) > 1:
            raise GitHubAPIError("Multiple authorized marker comments", kind="delivery_uncertain")
        return matches[0] if matches else None

    async def publish(self, finding_id):
        item = self.store.get_outbox(finding_id)
        if item is None:
            return {"finding_id": finding_id, "status": "absent"}
        revision = item["finding_revision"]
        if item["status"] in {"delivered", "head_changed"}:
            return item
        not_before = item.get("retry_not_before")
        if not_before is not None:
            if type(not_before) not in {int, float} or not math.isfinite(not_before):
                raise ValueError("invalid delivery cooldown")
            if item["status"] == "intended" and time() < not_before:
                return item
        body = self._body(item)
        correction = self.store.get_verdict(item["verdict_id"])["verdict"] in {"reject", "needs_evidence"}
        item = self.store.freeze_outbox(finding_id, revision, body)
        existing = await self._lookup(item)
        if existing and item["remote_id"] and existing["id"] != item["remote_id"]:
            raise GitHubAPIError("Marker receipt ID changed", kind="delivery_uncertain")
        if existing and existing["body"] == body:
            if not await self._head_matches(correction=correction):
                return self.store.transition_outbox(
                    finding_id, revision, item["status"], "head_changed", existing["id"]
                )
            return self.store.transition_outbox(finding_id, revision, item["status"], "delivered", existing["id"])
        if item["status"] == "uncertain":
            return item  # absence or a prior body does not prove the remote write failed
        if correction and existing is not None and existing["body"] != item["last_delivered_body"]:
            raise GitHubAPIError("Original comment body changed", kind="delivery_uncertain")
        if item["remote_id"] and existing is None:
            raise GitHubAPIError("Original comment receipt not found", kind="delivery_uncertain")
        if existing and not item["remote_id"]:
            raise GitHubAPIError("Existing marker has a different body", kind="delivery_uncertain")
        if not await self._head_matches(correction=correction):
            return self.store.transition_outbox(finding_id, revision, "intended", "head_changed")
        # The durable CAS fences concurrent writers and prevents revision changes.
        item = self.store.transition_outbox(finding_id, revision, "intended", "uncertain")
        binding = self.store.binding
        try:
            receipt = await self.github.write_delivery_issue_comment(
                binding.repo, binding.pr_number, body, remote_id=item["remote_id"]
            )
        except GitHubAPIError as exc:
            # A returned HTTP rejection proves no successful write. Transport errors,
            # timeouts, server errors and malformed success receipts remain unknown.
            if (
                400 <= exc.status_code < 500
                and exc.status_code != 408
                and exc.kind in {"validation", "rate_limit", "spam", "http"}
            ):
                self.store.transition_outbox(
                    finding_id, revision, "uncertain", "intended", retry_not_before=exc.retry_not_before
                )
            raise
        if not self._is_ours(receipt, item["marker"]) or receipt.get("body") != body:
            raise GitHubAPIError("Issue comment receipt mismatch", kind="delivery_uncertain")
        if item["remote_id"] and receipt["id"] != item["remote_id"]:
            raise GitHubAPIError("Issue comment receipt ID changed", kind="delivery_uncertain")
        self.store.transition_outbox(finding_id, revision, "uncertain", "uncertain", receipt["id"])
        status = "delivered" if await self._head_matches(correction=correction) else "head_changed"
        return self.store.transition_outbox(finding_id, revision, "uncertain", status, receipt["id"])

    async def drain(self):
        results = []
        for item in self.store.list_outbox():
            try:
                results.append(await self.publish(item["finding_id"]))
            except Exception as exc:
                # One finding's unavailable receipt cannot block another.
                current = self.store.get_outbox(item["finding_id"])
                results.append({**(current or item), "error": str(exc)})
        return results
