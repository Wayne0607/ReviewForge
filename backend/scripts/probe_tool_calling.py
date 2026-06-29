"""探测 provider 是否支持 tool calling。退出码 0=支持，1=不支持。

用法：
  python scripts/probe_tool_calling.py          # 读 .env，连真实 LLM
  python scripts/probe_tool_calling.py --mock    # 用 MockChatLLM（需先实现 bind_tools）
"""

from __future__ import annotations

import asyncio
import os
import sys

# 加载 .env
env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def main() -> None:
    use_mock = "--mock" in sys.argv

    if use_mock:
        from reviewforge.engine.mock_llm import MockChatLLM

        llm = MockChatLLM()
        print("[mock] MockChatLLM (needs bind_tools support)")
    else:
        from reviewforge.core.config import ReviewForgeConfig
        from reviewforge.engine.model_router import ModelRouter

        cfg = ReviewForgeConfig.load()
        router = ModelRouter(cfg.llm)
        llm = router.get_llm("security_reviewer")
        print(f"[real] model={cfg.llm.model} base_url={cfg.llm.base_url}")

    from langchain_core.messages import HumanMessage
    from langchain_core.tools import StructuredTool

    def get_weather(city: str) -> str:
        """query weather for a city."""
        return f"{city}: sunny, 25C"

    tool = StructuredTool.from_function(
        func=get_weather,
        name="get_weather",
        description="query weather for a city",
    )

    try:
        bound_llm = llm.bind_tools([tool])
        resp = await bound_llm.ainvoke([HumanMessage(content="Beijing weather? Use get_weather tool.")])
        calls = getattr(resp, "tool_calls", None)
        print(f"tool_calls: {calls}")
        if calls:
            print("RESULT: SUPPORTED")
            sys.exit(0)
        else:
            print("RESULT: NOT SUPPORTED (no tool_calls in response)")
            sys.exit(1)
    except NotImplementedError:
        print("RESULT: NOT SUPPORTED (bind_tools not implemented)")
        sys.exit(1)
    except Exception as e:
        print(f"RESULT: ERROR - {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
