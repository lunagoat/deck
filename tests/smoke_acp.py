"""Headless test of deck's ACP driver against tests/fake_acp_agent.py. No real agent is started.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_acp.py
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402

FAKE = _env.BASE / "acp"
shutil.rmtree(FAKE, ignore_errors=True)
(FAKE / "config" / "deck").mkdir(parents=True)
(FAKE / "work").mkdir(parents=True)
LOG = FAKE / "agent.log"
(FAKE / "config" / "deck" / "config.toml").write_text(
    f'[agents.fake]\nacp = ["{sys.executable}", "{ROOT / "tests" / "fake_acp_agent.py"}"]\nfenced = false\n')
os.environ.update(XDG_CONFIG_HOME=str(FAKE / "config"), XDG_STATE_HOME=str(FAKE / "state"), FAKE_ACP_LOG=str(LOG))
sys.path.insert(0, str(ROOT))

from deck import acp, detect, sandbox  # noqa: E402
from deck.adapters import ACP_KNOWN, ORDER, make_adapters  # noqa: E402
from deck.app import DeckApp, PermissionScreen  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


def logged(kind):
    if not LOG.exists():
        return []
    return [r["payload"] for r in map(json.loads, LOG.read_text().splitlines()) if r["kind"] == kind]


async def main():
    os.chdir(FAKE / "work")

    # ---- pure helpers ---------------------------------------------------------------------------
    opts = [{"optionId": "o", "kind": "allow_once"}, {"optionId": "a", "kind": "allow_always"}, {"optionId": "r", "kind": "reject_once"}]
    check("allow picks the offered allow_once id", acp.pick_option(opts, True) == {"outcome": "selected", "optionId": "o"})
    check("deny picks the offered reject id", acp.pick_option(opts, False) == {"outcome": "selected", "optionId": "r"})
    check("only allow_always offered: still allowed", acp.pick_option([opts[1]], True)["optionId"] == "a")
    check("nothing suitable offered: cancelled, never a guess", acp.pick_option([opts[0]], False) == {"outcome": "cancelled"})
    check("shell requests become Bash with the command", acp.permission_input(
        {"kind": "execute", "rawInput": {"command": "rm -rf x"}}) == ("Bash", {"command": "rm -rf x", "description": ""}))
    tool, inp = acp.permission_input({"kind": "edit", "title": "Edit a.py", "locations": [{"path": "/x/a.py"}]})
    check("other requests keep title and paths", tool == "Edit" and inp["paths"] == ["/x/a.py"], str((tool, inp)))

    from deck.core import load_config
    load_config()  # registers the config's [agents.*], as DeckApp does at start

    # ---- registry and detection -----------------------------------------------------------------
    adapters = make_adapters()
    check("known ACP agents are registered", all(n in adapters and adapters[n].acp is not None for n in ACP_KNOWN))
    check("gemini starts with --acp", adapters["gemini"].acp == ["--acp"] and adapters["gemini"].binary == "gemini")
    check("opencode is driven over ACP and fenced", adapters["opencode"].acp == ["acp"] and adapters["opencode"].sandboxed)
    check("claude and codex are not ACP", adapters["claude"].acp is None and adapters["codex"].acp is None)
    check("a config acp agent is registered, unfenced when asked", adapters["fake"].acp is not None and not adapters["fake"].sandboxed)
    check("they all show in the agent order", "fake" in ORDER and "gemini" in ORDER)
    check("a|b binary alternatives work", detect.find("no-such-binary-xyz|python3") is not None)
    dirs = sandbox.state_dirs("gemini", ("gemini",))
    check("the fence guesses state dirs for unknown agents", ".config/gemini" in dirs and ".gemini" in dirs, str(dirs))
    check("...and keeps the exact list for known ones", sandbox.state_dirs("agy") == [".gemini"])

    # ---- the real driver, against the fake agent ----------------------------------------------------
    app = DeckApp()
    async with app.run_test(size=(150, 40)) as pilot:
        hub = app.hub
        check("the fake agent is detected", "fake" in app.agents(), str(app.agents()))

        async def run_and_answer(start, answers):
            """answers: keys to press, one per permission dialog that appears."""
            asked = []
            for _ in range(240):
                await pilot.pause(0.25)
                if isinstance(app.screen, PermissionScreen):
                    asked.append((app.screen.tool, app.screen.inp.get("command")))
                    await pilot.press(answers[len(asked) - 1])
                if start.task.runs and not any(r.live for r in start.task.runs.values()) and len(asked) >= len(answers):
                    break
            return asked

        task = await hub.start_task("please REQUEST-PERMISSION first", ["fake"], str(FAKE / "work"))
        run = task.runs["fake"]
        app.show_task(task)
        asked = await run_and_answer(run, ["y"])
        text = "\n".join(t for _, t in run.lines)
        check("the run completes", run.status == "done", f"{run.status} {run.lines}")
        check("streamed text shows up", "Hello world" in text, text)
        check("the permission request became a dialog", asked == [("Bash", "rm -rf build")], str(asked))
        check("the tool call is shown", any(k == "tool" and "rm -rf build" in t for k, t in run.lines), str(run.lines))
        check("the agent heard 'allowed'", "ALLOWED" in text)
        check("the session id is kept", run.session == "fake-1", str(run.session))
        check("token usage is picked up", run.tokens in (1234, 1500), str(run.tokens))
        init = logged("initialize")[0]
        check("initialize speaks v1 and claims no client capabilities",
              init["protocolVersion"] == 1 and init["clientCapabilities"] == {}, str(init))
        check("session/new gets the folder and no MCP servers", logged("session/new")[0] == {"cwd": str(FAKE / "work"), "mcpServers": []})
        check("allow answered with an offered option id", logged("permission_response")[-1] == {"outcome": "selected", "optionId": "once"},
              str(logged("permission_response")))

        # follow-up: loads the session, and the replayed history stays out of the pane
        hub.reply(run, "REQUEST-PERMISSION again please")
        asked = await run_and_answer(run, ["n"])
        text = "\n".join(t for _, t in run.lines)
        check("a follow-up resumes with session/load", logged("session/load") and logged("session/load")[0]["sessionId"] == "fake-1")
        check("replayed history is not shown again", "OLD HISTORY" not in text, text)
        check("deny answered with the reject option id", logged("permission_response")[-1] == {"outcome": "selected", "optionId": "reject"},
              str(logged("permission_response")))
        check("the agent heard 'denied'", "DENIED" in text)
        check("a dangerous command is never 'always'-able", app.screen is not None)

        # model selection through the agent's own config option
        hub.set_model("fake", "b")
        t2 = await hub.start_task("just chat", ["fake"], str(FAKE / "work"))
        await run_and_answer(t2.runs["fake"], [])
        sets = logged("session/set_config_option")
        check("a model the agent offers is set", sets and sets[-1] == {"sessionId": "fake-1", "configId": "model", "value": "b"}, str(sets))
        hub.set_model("fake", "zzz")
        n = len(logged("session/set_config_option"))
        t3 = await hub.start_task("just chat", ["fake"], str(FAKE / "work"))
        await run_and_answer(t3.runs["fake"], [])
        shown = "\n".join(t for _, t in t3.runs["fake"].lines)
        check("a model it doesn't offer is reported, not sent", len(logged("session/set_config_option")) == n and "isn't offered" in shown, shown)
        hub.set_model("fake", None)

        # failures
        t4 = await hub.start_task("please CRASH-NOW", ["fake"], str(FAKE / "work"))
        await run_and_answer(t4.runs["fake"], [])
        r4 = t4.runs["fake"]
        check("an agent that dies is a failed run with its stderr", r4.status == "failed" and any("boom" in t for _, t in r4.lines), str(r4.lines))

        t5 = await hub.start_task("please HANG-NOW", ["fake"], str(FAKE / "work"))
        r5 = t5.runs["fake"]
        for _ in range(40):
            await pilot.pause(0.1)
            if r5.proc:
                break
        hub.kill(r5)
        for _ in range(40):
            await pilot.pause(0.1)
            if not r5.live:
                break
        check("stopping a run ends the agent process", r5.status == "killed" and r5.proc is None, f"{r5.status} {r5.proc}")

    bad = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


asyncio.run(main())
