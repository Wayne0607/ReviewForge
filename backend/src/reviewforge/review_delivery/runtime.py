"""Persistent, independent native-tool review sessions.

One controller owns a session at a time. Model calls are metered by the injected
model; an interrupted request without a response is retried on resume.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from .agents import LEAD_PROMPT, RESEARCH_PROMPT, VERIFY_PROMPT
from .context import READ_TOOLS, projected_receipt, reading_cycle, resident_sources
from .tools import COMMON, LEAD, RESEARCH, VERIFY


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _visible(response: AIMessage) -> AIMessage:
    # Provider reasoning, metadata, and opaque provider-specific blocks are not replayed.
    content = response.content
    if isinstance(content, list):
        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return AIMessage(
        content=content or "",
        tool_calls=response.tool_calls or [],
        invalid_tool_calls=response.invalid_tool_calls or [],
    )


class DeliveryRuntime:
    def __init__(self, store, gateway, reviewer_model, verifier_model, publisher=None, research_model=None):
        self.store, self.gateway = store, gateway
        self.models = {"lead": reviewer_model, "verifier": verifier_model, "research": research_model or reviewer_model}
        self.publisher = publisher

    def _events(self, session):
        return [row["payload"] for row in self.store.list_turns(session)]

    def _append(self, session, event, key):
        self.store.append_turn(session, event, turn_id=session + ":" + key)

    async def run(self, task: str):
        leads = [s for s in self.store.list_sessions() if s["role"] == "lead"]
        lead = leads[0] if leads else self.store.create_session("lead")
        # Recover outstanding work, including already accepted delivery, before further sampling.
        if self.publisher:
            await self.publisher()
        for item in self.store.list_research():
            if item["status"] == "open":
                await self._research(item)
        for finding in self.store.list_findings():
            if finding["state"] != "open":
                continue
            existing = [
                v
                for v in self.store.list_verifications()
                if v["finding_id"] == finding["id"] and v["finding_revision"] == finding["revision"]
            ]
            if not existing:
                session_id = (
                    "recover_" + hashlib.sha256(f"{finding['id']}:{finding['revision']}".encode()).hexdigest()[:24]
                )
                verifier_session = self.store.create_session("verifier", session_id=session_id)
                self.store.create_verification(finding["id"], verifier_session["id"])
        for item in self.store.list_verifications():
            if item["status"] == "open":
                await self._verify(item)
        result = await self._session(lead["id"], "lead", task)
        if self.publisher:
            await self.publisher()
        latest = {}
        for verification in self.store.list_verifications():
            finding = self.store.get_finding(verification["finding_id"])
            if verification["finding_revision"] == finding["revision"]:
                latest[finding["id"]] = verification
        unresolved = [v for v in latest.values() if v["status"] in {"open", "needs_evidence"}]
        unresolved_research = [r for r in self.store.list_research() if r["status"] == "open"]
        unresolved_findings = [f for f in self.store.list_findings() if f["state"] in {"open", "needs_evidence"}]
        if unresolved or unresolved_research or unresolved_findings:
            result = {
                **result,
                "status": "partial",
                "unresolved_verifications": unresolved,
                "unresolved_research": unresolved_research,
                "unresolved_findings": unresolved_findings,
            }
        return result

    async def resume_finding(self, finding_id):
        """Resume a missing-evidence finding without changing its identity or resampling acceptance."""
        finding = self.store.get_finding(finding_id)
        if finding["state"] != "needs_evidence":
            raise ValueError("only a finding with missing evidence can be retried")
        session = self.store.create_session("verifier")
        task = self.store.create_verification(finding_id, session["id"])
        return await self._verify(task)

    def _working_set(self, session, role):
        progress = {}
        events = self._events(session)
        retained = resident_sources(events, self.store.get_source)
        for event in events:
            if event.get("type") == "progress":
                progress = event["value"]
        sources = []
        for source_id in retained:
            item = self.store.get_source(source_id)
            sources.append({**item, "content": item["content"].decode("utf-8")})
        projected = {
            "progress": progress,
            "retained_sources": sources,
            "changed_notes": self.store.changed_note_tasks(session),
        }
        basis = self._context_basis(session)
        warning = next(
            (e["reading_cycle"] for e in reversed(events) if e.get("reading_cycle", {}).get("basis") == basis),
            None,
        )
        if warning and warning["status"] == "warning":
            projected["repeated_reading"] = {
                "reason": "The same read batch repeated without new material or investigation progress.",
                "resident_source_ids": retained,
                "next_action": (
                    "Compare the resident originals, investigate a new fact, or submit your conclusion or concrete gap."
                ),
            }
        if role == "lead":
            selected = set(progress.get("finding_ids", []))
            findings = self.store.list_findings()
            projected["finding_index"] = [
                {"id": f["id"], "title": f["claim"].get("title", ""), "state": f["state"]} for f in findings
            ]
            projected["findings"] = [
                {k: f[k] for k in ("id", "revision", "state", "claim")} for f in findings if f["id"] in selected
            ]
        return projected, retained

    def _context_basis(self, session):
        events = self._events(session)
        progress = next((e["value"] for e in reversed(events) if e.get("type") == "progress"), {})
        # Source availability and actual session reads are distinct. Neither
        # a shared catalog entry nor this digest grants citation permission.
        basis = {
            "available_sources": [s["id"] for s in self.store.list_sources()],
            "session_reads": sorted({sid for e in events if e.get("ok") for sid in e.get("source_ids", [])}),
            "progress": progress,
            # Ordinary visible observations also count as investigation progress;
            # explicit set_progress is optional, and provider reasoning is excluded.
            "visible_observations": sorted(
                {
                    " ".join(m["data"]["content"].split())
                    for e in events
                    if e.get("type") == "response"
                    for m in e.get("message", [])
                    if isinstance(m.get("data", {}).get("content"), str) and m["data"]["content"].strip()
                }
            ),
            "findings": self.store.list_findings(),
            "verdicts": self.store.list_verdicts(),
            "research": self.store.list_research(),
            "notes": sorted(
                {_json({k: n[k] for k in ("task_id", "text", "source_ids")}) for n in self.store.list_notes()}
            ),
        }
        return hashlib.sha256(_json(basis).encode()).hexdigest()

    def _read_cycle(self, session, calls, outcomes):
        if not calls or not all(e["ok"] for e in outcomes) or any(c["name"] not in READ_TOOLS for c in calls):
            return None
        batch = sorted(_json({"name": c["name"], "args": c.get("args")}) for c in calls)
        return reading_cycle(
            self._events(session), self._context_basis(session), hashlib.sha256(_json(batch).encode()).hexdigest()
        )

    @staticmethod
    def _cycle_result(session):
        return {
            "status": "partial",
            "reason": "repeated reading without new evidence or investigation progress after correction",
            "session_id": session,
            "recovery": (
                "Provide a new source or investigation focus; originals and prior results remain saved. "
                "No semantic verdict was inferred."
            ),
        }

    async def _research(self, item):
        return await self._session(item["session_id"], "research", item["question"], item["id"])

    async def _verify(self, item):
        finding = self.store.get_finding(item["finding_id"])
        if finding["revision"] != item["finding_revision"]:
            return {"status": "stale", "finding_id": finding["id"]}
        context = _json(
            {
                "finding_id": finding["id"],
                "revision": finding["revision"],
                "claim": finding["claim"],
                "lead_source_ids": finding["source_ids"],
                "revision_reason": self.store.get_finding_revision(finding["id"], finding["revision"])["reason"],
            }
        )
        return await self._session(item["session_id"], "verifier", context, item["id"])

    async def _session(self, session, role, task, task_id=None):
        prompts = {"lead": LEAD_PROMPT, "verifier": VERIFY_PROMPT, "research": RESEARCH_PROMPT}
        schemas = [
            *self.gateway.tool_specs,
            *COMMON,
            *(LEAD if role == "lead" else VERIFY if role == "verifier" else RESEARCH),
        ]
        model = self.models[role].bind_tools(schemas, tool_choice="auto")
        failed = None
        failures = 0
        while True:
            events = self._events(session)
            completed = next((e for e in reversed(events) if e.get("type") == "complete"), None)
            if completed:
                return completed["result"]
            halted = next(
                (e["reading_cycle"] for e in reversed(events) if e.get("reading_cycle", {}).get("status") == "halt"),
                None,
            )
            if halted and halted["basis"] == self._context_basis(session):
                return self._cycle_result(session)
            # An exchange is either fully persisted or resumed from the persisted response.
            requests = [e for e in events if e.get("type") == "request"]
            last = requests[-1] if requests else None
            response_event = next(
                (e for e in events if e.get("type") == "response" and last and e["request_id"] == last["id"]), None
            )
            done = any(e.get("type") == "exchange_done" and last and e["request_id"] == last["id"] for e in events)
            if last is None or done:
                previous = []
                delivered = []
                note_watermarks = {}
                if last and response_event:
                    previous = response_event["message"][:]
                    receipts = [e for e in events if e.get("type") == "receipt" and e["request_id"] == last["id"]]
                    receipts.sort(key=lambda x: x["index"])
                    delivered = [sid for e in receipts for sid in e.get("source_ids", [])]
                    for receipt in receipts:
                        for key, seq in receipt.get("note_watermarks", {}).items():
                            note_watermarks[key] = max(note_watermarks.get(key, 0), seq)
                working_set, retained = self._working_set(session, role)
                if last and response_event:
                    previous += [projected_receipt(e, retained) for e in receipts]
                delivered = list(dict.fromkeys([*delivered, *retained]))
                chat = [
                    SystemMessage(content=prompts[role]),
                    HumanMessage(content=task),
                    HumanMessage(content="Current session working set (notes are navigation): " + _json(working_set)),
                ]
                chat += messages_from_dict(previous)
                request_id = uuid.uuid4().hex
                last = {
                    "type": "request",
                    "id": request_id,
                    "messages": messages_to_dict(chat),
                    "source_ids": delivered,
                    "note_watermarks": note_watermarks,
                }
                self._append(session, last, "request:" + request_id)
                response_event = None
            request_id = last["id"]
            if response_event is None:
                raw_response = await model.ainvoke(messages_from_dict(last["messages"]))
                response = _visible(raw_response)
                response_event = {
                    "type": "response",
                    "request_id": request_id,
                    "message": messages_to_dict([response]),
                    "raw_message": messages_to_dict([raw_response]),
                }
                self._append(session, response_event, "response:" + request_id)
            # A persisted successful response proves the request and the included originals were delivered.
            self.store.record_delivery(session, last.get("source_ids", []))
            if last.get("note_watermarks"):
                self.store.record_note_reads(session, last["note_watermarks"])
            response = messages_from_dict(response_event["message"])[0]
            calls = response.tool_calls
            if not calls:
                # Give the model a correction, rather than treating prose as a completed review.
                result = {"error": "Call the appropriate completion or investigation tool."}
                receipt = {
                    "type": "receipt",
                    "request_id": request_id,
                    "index": 0,
                    "message": messages_to_dict([HumanMessage(content=_json(result))])[0],
                    "source_ids": [],
                    "ok": False,
                }
                self._append(session, receipt, f"receipt:{request_id}:0")
                calls_signature = _json(result)
                all_failed = True
            else:
                outcomes = []
                for index, call in enumerate(calls):
                    existing = next(
                        (
                            e
                            for e in self._events(session)
                            if e.get("type") == "receipt" and e["request_id"] == request_id and e["index"] == index
                        ),
                        None,
                    )
                    if existing:
                        if existing.get("finished"):
                            result = existing["result"]
                            self._append(session, {"type": "complete", "result": result}, "complete")
                            return result
                        outcomes.append(existing)
                        continue
                    try:
                        result, source_ids, finished = await self._tool(
                            role, session, task_id, call["name"], call.get("args") or {}, f"{request_id}:{index}"
                        )
                        ok = not (isinstance(result, dict) and result.get("error"))
                    except (ValueError, KeyError, TypeError) as exc:
                        result, source_ids, finished, ok = {"error": str(exc)}, [], False, False
                    note_watermarks = {}
                    if call["name"] == "read_notes" and ok:
                        for note in result.get("notes", []):
                            note_watermarks[note["task_id"]] = max(note_watermarks.get(note["task_id"], 0), note["seq"])
                    receipt = {
                        "type": "receipt",
                        "request_id": request_id,
                        "index": index,
                        "message": messages_to_dict([ToolMessage(content=_json(result), tool_call_id=call["id"])])[0],
                        "source_ids": source_ids,
                        "ok": ok,
                        "finished": finished,
                        "result": result,
                        "note_watermarks": note_watermarks,
                    }
                    self._append(session, receipt, f"receipt:{request_id}:{index}")
                    outcomes.append(receipt)
                    if finished:
                        self._append(session, {"type": "complete", "result": result}, "complete")
                        return result
                all_failed = all(not e["ok"] for e in outcomes)
                calls_signature = _json([{"name": c["name"], "args": c.get("args")} for c in calls])
            cycle = self._read_cycle(session, calls, outcomes) if calls else None
            self._append(
                session,
                {"type": "exchange_done", "request_id": request_id, **({"reading_cycle": cycle} if cycle else {})},
                "done:" + request_id,
            )
            if cycle and cycle["status"] == "halt":
                return self._cycle_result(session)
            if all_failed:
                failures = failures + 1 if failed == calls_signature else 1
                failed = calls_signature
                if failures >= 3:
                    return {
                        "status": "partial",
                        "reason": "repeated identical failed tool batch",
                        "batch": calls_signature,
                    }
            else:
                failures, failed = 0, None

    async def _tool(self, role, session, task_id, name, args, invocation):
        permitted = [
            *self.gateway.tool_specs,
            *COMMON,
            *(LEAD if role == "lead" else VERIFY if role == "verifier" else RESEARCH),
        ]
        schema = next((item["function"]["parameters"] for item in permitted if item["function"]["name"] == name), None)
        if schema is None:
            raise ValueError(f"unknown or unavailable tool: {name}")
        errors = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda error: str(list(error.path)))
        if errors:
            raise ValueError("invalid tool arguments: " + errors[0].message)
        if name in {x["function"]["name"] for x in self.gateway.tool_specs}:
            raw = await self.gateway(name, args)
            metadata = {k: v for k, v in raw.items() if k not in {"content", "kind", "side"}}
            side = raw["side"]
            if side in {"head", "base"} and raw.get("revision") != getattr(self.store.binding, side + "_sha"):
                raise ValueError("source revision does not match pinned binding")
            original = (
                raw["kind"] in {"source", "documentation", "issue", "test"}
                and not raw.get("metadata", {}).get("navigation_only")
                and raw.get("metadata", {}).get("status") == "available"
            )
            provenance = {"kind": raw["kind"], **metadata}
            if side == "external" and original:
                external = self.store.binding.source_environment.get("external", {})
                if raw.get("path") not in external:
                    raise ValueError("external original is not in frozen binding")
                provenance["provenance"] = external[raw["path"]]
            source = self.store.put_source(
                "original" if original else "navigation", side, raw["content"].encode("utf-8"), provenance
            )
            result = {"source_id": source["id"], **raw}
            if raw.get("metadata", {}).get("status") == "unavailable":
                result["error"] = raw.get("metadata", {}).get("reason", "source unavailable")
            return result, [source["id"]], False
        if name == "set_progress":
            self._append(session, {"type": "progress", "value": args}, "work:" + invocation)
            return {"status": "saved"}, [], False
        if name == "read_progress":
            return (
                {"history": [event["value"] for event in self._events(session) if event.get("type") == "progress"]},
                [],
                False,
            )
        if name in {"retain_source", "release_source"}:
            source_id = args["source_id"]
            self.store.get_source(source_id)
            if name == "retain_source" and source_id not in self.store.delivered_sources(session):
                raise ValueError("source must first be delivered to this session")
            self._append(
                session,
                {"type": "retain" if name == "retain_source" else "release", "source_id": source_id},
                "work:" + invocation,
            )
            return {"status": "saved", "source_id": source_id}, [], False
        if name == "list_sources":
            return (
                {"sources": [{k: v for k, v in item.items() if k != "content"} for item in self.store.list_sources()]},
                [],
                False,
            )
        if role == "lead" and name == "list_findings":
            return {"findings": self.store.list_findings()}, [], False
        if name == "read_saved_source":
            source = self.store.get_source(args["source_id"])
            return {**source, "content": source["content"].decode("utf-8")}, [args["source_id"]], False
        if name == "read_notes":
            return {"notes": self.store.list_notes(args.get("task_id"))}, [], False
        if name == "changed_notes":
            return {"tasks": self.store.changed_note_tasks(session)}, [], False
        if name == "add_note":
            return (
                self.store.add_note(
                    args.get("task_id") or task_id,
                    session,
                    args["text"],
                    args.get("source_ids", []),
                    note_id="note_" + hashlib.sha256(invocation.encode()).hexdigest()[:24],
                ),
                [],
                False,
            )
        if role == "lead" and name in {"submit_finding", "update_finding"}:
            if not set(args["source_ids"]).issubset(self.store.delivered_sources(session)):
                raise ValueError("source not delivered to lead session")
            if name == "submit_finding":
                finding = self.store.create_finding(args["claim"], args["source_ids"], session)
            else:
                finding = self.store.update_finding(
                    args["finding_id"], args["claim"], args["source_ids"], args.get("same_defect_reason")
                )
            existing = [
                v
                for v in self.store.list_verifications()
                if v["finding_id"] == finding["id"] and v["finding_revision"] == finding["revision"]
            ]
            if not existing:
                verifier_session = self.store.create_session("verifier")
                verification = self.store.create_verification(finding["id"], verifier_session["id"])
            else:
                verification = existing[-1]
            result = await self._verify(verification) if verification["status"] == "open" else verification
            return {"finding_id": finding["id"], "revision": finding["revision"], "verification": result}, [], False
        if role == "lead" and name == "challenge_finding":
            if not args["source_ids"] or not set(args["source_ids"]).issubset(self.store.delivered_sources(session)):
                raise ValueError("counterevidence must first be delivered to the lead session")
            ident = "challenge_" + hashlib.sha256(invocation.encode()).hexdigest()[:24]
            current = self.store.get_finding(args["finding_id"])
            all_sources = list(dict.fromkeys([*current["source_ids"], *args["source_ids"]]))
            finding = self.store.challenge_finding(args["finding_id"], args["reason"], all_sources, challenge_id=ident)
            verifier_session = self.store.create_session("verifier", session_id=ident + "_session")
            verification = self.store.create_verification(finding["id"], verifier_session["id"])
            result = await self._verify(verification) if verification["status"] == "open" else verification
            return {"finding_id": finding["id"], "revision": finding["revision"], "verification": result}, [], False
        if role == "lead" and name == "retry_verification":
            finding = self.store.get_finding(args["finding_id"])
            ident = "retry_" + hashlib.sha256(invocation.encode()).hexdigest()[:24]
            existing = [v for v in self.store.list_verifications() if v["session_id"] == ident]
            if existing:
                verification = existing[-1]
            else:
                if finding["state"] != "needs_evidence":
                    raise ValueError("only a finding with missing evidence can be retried")
                verifier_session = self.store.create_session("verifier", session_id=ident)
                verification = self.store.create_verification(finding["id"], verifier_session["id"])
            result = await self._verify(verification) if verification["status"] == "open" else verification
            return result, [], False
        if role == "lead" and name == "delegate_research":
            if not args["question"].strip():
                raise ValueError("research question cannot be empty")
            # Deterministic task ID makes replay after a crash safe.
            ident = "research_" + hashlib.sha256(invocation.encode()).hexdigest()[:24]
            existing = [r for r in self.store.list_research() if r["id"] == ident]
            if existing:
                research = existing[0]
            else:
                helper = self.store.create_session("research", session_id=ident + "_session")
                research = self.store.create_research(args["question"], helper["id"], task_id=ident)
            result = await self._research(research) if research["status"] == "open" else research
            return {"task_id": ident, "result": result}, [], False
        if role == "lead" and name == "read_research":
            return self.store.get_research(args["task_id"]), [], False
        if role == "lead" and name == "finish_review":
            return {"status": "partial" if args["gaps"] else "complete", **args}, [], True
        if role == "research" and name == "finish_research":
            if not set(args["source_ids"]).issubset(self.store.delivered_sources(session)):
                raise ValueError("research source not delivered to this session")
            self.store.complete_research(task_id, args)
            return args, [], True
        if role == "verifier" and name == "submit_verdict":
            result = self.store.record_verdict(
                task_id,
                args["verdict"],
                args["source_ids"],
                missing_fact=args.get("missing_fact"),
                attempted=args.get("attempted"),
                recovery_condition=args.get("recovery_condition"),
                reason=args.get("reason"),
            )
            if self.publisher:
                await self.publisher()
            return result, [], True
        raise ValueError(f"unknown or unavailable tool: {name}")
