from langchain_anthropic import ChatAnthropic
from langchain_core.tools import StructuredTool

from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM


def test_tracked_llm_preserves_anthropic_tool_schema() -> None:
    async def read_file(file_path: str) -> str:
        """Read a file."""

        return file_path

    inner = ChatAnthropic(
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/anthropic",
        anthropic_api_key="test-key",
    )
    tracked = TrackedChatLLM(inner=inner, ctx=RunContext(), agent_name="test")
    tool = StructuredTool.from_function(
        coroutine=read_file,
        name="read_file",
        description="Read a file.",
    )

    bound = tracked.bind_tools([tool])

    assert bound.kwargs["tools"][0]["name"] == "read_file"
    assert "function" not in bound.kwargs["tools"][0]
