"""Slash commands typed in deck's prompt box.

Modelled on Claude Code's list (https://code.claude.com/docs/en/commands) where the idea makes sense
for a dashboard of several agents. Commands we don't know are sent to the agent as a normal prompt.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from rich.text import Text

from . import detect, usage as usage_view
from .adapters import EFFORTS, ORDER, make_adapters
from .core import sh


@dataclass
class Cmd:
    name: str
    args: str
    desc: str
    run: Callable[..., Awaitable[None]]  # (app, rest) -> None; rest is everything after the command
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def _fmt_time(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _flag(value: str) -> bool | None:
    v = value.lower()
    return True if v in ("on", "true", "1", "yes") else False if v in ("off", "false", "0", "no") else None


# ---- session ---------------------------------------------------------------------------

async def cmd_new(app, rest: str):
    """Fresh start: leave the current task (it stays in the list) and wait for a new prompt."""
    if rest and app.current:
        app.current.name = rest
    app.show_empty()
    app.flash("new task: type a prompt below. earlier tasks stay in the list (ctrl+t)")


async def cmd_panes(app, rest: str):
    app.action_pick_panes()


async def cmd_compact(app, rest: str):
    run = app.target_run()
    if not run:
        return app.flash("nothing to compact: pick a task first")
    if run.agent != "claude":
        return app.flash(f"/compact is only wired up for claude (this pane is {run.agent})")
    if run.live or not run.session:
        return app.flash("wait for the run to finish first")
    await app.hub.follow_up(run, f"/compact {rest}".strip())


async def cmd_rename(app, rest: str):
    if not app.current:
        return app.flash("no task selected")
    if not rest:
        return app.flash("usage: /rename <name>")
    app.current.name = rest
    app.title = f"deck — #{app.current.id} {app.current.title}"
    app._dirty_tasks = True


async def cmd_stop(app, rest: str):
    run = app.target_run() if app.focused_run() else None
    if run:
        # Kill the focused run and its children (subagents)
        runs = [run]
    else:
        # Nothing focused: kill all live runs in the task
        runs = [r for r in (app.current.runs.values() if app.current else []) if r.live]
    n = 0
    for r in runs:
        if r.live:
            app.hub.kill(r)  # cascades to children via Hub.kill()
            n += 1
    app.flash(f"stopped {n} run{'s' if n != 1 else ''}" if n else "nothing is running")


async def cmd_exit(app, rest: str):
    app.exit()


async def cmd_cd(app, rest: str):
    path = Path(rest or "~").expanduser()
    if not path.is_dir():
        return app.flash(f"not a directory: {path}")
    os.chdir(path)
    app.flash(f"new tasks and /resume now use {os.getcwd()}")


# ---- looking at work ---------------------------------------------------------------------

async def cmd_diff(app, rest: str):
    run = app.target_run()
    if not run:
        return app.flash("no run selected")
    app.show_text(f"diff: {run.label}", await app.hub.diff(run), diff=True)


async def cmd_cost(app, rest: str):
    rows = []
    for t in app.hub.tasks:
        for r in t.runs.values():
            rows.append((t, r))
    if not rows:
        return app.flash("nothing has run yet")
    lines = [f"{'task':>5}  {'run':<14}{'model':<22}{'effort':<8}{'status':<9}{'time':>6}{'tokens':>10}{'cost':>9}"]
    tok = cost = 0
    for t, r in rows:
        tok += r.tokens or 0
        cost += r.cost or 0
        lines.append(f"{'#' + str(t.id):>5}  {r.label:<14}{(r.model or '-'):<22}{(r.effort or '-'):<8}"
                     f"{r.status:<9}{_fmt_time(r.elapsed):>6}{(r.tokens or 0):>10}"
                     f"{('$%.3f' % r.cost) if r.cost else '-':>9}")
    lines += ["", f"total: {tok:,} tokens, ${cost:.3f}  (cost is only reported by some CLIs; others show -)"]
    app.show_text("cost / usage", "\n".join(lines))


async def cmd_export(app, rest: str):
    t = app.current
    if not t:
        return app.flash("no task selected")
    out = [f"# deck task #{t.id}: {t.title}", f"cwd: {t.repo_cwd}", ""]
    for r in t.runs.values():
        head = f"{r.parent.label} → {r.label}" if r.parent else r.label
        out += [f"## {head}  ({r.agent}, {r.model or 'default model'}, {r.status})", ""]
        for kind, text in r.lines:
            out.append(f"▸ {text}" if kind == "tool" else f"✗ {text}" if kind == "error" else text)
        if r.summary:
            out += ["", "changes:", r.summary]
        out.append("")
    path = Path(rest or f"deck-task-{t.id}.md").expanduser()
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    try:
        path.write_text("\n".join(out))
    except OSError as e:
        return app.flash(f"couldn't write {path}: {e}")
    app.flash(f"exported to {path}")


async def cmd_copy(app, rest: str):
    run = app.target_run()
    text = run.result(limit=100_000) if run else ""
    if not text:
        return app.flash("no response to copy yet")
    if shutil.which("wl-copy"):
        p = await asyncio.create_subprocess_exec("wl-copy", stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await p.communicate(text.encode())
    else:
        app.copy_to_clipboard(text)  # terminal clipboard (OSC 52)
    app.flash(f"copied {len(text)} characters from {run.label}")


# ---- setup -------------------------------------------------------------------------------

async def cmd_status(app, rest: str):
    hub = app.hub
    lines = [f"cwd        {os.getcwd()}",
             f"worktrees  {'on' if hub.cfg['worktrees'] else 'off'}",
             f"claude mode  {hub.cfg['claude_mode']}",
             f"delegation socket  {hub.sock_path}",
             f"runs live  {sum(r.live for r in hub.runs.values())} of {len(hub.runs)}", ""]
    names = app.agents()
    adapters = make_adapters()

    async def version(n: str) -> str:
        try:
            rc, out = await asyncio.wait_for(sh(hub.exe(n), "--version"), 5)
        except (asyncio.TimeoutError, OSError):
            return "?"
        return (out.splitlines() or ["?"])[-1][:40]

    versions = await asyncio.gather(*(version(n) for n in names))
    lines.append(f"{'agent':<10}{'version':<24}{'model':<20}{'effort':<8}{'permissions':<28}where")
    for n, v in zip(names, versions):
        lines.append(f"{n:<10}{v:<24}{hub.models.get(n, '-'):<20}{hub.efforts.get(n, '-'):<8}"
                     f"{adapters[n].trust:<28}{hub.found[n].path}")
    missing = [n for n in ORDER if n not in hub.found]
    lines += ["", "not installed: " + (", ".join(missing) or "none") + "   (install one, then /rescan)"]
    extra = detect.scan_unsupported(adapters)
    if extra:
        lines.append("installed but not supported yet: " + ", ".join(f"{p} ({path})" for p, path in extra.items()))
    app.show_text("status", "\n".join(lines))


async def cmd_task(app, rest: str):
    if not rest:
        return app.flash("usage: /task [@agents] <prompt>   (plain text replies to the task on screen)")
    await app.submit_task(rest)


async def cmd_primary(app, rest: str):
    """The primary agent starts every new task and is the one that hands work to the others."""
    agent = rest.split()[0] if rest else ""
    if not agent:
        return app.flash(f"primary: {app.cfg['default_agents'][0]} (new tasks start with it; /primary <agent> to change)")
    if agent not in app.agents():
        return app.flash(f"{agent} isn't installed. found: {', '.join(app.agents()) or 'nothing'}")
    app.cfg["default_agents"] = [agent]
    app._update_target()
    app.flash(f"primary is now {agent} (this session; set default_agents in config.toml to keep it)")


async def cmd_usage(app, rest: str):
    words = rest.split()
    if words[:1] == ["refresh"]:
        app.flash("reading limits…")
        err = await app.hub.refresh_usage(claude="claude" in words)
        app._render_usage()
        app.flash(err or "limits refreshed")
    book, agents = app.hub.usage, app.agents()
    body = Text("\n\n").join(usage_view.lines(book, agents, app._colors(), bar_width=30, explain=True)) \
        if agents else Text("no agents found")
    body.append("\n\nclaude's bars update whenever claude runs (or: /usage refresh claude, which makes one tiny"
                "\nhaiku turn and counts against your limit). codex's are read live. alt+u switches the strip.",
                style="grey50")
    app.show_text("usage limits", body)


async def cmd_rescan(app, rest: str):
    app.rescan()


async def cmd_config(app, rest: str):
    cfg = app.cfg
    if not rest:
        return app.show_text("config (this session)", "\n".join([
            f"worktrees       {'on' if cfg['worktrees'] else 'off'}",
            f"claude_mode     {cfg['claude_mode']}   (auto = claude's classifier decides; manual = asks about nearly everything)",
            f"default_agents  {', '.join(cfg['default_agents'])}",
            f"max_depth       {cfg['max_depth']}",
            f"max_delegations {cfg['max_delegations']}", "",
            "change with /config worktrees=off claude_mode=manual",
            "lasts until you quit; put it in ~/.config/deck/config.toml to keep it"]))
    done = []
    for pair in rest.split():
        key, _, value = pair.partition("=")
        if key == "claude_mode" and app.hub.set_claude_mode(value):
            done.append(f"claude_mode={value}")
            continue
        flag = _flag(value)
        if key != "worktrees" or flag is None:
            return app.flash(f"can't set {pair!r}. use worktrees=on|off or claude_mode=auto|manual|acceptEdits")
        cfg[key] = flag
        done.append(f"{key}={'on' if flag else 'off'}")
    app.flash("set " + ", ".join(done) + " (next task)")


async def cmd_plan(app, rest: str):
    if not rest:
        return app.flash("usage: /plan <task>  (agents propose a plan and don't change files)")
    await app.submit_task(rest, plan=True)


async def cmd_model(app, rest: str):
    words = rest.split()
    if len(words) >= 2 and words[0] in app.avail:
        app._model_picked((words[0], words[1] if words[1] != "default" else ""))
    else:
        app.action_pick_model()


async def cmd_effort(app, rest: str):
    words = rest.split()
    if len(words) >= 2 and words[0] in EFFORTS and words[0] in app.avail:
        app._effort_picked((words[0], words[1] if words[1] != "default" else ""))
    else:
        app.action_pick_effort()


async def cmd_resume(app, rest: str):
    app.open_resume("all" in rest.split())


async def cmd_help(app, rest: str):
    lines = [f"{c.name:<9}{c.args:<24}{c.desc}" + (f"  (also {' '.join(c.aliases)})" if c.aliases else "")
             for c in COMMANDS]
    lines += ["", "anything else starting with / goes to the agent as a prompt (e.g. a Claude skill).",
              "keys: alt+1..5 default agents · ctrl+o model · alt+e effort · tab panes · esc back to input",
              "in a pane: d diff · m merge · x kill · f follow-up · D discard"]
    app.show_text("commands", "\n".join(lines))


COMMANDS: list[Cmd] = [
    Cmd("/new", "[name]", "blank slate; the old task stays in the list", cmd_new, ("/clear", "/reset")),
    Cmd("/task", "[@agents] <prompt>", "start a separate task (plain text replies to the current one)", cmd_task),
    Cmd("/primary", "[agent]", "which agent new tasks start with (it delegates to the others)", cmd_primary),
    Cmd("/resume", "[all]", "open another saved conversation beside the current panes", cmd_resume, ("/continue",)),
    Cmd("/panes", "", "choose conversations to keep open (alt+p)", cmd_panes),
    Cmd("/model", "[agent model|default]", "choose models; stays open to set several", cmd_model, ("/models",)),
    Cmd("/effort", "[agent level|default]", "choose reasoning effort; stays open too", cmd_effort),
    Cmd("/plan", "<task>", "agents plan without changing files", cmd_plan),
    Cmd("/compact", "[focus]", "summarise a claude session's context", cmd_compact),
    Cmd("/diff", "", "changes made by the selected run", cmd_diff),
    Cmd("/usage", "[refresh [claude]]", "limit bars for every agent (5h, weekly...)", cmd_usage, ("/limits",)),
    Cmd("/cost", "", "tokens, time and cost per run", cmd_cost, ("/stats",)),
    Cmd("/stop", "", "stop the selected run (or all live runs)", cmd_stop),
    Cmd("/rename", "<name>", "rename the current task", cmd_rename),
    Cmd("/export", "[file]", "save the task's transcript as markdown", cmd_export),
    Cmd("/copy", "", "copy the selected run's last response", cmd_copy),
    Cmd("/cd", "<path>", "change folder for new tasks", cmd_cd),
    Cmd("/status", "", "agents, versions, folder, settings", cmd_status, ("/doctor",)),
    Cmd("/rescan", "", "look for newly installed agents (alt+r)", cmd_rescan, ("/detect",)),
    Cmd("/config", "[key=value]", "view or change worktrees / claude_mode", cmd_config, ("/settings",)),
    Cmd("/help", "", "list commands and keys", cmd_help),
    Cmd("/exit", "", "quit deck", cmd_exit, ("/quit",)),
]


def lookup(word: str) -> Cmd | None:
    w = word.lower()
    return next((c for c in COMMANDS if w in c.names()), None)


def matches(prefix: str) -> list[Cmd]:
    """Menu entries: a command shows when its name or any alias starts with what's typed."""
    p = prefix.lower()
    return [c for c in COMMANDS if any(n.startswith(p) for n in c.names())]
