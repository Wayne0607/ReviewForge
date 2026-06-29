"""Token Tracker — wraps LLM calls to capture and persist token usage.

Wraps ChatOpenAI instances to automatically record token consumption
to the database after each call.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

from reviewforge.core.database import Database

logger = logging.getLogger(__name__)


class RunContext:
    """Mutable context shared across all tracked LLMs for a single run."""

    def __init__(self) -> None:
        self.run_id: str = ""
        self.db: Database | None = None

    def set(self, run_id: str, db: Database) -> None:
        self.run_id = run_id
        self.db = db


class TrackedChatLLM(BaseChatModel):
    """Wrapper around a ChatOpenAI that records token usage to the database.

    Uses a shared RunContext so the run_id can be updated per-run
    without recreating the wrapper.
    """

    _inner: BaseChatModel
    _ctx: RunContext
    _agent_name: str

    class Config:
        arbitrary_types_allowed = True

    def __init__(
        self,
        inner: BaseChatModel,
        ctx: RunContext,
        agent_name: str,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._ctx = ctx
        self._agent_name = agent_name

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = await self._inner._agenerate(messages, stop, run_manager, **kwargs)

        # Extract token usage from LangChain response
        usage = result.llm_output or {}
        token_usage = usage.get("token_usage", {})

        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)
        total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)
        model = usage.get("model_name", "")

        # Also check usage_metadata (newer LangChain versions)
        if total_tokens == 0 and result.generations:
            msg = result.generations[0][0].message if result.generations[0] else None
            if msg and hasattr(msg, "usage_metadata") and msg.usage_metadata:
                um = msg.usage_metadata
                prompt_tokens = um.get("input_tokens", prompt_tokens)
                completion_tokens = um.get("output_tokens", completion_tokens)
                total_tokens = um.get("total_tokens", prompt_tokens + completion_tokens)

        # Record to DB
        if total_tokens > 0 and self._ctx.db and self._ctx.run_id:
            try:
                await self._ctx.db.record_token_usage(
                    run_id=self._ctx.run_id,
                    agent_name=self._agent_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    model=model,
                )
                logger.info(
                    f"[Token] {self._agent_name}: {total_tokens} tokens "
                    f"(prompt={prompt_tokens}, completion={completion_tokens})"
                )
            except Exception as e:
                logger.warning(f"Failed to record token usage: {e}")

        return result

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._inner._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return f"tracked({self._inner._llm_type})"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return self._inner._identifying_params

    def bind_tools(self, tools, **kwargs):
        return self._inner.bind_tools(tools, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return self._inner.with_structured_output(schema, **kwargs)


def create_tracked_llm(
    inner: BaseChatModel,
    ctx: RunContext,
    agent_name: str,
) -> TrackedChatLLM:
    """Create a tracked LLM wrapper."""
    return TrackedChatLLM(inner=inner, ctx=ctx, agent_name=agent_name)
