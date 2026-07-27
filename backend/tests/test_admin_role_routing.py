"""End-to-end admin API tests for the 5-role LLM settings.

Covers:
- ``POST /llm-settings`` accepts a ``roles`` payload and stores it.
- ``GET /llm-settings`` returns per-role safe metadata only.
- The test endpoint validates every distinct effective endpoint.
- ``POST /llm-settings/reset`` clears all role overrides.
- The roles payload never echoes the API key back through any endpoint.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient


def _write_config(tmp_path: Path, *, role_overrides_yaml: str = "") -> Path:
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        "\n".join(
            [
                "llm:",
                '  base_url: "https://startup.example/v1"',
                '  api_key: "startup-secret"',
                '  model: "startup-model"',
                "  profiles:",
                "    fast:",
                '      model: "startup-fast"',
                "    accurate:",
                '      model: "startup-accurate"',
                f'events_dir: "{(tmp_path / "events").as_posix()}"',
                role_overrides_yaml,
            ]
        ),
        encoding="utf-8",
    )
    return config_path


@contextmanager
def _make_client(
    tmp_path: Path,
    monkeypatch,
    *,
    role_overrides_yaml: str = "",
) -> Iterator[TestClient]:
    monkeypatch.setenv("REVIEWFORGE_MOCK", "1")
    monkeypatch.setenv("REVIEWFORGE_API_TOKEN", "test-token")
    # Test URLs use non-resolving private hostnames; the runtime security
    # check would reject those unless the test explicitly opts in.
    monkeypatch.setenv("REVIEWFORGE_ALLOW_PRIVATE_LLM_ENDPOINTS", "1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("REVIEWFORGE_MODEL", raising=False)
    config_path = _write_config(tmp_path, role_overrides_yaml=role_overrides_yaml)

    from reviewforge.app import create_app

    app = create_app(str(config_path))
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        yield client


def test_admin_get_returns_role_safe_metadata(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        data = client.get("/api/v1/admin/llm-settings").json()
    assert data["source"] == "startup"
    assert set(data["roles"].keys()) == {
        "planner",
        "fast_review",
        "deep_review",
        "verifier",
        "publication_gate",
    }
    for role in data["roles"].values():
        # Effective endpoint metadata is safe; no raw key material.
        assert "api_key" not in role
        assert isinstance(role["api_key_configured"], bool)
        # All override flags are False in startup config (no role overrides).
        assert role["overrides_base_url"] is False
        assert role["overrides_model"] is False
        assert role["overrides_api_key"] is False
    blob = json.dumps(data)
    assert "startup-secret" not in blob


def test_admin_save_with_role_overrides_hot_swaps(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "deep_review": {
                    "base_url": "http://127.0.0.1:11435/v1",
                    "model": "deep-model",
                    "api_key": "deep-secret",
                },
                "publication_gate": {
                    "base_url": "",
                    "model": "gate-model",
                    "api_key": "",
                },
            },
        }
        resp = client.post("/api/v1/admin/llm-settings", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["settings"]["source"] == "console"
        roles = body["settings"]["roles"]
        assert roles["deep_review"]["overrides_base_url"] is True
        assert roles["deep_review"]["overrides_model"] is True
        assert roles["deep_review"]["overrides_api_key"] is True
        assert roles["deep_review"]["api_key_configured"] is True
        assert roles["publication_gate"]["overrides_model"] is True
        assert roles["publication_gate"]["overrides_base_url"] is False
        # Publication gate keeps the global key when blank.
        assert roles["publication_gate"]["api_key_configured"] is True
        # Ensure no raw key material in the GET response.
        get_data = client.get("/api/v1/admin/llm-settings").json()
    blob = json.dumps(get_data)
    assert "console-secret" not in blob
    assert "deep-secret" not in blob


def test_admin_test_endpoint_validates_distinct_role_endpoints(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "deep_review": {
                    "base_url": "http://127.0.0.1:11435/v1",
                    "model": "deep-model",
                    "api_key": "deep-secret",
                },
                "publication_gate": {
                    "base_url": "http://localhost:11436/v1",
                    "model": "gate-model",
                    "api_key": "gate-secret",
                },
            },
        }
        resp = client.post("/api/v1/admin/llm-settings/test", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Mock mode returns latency=0 but must enumerate the models.
        models = set(body["tested_models"])
        assert {"console-model", "deep-model", "gate-model"}.issubset(models)
        # The roles block in the test response must also be safe.
        assert "deep-secret" not in json.dumps(body.get("roles", {}))
        assert "gate-secret" not in json.dumps(body.get("roles", {}))


def test_admin_reset_clears_role_overrides(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        # First save with role overrides
        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "deep_review": {
                    "base_url": "http://127.0.0.1:11435/v1",
                    "model": "deep-model",
                    "api_key": "deep-secret",
                },
            },
        }
        save = client.post("/api/v1/admin/llm-settings", json=payload)
        assert save.status_code == 200, save.text
        assert save.json()["settings"]["roles"]["deep_review"]["overrides_model"] is True
        # Then reset.
        reset = client.post("/api/v1/admin/llm-settings/reset", json={})
        assert reset.status_code == 200, reset.text
        assert reset.json()["settings"]["source"] == "startup"
        for role in reset.json()["settings"]["roles"].values():
            assert role["overrides_base_url"] is False
            assert role["overrides_model"] is False
            assert role["overrides_api_key"] is False


def test_admin_role_payload_preserves_global_key_for_blank_role(tmp_path, monkeypatch):
    """When a role leaves the API key blank, the global key is reused."""
    with _make_client(tmp_path, monkeypatch) as client:
        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "deep_review": {
                    "base_url": "http://127.0.0.1:11435/v1",
                    "model": "deep-model",
                    "api_key": "",
                },
            },
        }
        resp = client.post("/api/v1/admin/llm-settings", json=payload)
        assert resp.status_code == 200, resp.text
    # When a role leaves key blank, the configured/last4 metadata follows
    # the global key (still safe to expose).
    role = resp.json()["settings"]["roles"]["deep_review"]
    assert role["api_key_configured"] is True
    assert role["api_key_last4"] == "cret"  # last 4 of "console-secret"
    # No raw key material exposed.
    assert "console-secret" not in json.dumps(resp.json())


def test_admin_role_payload_filters_unknown_role(tmp_path, monkeypatch):
    """Unknown role names are silently dropped, not stored."""
    with _make_client(tmp_path, monkeypatch) as client:
        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "not_a_role": {
                    "base_url": "http://localhost:11437/v1",
                    "model": "x",
                    "api_key": "k",
                },
                "verifier": {
                    "base_url": "http://127.0.0.1:11438/v1",
                    "model": "v",
                    "api_key": "k",
                },
            },
        }
        resp = client.post("/api/v1/admin/llm-settings", json=payload)
        assert resp.status_code == 200, resp.text
        roles = resp.json()["settings"]["roles"]
    assert "not_a_role" not in roles
    # Known role survives.
    assert roles["verifier"]["overrides_model"] is True


def test_admin_global_only_save_preserves_existing_role_override(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        first = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "verifier": {
                    "base_url": "http://127.0.0.1:11438/v1",
                    "model": "verify-model",
                    "api_key": "verify-1234",
                }
            },
        }
        assert client.post("/api/v1/admin/llm-settings", json=first).status_code == 200
        second = {
            **first,
            "model": "new-global-model",
            "api_key": "",
            "roles": {},
        }
        response = client.post("/api/v1/admin/llm-settings", json=second)
        assert response.status_code == 200, response.text
        verifier = response.json()["settings"]["roles"]["verifier"]
        assert verifier["model"] == "verify-model"
        assert verifier["api_key_last4"] == "1234"
        assert verifier["overrides_api_key"] is True


def test_admin_can_reset_one_role_to_global(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "fast_model": "",
            "accurate_model": "",
            "roles": {
                "verifier": {
                    "base_url": "http://127.0.0.1:11438/v1",
                    "model": "verify-model",
                    "api_key": "verify-secret",
                }
            },
        }
        assert client.post("/api/v1/admin/llm-settings", json=payload).status_code == 200
        payload["roles"] = {"verifier": {"reset": True}}
        payload["api_key"] = ""
        response = client.post("/api/v1/admin/llm-settings", json=payload)
        assert response.status_code == 200, response.text
        verifier = response.json()["settings"]["roles"]["verifier"]
        assert verifier["model"] == "console-model"
        assert verifier["overrides_model"] is False
        assert verifier["overrides_api_key"] is False
