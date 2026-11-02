"""Concurrent immutable blob installation must verify the winning bytes."""

import hashlib
from pathlib import Path

import pytest
from test_v42_case_counterevidence import board as board

from reviewforge.review_v42.contracts import BindingError
from reviewforge.review_v42.store import _install_verified_blob


@pytest.mark.parametrize("label", ["evidence", "turn audit"])
@pytest.mark.parametrize("winner", ["same", "corrupt", "missing"])
def test_windows_install_conflict_requires_a_verified_winner(tmp_path, monkeypatch, label, winner):
    content = b"original immutable body\n"
    digest = hashlib.sha256(content).hexdigest()
    temp = tmp_path / "new.tmp"
    destination = tmp_path / digest
    temp.write_bytes(content)

    def race(source, target):
        if winner == "same":
            Path(target).write_bytes(content)
        elif winner == "corrupt":
            Path(target).write_bytes(b"not the same content")
        raise PermissionError("simulated Windows sharing violation")

    monkeypatch.setattr("reviewforge.review_v42.store.os.link", race)
    if winner == "same":
        _install_verified_blob(temp, destination, digest, label=label)
        assert destination.read_bytes() == content
    else:
        error = BindingError if winner == "corrupt" else PermissionError
        with pytest.raises(error):
            _install_verified_blob(temp, destination, digest, label=label)


@pytest.mark.parametrize("kind", ["evidence", "audit"])
@pytest.mark.parametrize("winner", ["same", "corrupt", "missing"])
def test_blob_callers_clean_temp_and_commit_only_if_winner_matches(board, monkeypatch, kind, winner):
    store, task, _, _, _, _ = board

    def race(source, target):
        if winner == "same":
            Path(target).write_bytes(Path(source).read_bytes())
        elif winner == "corrupt":
            Path(target).write_bytes(b"not the same content")
        raise PermissionError("simulated Windows sharing violation")

    monkeypatch.setattr("reviewforge.review_v42.store.os.link", race)

    def write():
        if kind == "evidence":
            return store.put_evidence(
                b"new evidence\n",
                revision="head",
                side="head",
                kind="source",
                path="new.py",
                start_line=1,
                end_line=1,
                metadata={"workspace_digest": "ws"},
            )
        return store.append_turn_audit(
            task.id, task.lease_token, "concurrent", "request", [{"type": "human", "data": {"content": "new"}}], now=1
        )

    if winner == "same":
        result = write()
        assert result
    else:
        error = BindingError if winner == "corrupt" else PermissionError
        with pytest.raises(error):
            write()
    assert list(store.blob_dir.glob("*.tmp")) == []
    assert list(store.audit_dir.glob("*.tmp")) == []
    if kind == "audit" and winner != "same":
        with store._connect() as db:
            assert db.execute("SELECT 1 FROM turn_audit WHERE turn_id='concurrent'").fetchone() is None
