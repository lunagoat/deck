"""Headless test of deck's confirm/deny flow and the markdown pane. Never calls a real agent.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_permissions.py [screenshot.svg]
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402,F401  (throwaway state; must come before deck is imported)
sys.path.insert(0, str(ROOT))

from textual.widgets import Markdown  # noqa: E402
from textual.widgets._markdown import MarkdownTable  # noqa: E402

from deck.app import DeckApp, PermissionScreen  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


TABLE = """Here is the summary:

| Command | What it does |
|---|---|
| `/new` | Blank view |
| `/plan <task>` | Plan only |

Done."""


async def main():
    work = _env.BASE / "permwork"
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)
    app = DeckApp()
    async with app.run_test(size=(150, 45)) as pilot:
        run = app.hub.attach("claude", "sess-p", str(work), "perm test", [("text", "── you: hi")])
        app.show_task(run.task)
        await pilot.pause(0.3)

        # ---- markdown rendering and selectability ------------------------------------------------
        for line in TABLE.splitlines():
            run.emit("text", line)
        run.emit("tool", "Bash: ls")
        run.emit("text", "after the tool")
        pane = app.panes[run.task.id]["claude"]
        pane.refresh_all()
        await pilot.pause(0.6)
        check("agent text becomes one Markdown block", len(pane.query(Markdown)) == 2, str(len(pane.query(Markdown))))
        check("markdown table is a real table", len(pane.query(MarkdownTable)) == 1)
        check("pane text can be selected", all(w.allow_select for w in pane.query("Static")))
        if len(sys.argv) > 1:
            app.save_screenshot(sys.argv[1])

        # ---- the permission flow, through the real MCP script --------------------------------------
        env = {**os.environ, "DECK_SOCK": app.hub.sock_path, "DECK_RUN": str(run.rid)}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "bin" / "deck-permission-mcp"), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        mid = 0

        async def rpc(method, params=None):
            nonlocal mid
            mid += 1
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                          "params": params or {}}) + "\n").encode())
            await proc.stdin.drain()
            return json.loads(await asyncio.wait_for(proc.stdout.readline(), 15))

        async def approve(tool, inp):
            return json.loads((await rpc("tools/call", {"name": "approve", "arguments": {
                "tool_name": tool, "input": inp}}))["result"]["content"][0]["text"])

        async def screen_up() -> PermissionScreen:
            for _ in range(60):
                if isinstance(app.screen, PermissionScreen):
                    return app.screen
                await pilot.pause(0.1)
            raise AssertionError("no permission dialog appeared")

        init = await rpc("initialize", {"protocolVersion": "2024-11-05"})
        check("mcp initialize", "serverInfo" in init["result"])
        tools = await rpc("tools/list")
        check("mcp lists approve", tools["result"]["tools"][0]["name"] == "approve")

        t = asyncio.create_task(approve("Bash", {"command": "git status"}))
        scr = await screen_up()
        check("dialog shows the command", "git status" in str(scr.inp) and scr.key == "Bash: git status")
        check("run is marked as waiting", run.waiting.startswith("Bash"), run.waiting)
        await pilot.press("y")
        res = await t
        check("y allows", res["behavior"] == "allow" and res["updatedInput"]["command"] == "git status", str(res))
        await pilot.pause(0.2)
        check("waiting cleared", run.waiting == "")

        t = asyncio.create_task(approve("Bash", {"command": "rm -rf build"}))
        await screen_up()
        await pilot.press("n")
        res = await t
        check("n denies", res["behavior"] == "deny" and "denied" in res["message"], str(res))

        t = asyncio.create_task(approve("Bash", {"command": "ls -la src"}))
        await screen_up()
        await pilot.press("escape")
        res = await t
        check("esc denies", res["behavior"] == "deny")

        t = asyncio.create_task(approve("Bash", {"command": "git log -3"}))
        await screen_up()
        await pilot.press("a")
        res = await t
        check("a allows", res["behavior"] == "allow")
        res = await asyncio.wait_for(approve("Bash", {"command": "git log --oneline"}), 10)
        check("same command kind is then auto-allowed", res["behavior"] == "allow" and not isinstance(app.screen, PermissionScreen))
        t = asyncio.create_task(approve("Bash", {"command": "git push origin main"}))
        scr = await screen_up()
        check("a dangerous command always asks, never 'always'", scr.key is None)
        await pilot.press("n")
        await t

        t = asyncio.create_task(approve("Bash", {"command": "ls && rm -rf /tmp/x"}))
        scr = await screen_up()
        check("chained commands never get 'always'", scr.key is None)
        await pilot.press("a")  # must be ignored
        await pilot.pause(0.2)
        check("pressing a does nothing there", isinstance(app.screen, PermissionScreen))
        await pilot.press("n")
        res = await t
        check("chained command denied", res["behavior"] == "deny")

        # two questions at once queue instead of overwriting each other
        async def raw(tool, inp):  # what two different runs' MCP servers would each do
            rd, wr = await asyncio.open_unix_connection(app.hub.sock_path)
            wr.write((json.dumps({"op": "permission", "from": run.rid, "tool": tool, "input": inp}) + "\n").encode())
            await wr.drain()
            return json.loads(await rd.readline())

        t1 = asyncio.create_task(raw("WebFetch", {"url": "https://example.com"}))
        t2 = asyncio.create_task(raw("Bash", {"command": "date"}))
        await screen_up()
        await pilot.press("y")
        await pilot.pause(0.5)
        await screen_up()
        await pilot.press("n")
        r1, r2 = await t1, await t2
        check("two simultaneous requests are both answered", {r1["behavior"], r2["behavior"]} == {"allow", "deny"}, f"{r1} {r2}")

        # a yes to an exact command is remembered for a couple of minutes, so two layers asking about the
        # same rm (an agent's own rule and deck's shell guard) don't double-prompt
        t = asyncio.create_task(raw("Bash", {"command": "rm -rf cache"}))
        await screen_up()
        await pilot.press("y")
        check("the first ask for a dangerous command shows a dialog", (await t)["behavior"] == "allow")
        again = await asyncio.wait_for(raw("Bash", {"command": "rm -rf cache"}), 10)
        check("asking again for exactly that command doesn't prompt twice",
              again["behavior"] == "allow" and not isinstance(app.screen, PermissionScreen), str(again))
        t = asyncio.create_task(raw("Bash", {"command": "rm -rf cache2"}))
        await screen_up()
        await pilot.press("n")
        check("a different command still asks", (await t)["behavior"] == "deny")

        # no dialog possible -> fail closed
        app.hub.asker = None
        res = await approve("Bash", {"command": "date"})
        check("no UI means deny", res["behavior"] == "deny")

        proc.stdin.close()
        await proc.wait()

    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


asyncio.run(main())
