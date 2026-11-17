"""Provider conversation contracts for the new delivery kernel only.

DeepSeek thinking tool rounds require the provider's own continuation field.
Historical v4.2 deliberately strips it; do not change that adapter or silently
reuse its workspace-only conversation policy for this new native session.
"""

from urllib.parse import urlsplit

from reviewforge.engine.model_router import ModelRouter, _is_minimax

AGENTS = {"lead": "v42_reviewer", "research": "v42_reviewer", "verifier": "v42_verifier"}


def conversation_protocols(router):
    policies = {}
    for role, agent in AGENTS.items():
        if not isinstance(router, ModelRouter):
            policies[role] = {"provider": "generic", "native_history": False}
            continue
        effective = router.effective(agent)
        url = urlsplit(effective["base_url"])
        go = (
            url.scheme == "https"
            and url.hostname == "opencode.ai"
            and url.port in {None, 443}
            and url.path.rstrip("/") == "/inference/go/openai/v1"
        )
        policies[role] = {
            "provider": "opencode-go" if go else "generic",
            "native_history": go and effective["model"] in {"deepseek-v4.1-flash", "deepseek-v4-pro"},
        }
        if policies[role]["native_history"]:
            policies[role]["history_layout"] = "native-append-1"
    return policies


def raw_models(router, *, request_timeout=None):
    policies = conversation_protocols(router)
    models = {}
    for role, name in (("lead", "v42_reviewer"), ("verifier", "v42_verifier")):
        if isinstance(router, ModelRouter):
            effective = router.effective(name)
            # The existing non-streaming compatibility adapter preserves the
            # provider-returned reasoning_content without enabling thinking or
            # fabricating any state. Retry/metering remain outside the SDK.
            stream = not policies[role]["native_history"] and not _is_minimax(effective["base_url"], effective["model"])
            models[name] = router.get_llm(name, max_retries=0, stream_transport=stream, request_timeout=request_timeout)
        else:
            models[name] = router.get_llm(name)
    return models
