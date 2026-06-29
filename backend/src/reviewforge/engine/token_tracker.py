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


class TrackedChatLLM(BaseChatModel):
    """Wrapper around a ChatOpenAI that records token usage to the database."""

    _inner: BaseChatModel
    _db: Database
    _agent_name: str
    _run_id: str

    class Config:
        arbitrary_types_allowed = True

    def __init__(
        self,
        inner: BaseChatModel,
        db: Database,
        agent_name: str,
        run_id: str,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._db = db
        self._agent_name = agent_name
        self._run_id = run_id

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = await self._inner._agenerate(messages, stop, run_manager, **kwargs)

        # Extract token usage
        usage = result.llm_output or {}
        token_usage = usage.get("token_usage", {})

        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)
        total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)
        model = usage.get("model_name", "")

        # Record to DB
        if total_tokens > 0:
            try:
                await self._db.record_token_usage(
                    run_id=self._run_id,
                    agent_name=self._agent_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    model=model,
                )
                logger.debug(
                    f"Token tracked: {self._agent_name} = {total_tokens} "
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
        # Sync fallback (should not be used in async context)
        return self._inner._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return f"tracked({self._inner._llm_type})"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return self._inner._identifying_params

    # Delegate common attributes
    def bind_tools(self, tools, **kwargs):
        return self._inner.bind_tools(tools, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return self._inner.with_structured_output(schema, **kwargs)


def wrap_llm(llm: BaseChatModel, db: Database, agent_name: str, run_id: str) -> TrackedChatLLM:
    """Convenience function to wrap an LLM with token tracking."""
    return TrackedChatLLM(inner=llm, db=db, agent_name=agent_name, run_id=run_id)
