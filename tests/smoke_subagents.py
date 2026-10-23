"""Test subagent (delegation) lifecycle: cascading kill, pane creation, delegation guards.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_subagents.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402,F401  (throwaway state; must come before deck is imported)
sys.path.insert(0, str(ROOT))

from deck import core  # noqa: E402
from deck.app import DeckApp  # noqa: E402

core.CHECKIN_DELAY = 0.05
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


def fake_proc():
    """A fake process object with a terminate() that doesn't crash."""
    p = MagicMock()
    p.terminate = MagicMock()
    return p


async def main():
    os.chdir(_env.BASE)
    app = DeckApp()
    sent: list[tuple[str, str]] = []

    async def fake_run(run, prompt, use_wt, session):
        sent.append((run.label, prompt))
        app.hub.notify(run)

    app.hub._run = fake_run
    async with app.run_test(size=(160, 45)) as pilot:
        hub = app.hub
        await pilot.pause(0.3)

        # ---- 1: cascading kill --------------------------------------------------------
        primary = hub.attach("claude", "sess-p", str(ROOT), "subagent test", [("text", "hello")])
        task = primary.task
        primary.status = "running"
        primary.proc = True  # fake, but truthy enough for kill checks
        child1 = hub._add_run(task, "codex", parent=primary)
        child1.status, child1.proc = "running", True
        child2 = hub._add_run(task, "opencode", parent=primary)
        child2.status, child2.proc = "running", True
        child3 = hub._add_run(task, "copilot", parent=primary)
        child3.status = "queued"  # no proc yet

        hub.kill(primary)
        check("killing parent cascades to all children",
              primary.status == "killed" and child1.status == "killed"
              and child2.status == "killed" and child3.status == "killed",
              f"p={primary.status} c1={child1.status} c2={child2.status} c3={child3.status}")

        # Already-killed runs are no-ops
        hub.kill(child1)
        check("re-killing a killed run is a no-op", child1.status == "killed")

        # ---- 2: _run bails out for pre-killed runs ------------------------------------
        killed_run = hub._add_run(task, "codex", parent=primary)
        killed_run.status = "killed"
        await hub._run(killed_run, "this should not run", False, None)
        check("_run() exits early for pre-killed run",
              killed_run.status == "killed" and ("codex-2", "this should not run") not in sent,
              f"status={killed_run.status} sent={sent}")

        # ---- 3: delegation from dead parent is rejected --------------------------------
        primary.status = "killed"
        result = await hub._handle({"op": "delegate", "from": primary.rid, "agent": "codex", "prompt": "test"})
        check("delegation from killed parent is rejected",
              "error" in result and "no longer active" in result["error"], str(result))

        primary.status = "done"
        result = await hub._handle({"op": "delegate", "from": primary.rid, "agent": "codex", "prompt": "test"})
        check("delegation from finished parent is rejected",
              "error" in result and "no longer active" in result["error"], str(result))

        # ---- 4: subagent panes are created dynamically ---------------------------------
        task2_primary = hub.attach("claude", "sess-t2", str(ROOT), "pane test", [("text", "hi")])
        task2 = task2_primary.task
        task2_primary.status = "running"
        app.show_task(task2)
        await pilot.pause(0.3)
        check("primary pane exists", "claude" in app.panes[task2.id])

        # Simulate a delegation: _on_run_change should create a pane for the new child
        delegate = hub._add_run(task2, "codex", parent=task2_primary)
        delegate.status = "running"
        app._on_run_change(delegate)
        await pilot.pause(0.3)
        check("subagent pane is created on delegation",
              "codex" in app.panes[task2.id] and app.panes[task2.id]["codex"].run is delegate)

        # Add two more subagents
        d2 = hub._add_run(task2, "opencode", parent=task2_primary)
        d2.status = "running"
        app._on_run_change(d2)
        d3 = hub._add_run(task2, "copilot", parent=task2_primary)
        d3.status = "running"
        app._on_run_change(d3)
        await pilot.pause(0.3)
        check("three subagent panes created",
              "opencode" in app.panes[task2.id] and "copilot" in app.panes[task2.id])

        # Parent pane should be starred (has children)
        parent_pane = app.panes[task2.id]["claude"]
        check("parent pane gets ★ marker", parent_pane.border_title.startswith("★"),
              parent_pane.border_title)

        # ---- 5: _set_columns returns sensible values -----------------------------------
        expected = {0: 1, 1: 1, 2: 2, 3: 3, 4: 2, 5: 3, 6: 3, 7: 3, 8: 3}
        for n, want in expected.items():
            app._set_columns(n)
            got = app.query_one("#grid").styles.grid_size_columns
            check(f"_set_columns({n})={want}", got == want, f"got {got}")

        # ---- 6: pane visibility for subagents ------------------------------------------
        # All running: all visible
        visible = [p for p in app.panes[task2.id].values() if p.display]
        check("all running subagents are visible", len(visible) == 4, f"{len(visible)} visible")

        # Finish subagents, keep parent running
        for r in (delegate, d2, d3):
            r.status = "done"
            r.proc = None
            hub.notify(r)
        app._sync_pane_visibility()
        await pilot.pause(0.2)
        visible = [p for p in app.panes[task2.id].values() if p.display]
        check("finished subagents hide when parent still runs",
              len(visible) == 1 and visible[0].run is task2_primary,
              f"{len(visible)} visible: {[p.run.label for p in visible]}")

        # Finish parent too: it becomes the fallback
        task2_primary.status = "done"
        task2_primary.proc = None
        hub.notify(task2_primary)
        app._sync_pane_visibility()
        await pilot.pause(0.2)
        visible = [p for p in app.panes[task2.id].values() if p.display]
        check("when all done, primary stays visible as fallback",
              len(visible) == 1 and visible[0].run is task2_primary,
              f"{len(visible)} visible: {[p.run.label for p in visible]}")

        # alt+h shows everything
        app.show_finished = True
        app._sync_pane_visibility()
        visible = [p for p in app.panes[task2.id].values() if p.display]
        check("alt+h shows all panes including finished subagents", len(visible) == 4,
              f"{len(visible)} visible")
        app.show_finished = False

        # ---- 7: multi-level delegation (depth > 1) ------------------------------------
        parent2 = hub.attach("claude", "sess-ml", str(ROOT), "depth test", [("text", "go")])
        task3 = parent2.task
        parent2.status = "running"
        mid = hub._add_run(task3, "codex", parent=parent2)
        mid.status = "running"
        leaf = hub._add_run(task3, "opencode", parent=mid)
        leaf.status = "running"
        check("multi-level delegation creates correct depth",
              parent2.depth == 0 and mid.depth == 1 and leaf.depth == 2,
              f"depths: {parent2.depth}, {mid.depth}, {leaf.depth}")

        # Killing middle kills leaf but not parent
        hub.kill(mid)
        check("killing mid-level cascades to leaf",
              mid.status == "killed" and leaf.status == "killed" and parent2.status == "running",
              f"parent={parent2.status} mid={mid.status} leaf={leaf.status}")

    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


asyncio.run(main())
