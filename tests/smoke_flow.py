"""Headless test of replying, queueing, delegate check-ins, polling and the usage bars. No real agent is started.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_flow.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402,F401  (throwaway state; must come before deck is imported)
sys.path.insert(0, str(ROOT))

from deck import core, usage  # noqa: E402
from deck.app import DeckApp, DiffScreen  # noqa: E402

core.CHECKIN_DELAY = 0.05
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


CLAUDE_EVENT = {"status": "allowed", "resetsAt": 1792737600, "rateLimitType": "five_hour",
                "unifiedWindows": {"five_hour": {"utilization": 0.63, "resetsAt": 1792737600},
                                   "seven_day": {"utilization": 0.75, "resetsAt": 1792792800}}}
CODEX_LIMITS = {"limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1792736197},
                "secondary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1793322997},
                "credits": {"hasCredits": True, "unlimited": False, "balance": "2500.00"}, "planType": "plus"}
AGY_EVENT = {"event": "command_result", "command": {"name": "usage", "data": {"groups": [
    {"name": "Gemini Models", "buckets": [
        {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.18, "reset_time": "2026-10-26T19:00:34Z"},
        {"id": "gemini-5h", "window": "5h", "remaining_fraction": 1, "reset_time": "2026-10-23T21:57:35Z"}]},
    {"name": "Claude and GPT models", "buckets": [
        {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.5, "reset_time": "2026-10-30T05:30:38Z"}]}]}}}

COPILOT_USER = {"copilot_plan": "individual", "quota_reset_date_utc": "2026-11-01T00:00:00.000Z", "quota_snapshots": {
    "chat": {"percent_remaining": 98.8, "has_quota": True, "unlimited": False},
    "completions": {"percent_remaining": 100.0, "has_quota": True, "unlimited": False},
    "premium_interactions": {"percent_remaining": 0.0, "has_quota": False, "unlimited": False}}}


async def main():
    os.chdir(_env.BASE)
    app = DeckApp()
    sent: list[tuple[str, str]] = []   # (run label, prompt) for everything deck would have launched

    async def fake_run(run, prompt, use_wt, session):
        sent.append((run.label, prompt))
        app.hub.notify(run)  # the real _run announces itself like this, which is what adds a pane

    app.hub._run = fake_run
    msgs: list[str] = []
    async with app.run_test(size=(160, 45)) as pilot:
        flash = app.flash
        app.flash = lambda m: (msgs.append(m), flash(m))[1]

        async def type_(text):
            msgs.clear()
            app.query_one("#prompt").value = text
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.4)

        hub = app.hub
        primary = hub.attach("claude", "sess-claude", str(ROOT), "my task", [("text", "hello")])
        task = primary.task
        codex = hub._add_run(task, "codex", parent=primary)
        codex.session, codex.status, codex.cwd = "sess-codex", "done", primary.cwd
        app.show_task(task)
        await pilot.pause(0.5)

        # ---- 4: typing replies to the task on screen -------------------------------------------
        check("placeholder names the reply target", "reply to claude" in app.query_one("#prompt").placeholder,
              app.query_one("#prompt").placeholder)
        await type_("please also add tests")
        check("plain text replies to the primary run, not a new chat",
              sent == [("claude", "please also add tests")] and len(hub.tasks) == 1, f"{sent} {len(hub.tasks)}")
        sent.clear()

        await type_("@codex check the edge cases")
        check("@codex replies to codex's run", sent == [("codex", "check the edge cases")], str(sent))
        sent.clear()

        await pilot.press("alt+h")  # review a finished conversation before targeting it
        app.panes[task.id]["codex"].focus()
        await pilot.pause(0.3)
        check("focusing a pane retargets replies", "reply to codex" in app.query_one("#prompt").placeholder,
              app.query_one("#prompt").placeholder)
        sent.clear()
        await app.reply("and the docs")  # focus is on codex's pane
        await pilot.pause(0.3)
        check("a focused pane receives the reply", sent == [("codex", "and the docs")], str(sent))
        sent.clear()
        app.query_one("#prompt").focus()
        await pilot.pause(0.2)
        await pilot.press("alt+h")

        # busy -> queued -> delivered when it stops
        primary.status = "running"
        await type_("one more thing")
        check("a busy run queues the reply", primary.queued == ["one more thing"] and sent == [], f"{primary.queued} {sent}")
        check("and tells you", any("queued" in m for m in msgs), str(msgs))
        primary.status = "done"
        hub._after_turn(primary)
        await pilot.pause(0.3)
        check("queued replies go out when the run finishes", sent and sent[0][0] == "claude" and "one more thing" in sent[0][1], str(sent))
        sent.clear()

        # an agent that isn't in the task yet can be pulled in
        agy_ok = "agy" in app.avail
        if agy_ok:
            await type_("@agy review the UI")
            check("@agent not in the task joins it", sent == [("agy", "review the UI")] and "agy" in task.runs, str(sent))
            sent.clear()
        await type_("/new")
        check("/new goes back to a fresh prompt", app.query_one("#prompt").placeholder == "describe a task…")
        app.show_task(task)
        await pilot.pause(0.3)

        # ---- 3: check-ins ---------------------------------------------------------------------
        for r in task.runs.values():
            r.reported = False
        sent.clear()
        codex.status = "done"
        codex.lines.append(("text", "codex found 2 bugs in parser.py"))
        hub._after_turn(codex)
        await pilot.pause(0.4)
        check("an idle primary is woken when its delegate finishes",
              len(sent) == 1 and sent[0][0] == "claude" and "[deck] Your delegated tasks" in sent[0][1]
              and "codex found 2 bugs" in sent[0][1], str(sent))
        check("the result is marked delivered", codex.reported)
        sent.clear()
        hub._after_turn(codex)
        await pilot.pause(0.3)
        check("no duplicate check-in", sent == [], str(sent))

        # a busy primary is not interrupted; it is woken when its own turn ends
        codex.reported = False
        primary.status = "running"
        hub._after_turn(codex)
        await pilot.pause(0.3)
        check("a working primary isn't interrupted", sent == [], str(sent))
        primary.status = "done"
        hub._after_turn(primary)
        await pilot.pause(0.4)
        check("...but is checked in with when it stops", len(sent) == 1 and "[deck] Your delegated tasks" in sent[0][1], str(sent))
        sent.clear()

        # the cap stops two agents waking each other forever
        codex.reported = False
        primary.checkins = hub.cfg["max_checkins"]
        hub._after_turn(codex)
        await pilot.pause(0.3)
        check("check-ins are capped", sent == [], str(sent))
        primary.checkins = 0

        # ---- 3: polling through the real socket and the real script ------------------------------
        codex.reported = False
        codex.lines.append(("text", "second result"))

        async def socket_call(req):
            rd, wr = await asyncio.open_unix_connection(hub.sock_path)
            wr.write((json.dumps(req) + "\n").encode())
            await wr.drain()
            return json.loads(await rd.readline())

        r1 = await socket_call({"op": "poll", "from": primary.rid})
        row = r1["delegates"][0]
        check("poll reports a finished delegate once, with its result",
              row["agent"] == "codex" and row["status"] == "done" and "second result" in row.get("result", ""), str(r1))
        r2 = await socket_call({"op": "poll", "from": primary.rid})
        check("...and not again", "result" not in r2["delegates"][0], str(r2))

        env = {**os.environ, "DECK_SOCK": hub.sock_path, "DECK_RUN": str(primary.rid)}
        out = await asyncio.to_thread(lambda: subprocess.run(
            [sys.executable, str(ROOT / "bin" / "deck-delegate"), "--poll"], env=env, capture_output=True, text=True, timeout=20))
        check("deck-delegate --poll prints the table", "codex: done" in out.stdout, out.stdout + out.stderr)
        out = await asyncio.to_thread(lambda: subprocess.run(
            [sys.executable, str(ROOT / "bin" / "deck-delegate"), "--start", "codex", "do", "a", "thing"],
            env=env, capture_output=True, text=True, timeout=20))
        check("deck-delegate --start returns at once", "started codex" in out.stdout and "wake" in out.stdout, out.stdout + out.stderr)
        check("...and the delegate got its prompt", ("codex-2", "do a thing") in sent, str(sent))
        check("the new delegate shows up as a pane", "codex-2" in app.panes[task.id])
        check("the primary pane is starred", app.panes[task.id]["claude"].border_title.startswith("★"))

        # ---- 1: usage bars -----------------------------------------------------------------------
        cw = usage.claude_windows(CLAUDE_EVENT)
        check("claude windows parsed", [(w.label, round(w.used * 100)) for w in cw] == [("5h", 63), ("7d", 75)], str(cw))
        xw, note = usage.codex_windows(CODEX_LIMITS)
        check("codex windows parsed", [(w.label, round(w.used * 100)) for w in xw] == [("5h", 25), ("7d", 4)], str(xw))
        check("codex plan and credits noted", "plus" in note and "2,500" in note, note)
        hub.usage.set("claude", cw)
        hub.usage.set("codex", xw, note)
        app._render_usage()
        await pilot.pause(0.2)
        strip = str(app.query_one("#usage").render())
        check("the strip shows both agents with bars and percentages",
              "claude" in strip and "63%" in strip and "75%" in strip and "codex" in strip and "25%" in strip and "█" in strip, strip)
        check("it is visible", not app.query_one("#usage").has_class("hidden"))
        await pilot.press("alt+u")
        await pilot.pause(0.2)
        compact = str(app.query_one("#usage").render())
        check("alt+u collapses it to one line", "\n" not in compact.strip() and "63%" in compact, compact)
        await pilot.press("alt+u")
        reopened = usage.UsageBook()
        check("readings survive a restart", reopened.data["claude"].windows[0].used == 0.63)
        old = usage.lines(hub.usage, ["claude"], {}, now=time.time() + 7200)[0].plain
        check("an old reading says so", "as of 2h ago" in old, old)
        aw = usage.agy_windows(AGY_EVENT)
        check("agy's /usage maps to windows, most used first",
              [(w.label, round(w.used, 2)) for w in aw] == [("gem 7d", 0.82), ("claude/gpt 7d", 0.5), ("gem 5h", 0.0)]
              and aw[0].resets_at == 1793041234, aw)
        cw2, plan = usage.copilot_windows(COPILOT_USER)
        check("copilot quotas skip plans without them, most used first",
              [(w.label, round(w.used, 3)) for w in cw2] == [("chat", 0.012), ("compl", 0.0)] and plan == "individual"
              and cw2[0].resets_at == 1793491200, cw2)
        note = usage.opencode_note({"tokens": {"input": 0, "output": 0}}, {"tokens": {"input": 940542, "output": 123246, "reasoning": 200954,
                                                                                        "cache": {"read": 21600986}}, "cost": 0})
        check("opencode shows tokens, not cache reads", note == "0 tokens today · 1.3m in 7d", note)
        check("countdown formats", usage.countdown(time.time() + 3 * 3600 + 5 * 60 + 30) == "3h05m"
              and usage.countdown(time.time() + 2 * 86400 + 3600 + 30) == "2d1h" and usage.countdown(time.time() - 5) == "reset")
        await type_("/usage")
        check("/usage opens the bars", isinstance(app.screen, DiffScreen) and "63%" in app.screen.text.plain, "")
        check("/usage is honest about agents without limits", "needs the gh CLI" in app.screen.text.plain, app.screen.text.plain[-400:])
        await pilot.press("escape")

        # ---- 2: primary -------------------------------------------------------------------------
        await type_("/primary codex")
        check("/primary changes who starts tasks", app.cfg["default_agents"] == ["codex"], str(app.cfg["default_agents"]))
        await type_("/primary claude")
        check("...and back", app.cfg["default_agents"] == ["claude"])
        await type_("/primary nosuchagent")
        check("/primary refuses agents you don't have", app.cfg["default_agents"] == ["claude"] and any("isn't installed" in m for m in msgs), str(msgs))

        # Finished panes keep their history but leave the live layout and keyboard cycle.
        for run in task.runs.values():
            run.status = "done"
            hub.notify(run)
        await pilot.pause(0.2)
        panes = app.panes[task.id]
        check("idle task keeps its main conversation visible", panes[primary.label].display)
        check("finished delegates hide", all(not p.display for p in panes.values() if p.run is not primary))
        await pilot.press("alt+h")
        check("finished history can be revealed", all(p.display for p in panes.values()))
        pane = panes["codex"]
        pane.focus()
        await pilot.pause(0.1)
        await pilot.press("alt+h")
        check("hiding focused history returns to input", app.query_one("#prompt").has_focus)
        codex.status = "queued"
        hub.notify(codex)
        await pilot.pause(0.2)
        check("new work restores the same pane", pane.display and panes["codex"] is pane)
        check("only active panes occupy the grid", app.query_one("#grid").styles.grid_size_columns == 1
              and not panes[primary.label].display)
        await pilot.press("tab")
        check("tab skips finished panes", pane.has_focus)
        codex.waiting = "permission"
        codex.status = "done"
        hub.notify(codex)
        check("permission requests remain visible", pane.display)
        codex.waiting = ""
        hub.notify(codex)
        app.show_task(task)
        await pilot.pause(0.2)
        check("reopening a completed task shows the conversation", app.panes[task.id][primary.label].display)

        await type_("/panes")
        app.screen.query_one("#q").value = "codex"
        await pilot.pause(0.1)
        await pilot.press("enter")
        app.screen.query_one("#q").value = "claude"
        await pilot.pause(0.1)
        await pilot.press("enter", "escape")
        check("picker keeps multiple finished conversations open", codex.rid in app.pinned_panes
              and primary.rid in app.pinned_panes
              and app.panes[task.id][codex.label].display and app.panes[task.id][primary.label].display)
        app.show_task(task)
        await pilot.pause(0.2)
        check("kept-open panes survive task navigation", app.panes[task.id][codex.label].display)
        app._toggle_pane_pin(codex)
        check("unpinning restores automatic hiding", not app.panes[task.id][codex.label].display
              and app.panes[task.id][primary.label].display)

        from types import SimpleNamespace
        from unittest.mock import patch
        session = SimpleNamespace(agent="claude", id="resumed-claude", cwd=str(ROOT), title="old conversation")
        with patch("deck.app.transcript", return_value=[("text", "Previous answer")]):
            app._resume_picked(session)
        await pilot.pause(0.2)
        resumed = app.follow
        check("resume opens a finished conversation immediately", resumed.status == "done"
              and app.panes[app.current.id][resumed.label].display and app.follow is resumed)
        session2 = SimpleNamespace(agent="claude", id="another-claude", cwd=str(ROOT), title="another conversation")
        with patch("deck.app.transcript", return_value=[("text", "Separate history")]):
            app._resume_picked(session2)
        await pilot.pause(0.2)
        resumed2 = app.follow
        check("multiple resumed conversations remain visible together", resumed2 is not resumed
              and app.panes[task.id][resumed.label].display and app.panes[task.id][resumed2.label].display)
        check("resumed conversations retain independent sessions and histories",
              resumed.session == session.id and resumed2.session == session2.id
              and resumed.lines == [("text", "Previous answer")]
              and resumed2.lines == [("text", "Separate history")]
              and resumed.parent is None and resumed2.parent is None)
        sent.clear()
        await type_(f"@{resumed.label} continue the first conversation")
        check("explicit reply targets the requested resumed conversation",
              sent == [(resumed.label, "continue the first conversation")], str(sent))
        count = len(task.runs)
        with patch("deck.app.transcript", return_value=[]):
            app._resume_picked(session)
        await pilot.pause(0.2)
        check("opening the same session again reuses its pane", len(task.runs) == count and app.follow is resumed)

    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


asyncio.run(main())
