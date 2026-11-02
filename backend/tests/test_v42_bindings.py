"""Failure cases for run binding and restart coordination."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from reviewforge.core.state import StateStore
from reviewforge.review_v42.integration import PhysicalMeter, _load_bound_diff, _model_signature, _process_lock


class DiffGithub:
    def __init__(self, before, after, files=None):
        self.values = iter([before, after])
        self.files = files if files is not None else [{"filename": "a.py", "patch": "new patch"}]
        self.file_calls = 0

    async def get_pr_info(self, repo, number):
        return next(self.values)

    async def get_pr_files(self, repo, number):
        self.file_calls += 1
        return self.files


def pr(head="head", base="base", count=1):
    return {"head": {"sha": head}, "base": {"sha": base}, "changed_files": count}


def prepared(github):
    state = StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="head",
        base_sha="base",
        files_changed=["original.py"],
        file_diffs={"original.py": "original"},
    )
    return SimpleNamespace(_gateway=SimpleNamespace(_github=github)), state


@pytest.mark.parametrize("bad", [pr(head="other"), pr(base="other")])
async def test_pre_capture_drift_rejects_without_fetching_or_overwriting(bad):
    github = DiffGithub(bad, pr())
    orchestrator, state = prepared(github)
    with pytest.raises(ValueError, match="base/head changed"):
        await _load_bound_diff(orchestrator, state)
    assert github.file_calls == 0
    assert state.files_changed == ["original.py"] and state.file_diffs == {"original.py": "original"}


@pytest.mark.parametrize("bad", [pr(head="other"), pr(base="other")])
async def test_post_capture_drift_rejects_without_overwriting(bad):
    github = DiffGithub(pr(), bad)
    orchestrator, state = prepared(github)
    with pytest.raises(ValueError, match="base/head changed"):
        await _load_bound_diff(orchestrator, state)
    assert github.file_calls == 1
    assert state.files_changed == ["original.py"] and state.file_diffs == {"original.py": "original"}


async def test_diff_requires_complete_unique_manifest_and_accepts_matching_snapshots():
    for files, count in (([{"filename": "a"}, {"filename": "a"}], 2), ([{"filename": "a"}], 2)):
        orchestrator, state = prepared(DiffGithub(pr(count=count), pr(count=count), files))
        with pytest.raises(ValueError, match="manifest"):
            await _load_bound_diff(orchestrator, state)
        assert state.file_diffs == {"original.py": "original"}
    orchestrator, state = prepared(DiffGithub(pr(), pr()))
    await _load_bound_diff(orchestrator, state)
    assert state.files_changed == ["a.py"] and state.file_diffs == {"a.py": "new patch"}


def test_meter_preserves_bad_valid_json_as_unknown_and_rejects_foreign_run(tmp_path):
    path = tmp_path / "requests.jsonl"
    binding = {"run": "one", "repo": "r", "base": "b", "head": "h"}
    meter = PhysicalMeter(path, binding)
    meter.append({"logical_request_id": "ok", "attempt": 1, "usage": {"input_tokens": 3, "output_tokens": 1}})
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{}\n[]\n{"logical_request_id":"partial"}\n')
    rows = meter.requests()
    assert len(rows) == 4 and sum(bool(x.get("meter_fragment_unknown")) for x in rows) == 3
    assert next(x for x in rows if x.get("logical_request_id") == "ok")["usage"]["input_tokens"] == 3
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"logical_request_id": "foreign", "attempt": 1, "binding": {**binding, "head": "different"}})
            + "\n"
        )
    with pytest.raises(ValueError, match="another run"):
        meter.requests()


def test_process_lock_is_nonblocking_and_released_after_crash(tmp_path):
    path = tmp_path / "run.lock"
    script = (
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "from reviewforge.review_v42.integration import _process_lock\n"
        "with _process_lock(Path(sys.argv[1])):\n"
        " print(os.getpid(),flush=True)\n"
        " time.sleep(60)\n"
    )
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    process = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-u", "-c", script, str(path)],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    try:
        holder_pid = int(process.stdout.readline().strip())
        assert holder_pid == process.pid, "Popen must own the actual lock-holding process"
        with pytest.raises(BlockingIOError):
            with _process_lock(path):
                pass
        process.kill()
        process.wait(timeout=10)
        with _process_lock(path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("temperature", 0.7),
        ("max_tokens", 1024),
        ("top_p", 0.8),
        ("reasoning_effort", "high"),
        ("extra_body", {"thinking": True}),
    ],
)
def test_model_signature_changes_when_effective_options_change(field, new_value):
    baseline = SimpleNamespace(
        model="same", temperature=0.1, max_tokens=None, top_p=1.0, reasoning_effort="low", extra_body={}
    )
    altered = SimpleNamespace(**vars(baseline))
    setattr(altered, field, new_value)
    assert _model_signature(baseline) != _model_signature(altered)
    assert _model_signature(baseline) == _model_signature(SimpleNamespace(**vars(baseline)))
