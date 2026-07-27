from reviewforge.core.json_output import extract_json_value, strip_reasoning_blocks


def test_extracts_fenced_envelope_after_reasoning_with_code_braces() -> None:
    content = """<think>
void run() { call(); }
{"type":"function","name":"read_file"}
</think>
```json
{"findings":[{"file":"app.py","line":7}]}
```"""

    assert extract_json_value(content, required_key="findings") == {"findings": [{"file": "app.py", "line": 7}]}


def test_skips_textual_tool_call_objects_when_findings_are_required() -> None:
    content = """<think>Use tools.</think>
{"type":"function","name":"read_file","arguments":{"file_path":"app.py"}}
{"findings":[]}"""

    assert extract_json_value(content, required_key="findings") == {"findings": []}


def test_extracts_planner_tasks_after_reasoning() -> None:
    content = """<think>if (changed) { inspect(); }</think>
```json
{"tasks":[{"reviewer":"correctness","files":["app.py"]}]}
```"""

    assert extract_json_value(content, required_key="tasks", allow_list=False) == {
        "tasks": [{"reviewer": "correctness", "files": ["app.py"]}]
    }


def test_extracts_verdict_after_reasoning_and_skips_tool_call() -> None:
    content = """<think>if (uncertain) { inspect(); }</think>
{"type":"function","name":"search_code","arguments":{"query":"unsafe"}}
{"verdict":"confirmed","confidence":0.91}"""

    assert extract_json_value(
        content,
        required_key="verdict",
        allow_list=False,
    ) == {"verdict": "confirmed", "confidence": 0.91}


def test_strip_reasoning_blocks_rejects_non_text() -> None:
    assert strip_reasoning_blocks({"type": "text"}) == ""


def test_extracts_json_from_anthropic_text_blocks() -> None:
    content = [
        {"type": "thinking", "thinking": "private reasoning"},
        {"type": "text", "text": '{"findings":[]}'},
    ]

    assert extract_json_value(content, required_key="findings") == {"findings": []}
