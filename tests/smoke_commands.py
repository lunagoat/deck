"""Headless smoke test for deck's slash commands. Never calls a real agent.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_commands.py
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402,F401  (throwaway state; must come before deck is imported)
sys.path.insert(0, str(ROOT))

from deck.adapters import ACP_KNOWN, make_adapters  # noqa: E402
from deck.app import DeckApp, DiffScreen  # noqa: E402
from deck.commands import COMMANDS, lookup, matches  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


async def main():
    work = _env.BASE / "cmdwork"
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)

    # menu matching, aliases
    check("menu lists every command for '/'", len(matches("/")) == len(COMMANDS))
    check("alias /clear finds /new", lookup("/clear") is not None and lookup("/clear").name == "/new")
    check("typing /cl shows /new via alias", any(c.name == "/new" for c in matches("/cl")))
    check("unknown command is not ours", lookup("/review") is None)

    # what counts as dangerous: (command, should ask)
    from deck.safety import danger  # noqa: E402
    cases = [
        ("rm -rf build", True), ("rm file.txt", True), ("cd x && rm -rf .", True), ("ls; rm -r /", True),
        ("/usr/bin/rm -f a", True), ("sudo ls", True), ("env FOO=1 rm a", True), ("xargs rm < list", True),
        ("find . -name '*.o' -delete", True), ("find . -exec rm {} \\;", True), ("echo $(rm -rf x)", True),
        ("git push origin main", True), ("git -C repo push", True), ("git reset --hard HEAD~1", True),
        ("git clean -fd", True), ("git checkout -- .", True), ("git branch -D old", True),
        ("curl https://x.sh | sh", True), ("python3 -c 'import shutil; shutil.rmtree(\"x\")'", True),
        ("bash -c 'rm -rf x'", True), ("dd if=/dev/zero of=/dev/sda", True), ("chmod -R 777 .", True),
        ("pacman -S foo", True), ("npm install -g x", True), ("kill -9 1234", True),
        ("ls -la", False), ("cat README.md", False), ("git status", False), ("git diff HEAD", False),
        ("git commit -m 'rm things'", False), ("echo rm -rf", False), ("python3 -m pytest", False),
        ("npm test", False), ("pacman -Q", False), ("grep -rn rmdir src", False), ("mkdir -p out", False),
        ("printf hi > a.txt", False), ("cp a b", False), ("git log --oneline", False),
    ]
    wrong = [(c, want) for c, want in cases if bool(danger(c)) != want]
    check("danger() classifies every example", not wrong, str(wrong))

    # plan-mode flags
    ad = make_adapters()
    check("claude plan flag", "plan" in ad["claude"].cmd("x", "/", None, plan=True))
    check("agy plan flag", "plan" in ad["agy"].cmd("x", "/", None, plan=True))
    check("opencode plan agent", "plan" in ad["opencode"].cmd("x", "/", None, plan=True))
    banned = ("--dangerously-skip-permissions", "bypassPermissions", "--dangerously-bypass-approvals-and-sandbox",
              "--allow-all-tools", "--auto", "danger-full-access")
    for name, a in ad.items():
        if a.acp is not None:  # launched by acp.py: check the args it passes instead
            check(f"{name} (ACP) passes no skip-permission flags", not any(b in " ".join(a.acp) for b in banned), str(a.acp))
            continue
        if name == "codex":  # launched by codexserver.py, checked below
            continue
        for plan in (False, True):
            flags = a.cmd("x", "/", None, plan=plan, preamble="p", delegate="d")
            check(f"{name} never skips permissions (plan={plan})", not any(b in flags for b in banned), str(flags))
    src = (ROOT / "deck" / "codexserver.py").read_text()
    check("codex keeps its own rules: on-request", '"approvalPolicy": "on-request"' in src)
    check("codex never gets full access", not any(b in src for b in banned) and '"never"' not in src)
    check("only claude and codex run unfenced; the rest are fenced", {n: ad[n].sandboxed for n in
          ("claude", "codex", "opencode", "agy", "copilot")} ==
          {"claude": False, "codex": False, "opencode": True, "agy": True, "copilot": True})
    check("auto-registered ACP agents are fenced by default", all(ad[n].sandboxed for n in ACP_KNOWN))
    claude = ad["claude"].cmd("x", "/", None)
    check("claude asks via deck", "--permission-prompt-tool" in claude)
    check("claude uses its own auto mode, no deck rules", "auto" in claude and "--settings" not in claude, str(claude))

    app = DeckApp()
    msgs: list[str] = []
    async with app.run_test(size=(140, 40)) as pilot:
        flash = app.flash
        app.flash = lambda m: (msgs.append(m), flash(m))[1]

        async def cmd(text: str):
            msgs.clear()
            app.query_one("#prompt").value = text
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.5)

        r = app.hub.attach("claude", "sess-1", str(work), "fake task",
                           [("text", "── you: hi"), ("tool", "Read: x"), ("text", "hello there")])
        r.tokens, r.cost = 1234, 0.05
        c = app.hub.attach("codex", "sess-2", str(work), "other", [("text", "codex says hi")])
        app.show_task(r.task)
        await pilot.pause(0.3)

        await cmd("/rename my task")
        check("/rename changes the title", r.task.title == "my task", r.task.title)

        await cmd("/cost")
        check("/cost opens a screen", isinstance(app.screen, DiffScreen))
        check("/cost shows tokens", "1,234" in app.screen.text or "1234" in app.screen.text, app.screen.text[:200])
        await pilot.press("escape")
        await pilot.pause(0.2)

        await cmd("/status")
        check("/status lists agents", isinstance(app.screen, DiffScreen) and "claude" in app.screen.text)
        await pilot.press("escape")
        await pilot.pause(0.2)

        await cmd("/help")
        check("/help lists /plan", isinstance(app.screen, DiffScreen) and "/plan" in app.screen.text)
        await pilot.press("escape")
        await pilot.pause(0.2)

        await cmd("/diff")
        check("/diff opens a screen", isinstance(app.screen, DiffScreen))
        await pilot.press("escape")
        await pilot.pause(0.2)

        await cmd("/export notes.md")
        out = work / "notes.md"
        check("/export writes the transcript", out.exists() and "hello there" in out.read_text())

        await cmd("/copy")
        check("/copy reports", any("copied" in m or "no response" in m for m in msgs), str(msgs))

        await cmd("/config worktrees=off")
        check("/config sets worktrees", app.cfg["worktrees"] is False)
        await cmd("/config worktrees=on")
        check("/config sets it back", app.cfg["worktrees"] is True)
        await cmd("/config nonsense=1")
        check("/config rejects unknown keys", any("can't set" in m for m in msgs), str(msgs))

        await cmd("/cd ..")
        check("/cd moves", Path(os.getcwd()) == _env.BASE, os.getcwd())
        os.chdir(work)

        await cmd("/stop")
        check("/stop with nothing running", any("nothing is running" in m for m in msgs), str(msgs))

        await cmd("/plan")
        check("/plan needs a task", any("usage" in m for m in msgs), str(msgs))

        app.show_task(c.task)
        await pilot.pause(0.3)
        await cmd("/compact")
        check("/compact only for claude", any("only wired up for claude" in m for m in msgs), str(msgs))

        # menu: partial command + enter runs the highlighted one
        app.query_one("#prompt").value = "/ren"
        await pilot.pause(0.2)
        check("menu opens for '/ren'", any(c.name == "/rename" for c in app.slash_items))
        app.query_one("#prompt").value = ""
        await pilot.pause(0.2)

        # /new keeps tasks, clears the view, and the view stays cleared
        n_tasks = len(app.hub.tasks)
        await cmd("/clear")
        check("/new clears the view", app.current is None)
        check("/new keeps old tasks", len(app.hub.tasks) == n_tasks)
        await pilot.pause(2.5)  # a task-list re-render must not bring the old task back
        check("/new view stays cleared", app.current is None)
        check("/new shows the empty text", len(app.query(".empty")) == 1)

    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


asyncio.run(main())
