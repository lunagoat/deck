"""Isolated test of distinct rendering for thought, response, tool, and tool_output events.

Run:
    XDG_RUNTIME_DIR=/tmp ~/Projects/deck/.venv/bin/python tests/smoke_rendering.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402,F401
sys.path.insert(0, str(ROOT))

from textual.widgets import Markdown, Static  # noqa: E402
from textual.widgets._markdown import MarkdownTable, MarkdownBulletList  # noqa: E402

from deck.app import DeckApp  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


async def main():
    work = _env.BASE / "renderwork"
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)

    app = DeckApp()
    app.hub.serve = AsyncMock()
    app._refresh_usage_bg = AsyncMock()
    async with app.run_test(size=(140, 45)) as pilot:
        run = app.hub.attach("claude", "sess-render", str(work), "render test", [])
        app.show_task(run.task)
        await pilot.pause(0.2)
        pane = app.panes[run.task.id]["claude"]

        # 1. Test Thought block and streaming
        run.emit_delta("Thinking about ", "thought")
        run.emit_delta("how to solve this problem.\n", "thought")
        pane.refresh_all()
        await pilot.pause(0.3)

        thoughts = pane.query(".thought-block")
        check("thought block mounted", len(thoughts) == 1, str(len(thoughts)))
        check("thought block has border_title 'thought'", bool(thoughts and thoughts[0].border_title == "thought"),
              str(thoughts[0].border_title if thoughts else None))
        check("thought block is Markdown", bool(thoughts and isinstance(thoughts[0], Markdown)))

        # Stream additional thought lines
        run.emit_delta("- step 1: analyze\n- step 2: execute\n", "thought")
        run.flush()
        pane.refresh_all()
        await pilot.pause(0.3)

        thoughts = pane.query(".thought-block")
        check("streaming thought preserves single block", len(thoughts) == 1, str(len(thoughts)))
        check("thought markdown list rendered", len(pane.query(".thought-block MarkdownBulletList")) >= 1)

        # 2. Test Tool / Command event
        run.emit("tool", "pytest tests/smoke_detect.py")
        pane.refresh_all()
        await pilot.pause(0.3)

        tools = pane.query(".tool-block")
        check("tool block mounted", len(tools) == 1, str(len(tools)))
        check("tool block has tool/command title", bool(tools and "tool" in tools[0].border_title.lower()),
              str(tools[0].border_title if tools else None))
        check("tool block text formatted with command marker", bool(tools and "pytest" in str(tools[0].render())),
              str(tools[0].render() if tools else None))

        # 3. Test Tool Output event and streaming
        run.emit("tool_output", "PASS test_one\n")
        pane.refresh_all()
        await pilot.pause(0.3)

        outputs = pane.query(".tool-output-block")
        check("tool_output block mounted", len(outputs) == 1, str(len(outputs)))
        check("tool_output block has output title", bool(outputs and "output" in outputs[0].border_title.lower()),
              str(outputs[0].border_title if outputs else None))

        # Stream more output into the same tool_output block
        run.emit("tool_output", "PASS test_two\n2/2 passed\n")
        pane.refresh_all()
        await pilot.pause(0.3)

        outputs = pane.query(".tool-output-block")
        check("consecutive tool_output updates the same block", len(outputs) == 1, str(len(outputs)))
        check("tool_output contains accumulated text",
              bool(outputs and "test_one" in str(outputs[0].render()) and "test_two" in str(outputs[0].render())),
              str(outputs[0].render() if outputs else None))

        # 4. Test Text Response block with Markdown
        table_md = "| Metric | Value |\n|---|---|\n| Tests | 2 passed |"
        for line in table_md.splitlines():
            run.emit("text", line)
        pane.refresh_all()
        await pilot.pause(0.3)

        responses = pane.query(".response-block")
        check("response block mounted", len(responses) == 1, str(len(responses)))
        check("response block has border_title 'response'",
              bool(responses and responses[0].border_title == "response"),
              str(responses[0].border_title if responses else None))
        check("response renders markdown table", len(pane.query(".response-block MarkdownTable")) == 1)

        # 5. Check CSS borders and distinct styling on all blocks
        t_style = thoughts[0].styles
        r_style = responses[0].styles
        cmd_style = tools[0].styles
        out_style = outputs[0].styles

        check("thought has distinct border", bool(t_style.border.top[0] == "round"))
        check("response has distinct border", bool(r_style.border.top[0] == "round"))
        check("tool has distinct border", bool(cmd_style.border.top[0] == "round"))
        check("tool_output has distinct border", bool(out_style.border.top[0] == "round"))

        # Colors should be distinct
        t_color = t_style.border.top[1]
        r_color = r_style.border.top[1]
        c_color = cmd_style.border.top[1]
        o_color = out_style.border.top[1]
        check("thought and response border colors differ", t_color != r_color, f"{t_color} vs {r_color}")
        check("tool and output border colors differ", c_color != o_color, f"{c_color} vs {o_color}")
        check("tool and response border colors differ", c_color != r_color, f"{c_color} vs {r_color}")

        # 6. Check that Static widgets remain selectable
        statics = pane.query(Static)
        check("static widgets allow text selection", all(s.allow_select for s in statics))

    print(f"\n{sum(1 for ok, _ in results if ok)}/{len(results)} passed")
    if not all(ok for ok, _ in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
