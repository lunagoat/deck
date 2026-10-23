"""Headless test of agent detection, /rescan and custom agents, on a fake HOME with stub binaries.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_detect.py
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402

FAKE = _env.BASE / "detect"
shutil.rmtree(FAKE, ignore_errors=True)
BIN, HOME = FAKE / "bin", FAKE / "home"
for d in (BIN, HOME / ".local" / "bin", HOME / ".config" / "deck"):
    d.mkdir(parents=True)
os.environ.update(HOME=str(HOME), PATH=str(BIN), XDG_STATE_HOME=str(FAKE / "state"),
                  XDG_CONFIG_HOME=str(HOME / ".config"))
sys.path.insert(0, str(ROOT))

from deck.app import DeckApp  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


def stub(where: Path, name: str):
    p = where / name
    p.write_text("#!/bin/sh\necho stub 1.0\n")
    p.chmod(0o755)
    return p


async def main():
    os.chdir(FAKE)
    stub(BIN, "claude")
    stub(BIN, "codex")
    app = DeckApp()
    msgs: list[str] = []
    async with app.run_test(size=(150, 40)) as pilot:
        flash = app.flash
        app.flash = lambda m: (msgs.append(m), flash(m))[1]

        async def cmd(text):
            msgs.clear()
            app.query_one("#prompt").value = text
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.4)

        check("only installed agents are offered", app.agents() == ["claude", "codex"], str(app.agents()))
        chips = str(app.query_one("#chips").render())
        check("chips list only those", "1:claude" in chips and "2:codex" in chips and "opencode" not in chips, chips)

        stub(BIN, "opencode")
        await cmd("/rescan")
        check("/rescan finds a new agent", "opencode" in app.agents() and any("new: opencode" in m for m in msgs), str(msgs))

        (BIN / "claude").unlink()
        await cmd("/detect")
        check("/rescan notices a removed agent", "claude" not in app.agents() and any("gone: claude" in m for m in msgs), str(msgs))

        stub(HOME / ".local" / "bin", "agy")  # not on PATH, but a usual install dir
        app.action_rescan()
        check("finds agents in the usual install dirs", "agy" in app.agents(), str(app.agents()))
        check("launches it by its full path", app.hub.exe("agy") == str(HOME / ".local" / "bin" / "agy"), app.hub.exe("agy"))

        stub(BIN, "aider")
        app.action_rescan()
        check("mentions popular agents it can't drive yet", any("Aider" in m for m in msgs), str(msgs))

        await pilot.press("alt+1")
        first = app.agents()[0]
        check("alt+1 toggles the first detected agent", app.chosen == {first}, f"{first} {app.chosen}")
        app.chosen.clear()

        # /status runs each agent's --version
        await cmd("/status")
        from deck.app import DiffScreen
        check("/status shows detected agents and their paths", isinstance(app.screen, DiffScreen)
              and "stub 1.0" in app.screen.text and str(BIN) in app.screen.text, getattr(app.screen, "text", "")[:300])
        await pilot.press("escape")
        await pilot.pause(0.2)

        # /new several times in a row used to crash (duplicate widget id)
        r = app.hub.attach("codex", "s1", str(FAKE), "t", [("text", "hi")])
        app.show_task(r.task)
        await pilot.pause(0.3)
        for _ in range(3):
            app.query_one("#prompt").value = "/new"
            await pilot.pause(0.05)
            await pilot.press("enter")
        await pilot.pause(1)
        check("/new three times in a row survives", app.current is None and len(app.query(".empty")) == 1)

    # ---- custom agents from config.toml, and "nothing installed" ---------------------------------
    (HOME / ".config" / "deck" / "config.toml").write_text(
        '[agents.gemini]\ncommand = ["gemini", "-p", "{prompt}"]\nmodel_flag = ["--model", "{model}"]\n'
        'color = "#4285f4"\n[agents.broken]\nfoo = 1\n[agents.claude]\ncommand = ["x"]\n')
    for n in ("codex", "opencode", "aider"):
        (BIN / n).unlink(missing_ok=True)
    (HOME / ".local" / "bin" / "agy").unlink()
    app = DeckApp()
    async with app.run_test(size=(150, 40)) as pilot:
        from deck.adapters import ORDER
        check("a config [agents.*] entry is registered", "gemini" in ORDER, str(ORDER))
        check("bad entries are reported, not fatal", len(app.cfg["problems"]) == 2, str(app.cfg["problems"]))
        check("custom agent is absent until its binary exists", app.agents() == [], str(app.agents()))
        check("zero agents explains itself", "no coding agents found" in str(app.query_one("#chips").render()))
        app.query_one("#prompt").value = "do something"
        await pilot.press("enter")
        await pilot.pause(0.3)
        stub(BIN, "gemini")
        app.action_rescan()
        check("rescan picks up the custom agent", app.agents() == ["gemini"], str(app.agents()))
        from deck.adapters import make_adapters
        g = make_adapters()["gemini"]
        check("custom command is filled in", g.cmd("hi there", "/", None, model="m1") == ["gemini", "-p", "hi there", "--model", "m1"], str(g.cmd("hi there", "/", None, model="m1")))
        check("custom agents are fenced", g.sandboxed)

    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


asyncio.run(main())
