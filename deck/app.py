"""Textual UI: task list on the left, one live pane per agent on the right."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Markdown, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .adapters import EFFORTS, ORDER, make_adapters
from .commands import Cmd, lookup, matches
from .safety import danger
from . import __version__, detect, usage as usage_view
from .core import Hub, Run, Task, load_config, route
from .models import list_models
from .sessions import find_sessions, transcript

ADAPTERS = make_adapters()
EMPTY = ("No tasks yet. Type below and press Enter.\n\n"
         "@claude @codex @all  pick agents inline\n"
         "alt+1..5            toggle default agents\n"
         "(none picked)       goes to claude, which delegates when asked\n"
         "/                   commands: /new /resume /model /effort /plan /help …")
DOT = {"queued": ("○", "grey50"), "running": ("◉", "yellow"), "done": ("●", "green"),
       "failed": ("✗", "red"), "killed": ("■", "grey50")}


def fmt_time(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def fmt_tokens(n: int | None) -> str:
    if not n:
        return ""
    return f"{n / 1000:.1f}k tok" if n >= 1000 else f"{n} tok"


class DiffScreen(ModalScreen):
    BINDINGS = [("escape,q", "app.pop_screen", "close")]
    DEFAULT_CSS = """
    DiffScreen { align: center middle; }
    DiffScreen VerticalScroll { width: 90%; height: 90%; border: round $accent; background: transparent; padding: 0 1; }
    DiffScreen Static { background: transparent; width: auto; }
    """

    def __init__(self, text: str, diff: bool = True, heading: str = ""):
        super().__init__()
        self.text = text
        self.diff = diff  # colour +/- lines; off for plain info screens
        self.heading = heading

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(id="body"))  # a Static, so the text can be selected with the mouse

    def on_mount(self):
        box = self.query_one(VerticalScroll)
        box.border_title = self.heading + "  (esc closes)" if self.heading else "esc closes"
        if isinstance(self.text, Text):  # already styled (the usage bars)
            self.query_one("#body", Static).update(self.text)
            box.focus()
            return
        body = Text(no_wrap=True)
        for line in self.text.splitlines():
            style = ("" if not self.diff else "green" if line.startswith("+") else "red" if line.startswith("-")
                     else "cyan" if line.startswith("@@") else "bold" if line.startswith("diff") else "")
            body.append(line + "\n", style=style)
        self.query_one("#body", Static).update(body)
        box.focus()


class Picker(ModalScreen):
    """Filterable list in a box. Subclasses fill `entries` of (value, label, search text)."""
    BINDINGS = [("escape", "dismiss(None)", "close"), ("down", "move(1)", ""), ("up", "move(-1)", "")]
    DEFAULT_CSS = """
    Picker { align: center middle; background: transparent; }
    #box { width: 90%; max-width: 110; height: 80%; border: round $accent; background: transparent; padding: 0 1; }
    #box OptionList { background: transparent; height: 1fr; }
    #box Input { background: transparent; }
    """
    HELP = ""
    PLACEHOLDER = ""

    def __init__(self, on_choice=None):
        super().__init__()
        self.on_choice = on_choice  # set: apply each pick and stay open; unset: dismiss with the pick
        self.entries: list[tuple[object, Text, str]] = []
        self.shown: list[object] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(self.HELP)
            yield Input(placeholder=self.PLACEHOLDER, id="q")
            yield OptionList(id="list")

    async def on_mount(self):
        self.query_one("#q", Input).focus()
        await self.load()

    async def load(self):
        raise NotImplementedError

    def custom(self, words: list[str]) -> tuple[object, Text] | None:
        return None

    def _rebuild(self):
        """Regenerate entries after a pick (marks move). Only sticky pickers need it."""

    def _refresh(self, keep: bool = False):
        q = self.query_one("#q", Input).value
        terms = q.lower().split()
        ol = self.query_one("#list", OptionList)
        at = ol.highlighted or 0
        ol.clear_options()
        self.shown = []
        for value, label, hay in self.entries:
            if all(t in hay.lower() for t in terms):
                ol.add_option(Option(label))
                self.shown.append(value)
        extra = self.custom(q.split())
        if extra:
            ol.add_option(Option(extra[1]))
            self.shown.append(extra[0])
        if ol.option_count:
            ol.highlighted = min(at, ol.option_count - 1) if keep else 0

    def on_input_changed(self, e: Input.Changed):
        self._refresh()

    def action_move(self, d: int):
        ol = self.query_one("#list", OptionList)
        if ol.option_count:
            ol.highlighted = ((ol.highlighted or 0) + d) % ol.option_count

    def _pick(self, i):
        if i is None or i >= len(self.shown):
            return
        if self.on_choice:
            self.on_choice(self.shown[i])
            self._rebuild()
            self._refresh(keep=True)
        else:
            self.dismiss(self.shown[i])

    def on_input_submitted(self, e: Input.Submitted):
        e.stop()  # don't let the app treat the filter text as a task
        self._pick(self.query_one("#list", OptionList).highlighted)

    def on_option_list_option_selected(self, e: OptionList.OptionSelected):
        self._pick(e.option_index)


class PaneScreen(Picker):
    HELP = "Enter toggles keep-open; pick several, then Esc closes. Active panes always stay visible."
    PLACEHOLDER = "filter by agent or label"

    def __init__(self, runs, pinned, on_choice):
        super().__init__(on_choice)
        self.runs, self.pinned = runs, pinned

    def _rebuild(self):
        self.entries = []
        for run in self.runs:
            mark = "●" if run.rid in self.pinned else "○"
            label = Text(f"{mark} {run.label} · {run.status}")
            self.entries.append((run, label, f"{run.label} {run.agent} {run.status}"))

    async def load(self):
        self._rebuild()
        self._refresh()


class ModelScreen(Picker):
    HELP = "Set models: enter sets one and stays open, so pick as many as you like. esc closes. `agent model` = custom"
    PLACEHOLDER = "e.g. codex   |   opus   |   claude sonnet"

    def __init__(self, agents: list[str], current: dict[str, str], on_choice):
        super().__init__(on_choice)
        self.agents = agents
        self.current = current
        self.cache: dict[str, list[tuple[str, str]]] = {a: [] for a in agents}

    def _rebuild(self):
        self.entries = []
        for a in self.agents:
            rows = [("", "CLI default"), *self.cache[a]]
            cur = self.current.get(a, "")
            if cur and cur not in [m for m, _ in rows]:  # a custom model the CLI didn't list
                rows.insert(1, (cur, "custom"))
            for m, note in rows:
                mark = "●" if cur == m else " "
                label = Text.assemble((f"{mark} {a:9}", ADAPTERS[a].color), (f"{m or '(default)':36}", ""),
                                      (note, "grey50"))
                self.entries.append(((a, m), label, f"{a} {m} {note}"))

    async def load(self):
        self._rebuild()
        self._refresh()
        for a in self.agents:
            self.cache[a] = await list_models(a)
            self._rebuild()
            self._refresh(keep=True)

    def custom(self, words):
        if len(words) >= 2 and words[0] in self.agents:
            return (words[0], words[1]), Text(f"  set custom: {words[0]} {words[1]}", style="italic")


class EffortScreen(Picker):
    HELP = "Set effort: enter sets one and stays open, so pick as many as you like. esc closes"
    PLACEHOLDER = "e.g. codex   |   high   |   claude max"

    def __init__(self, current: dict[str, str], models: dict[str, str], agents: list[str], on_choice):
        super().__init__(on_choice)
        self.current, self.models, self.agents = current, models, agents

    def _rebuild(self):
        self.entries = []
        for a in self.agents:
            if a not in EFFORTS:
                continue
            for lvl in ["", *EFFORTS[a]]:
                mark = "●" if self.current.get(a, "") == lvl else " "
                note = ("CLI default" if not lvl else "needs a model set (/model)"
                        if a == "opencode" and not self.models.get(a) else "")
                label = Text.assemble((f"{mark} {a:9}", ADAPTERS[a].color), (f"{lvl or '(default)':12}", ""),
                                      (note, "grey50"))
                self.entries.append(((a, lvl), label, f"{a} {lvl}"))

    async def load(self):
        self._rebuild()
        self._refresh()


class SessionScreen(Picker):
    PLACEHOLDER = "filter by agent or words from the prompt"

    def __init__(self, cwd: str, everywhere: bool):
        super().__init__()
        self.cwd, self.everywhere = cwd, everywhere
        self.HELP = ("Resume a session " + ("(all folders)" if everywhere else f"in {cwd}")
                     + ". enter resumes it, esc closes")

    async def load(self):
        self.query_one("#list", OptionList).add_option(Option(Text("loading…", style="grey50")))
        for s in await find_sessions(self.cwd, self.everywhere):
            age = time.time() - s.ts
            when = (f"{int(age // 60)}m" if age < 3600 else f"{int(age // 3600)}h" if age < 86400
                    else f"{int(age // 86400)}d")
            where = "" if s.cwd == self.cwd else "  " + os.path.relpath(s.cwd, self.cwd)[-28:]
            label = Text.assemble((f"{s.agent:9}", ADAPTERS[s.agent].color), (f"{when:>4}  ", "grey50"),
                                  (s.title[:70], ""), (where, "grey50"))
            self.entries.append((s, label, f"{s.agent} {s.title} {s.cwd}"))
        self._refresh()
        if not self.entries:
            self.query_one("#list", OptionList).add_option(Option(Text("no sessions found here. try /resume all", style="grey50")))


class PermissionScreen(ModalScreen[str]):
    """An agent wants to run something. Nothing is pre-selected: y allows once, a allows the same
    kind of thing for the rest of this run, n or esc denies."""
    BINDINGS = [("y", "answer('allow')", "allow"), ("a", "answer('always')", "always"),
                ("n,escape", "answer('deny')", "deny")]
    DEFAULT_CSS = """
    PermissionScreen { align: center middle; background: transparent; }
    #perm { width: 90%; max-width: 110; height: auto; max-height: 80%; border: heavy $warning;
            background: transparent; padding: 0 1; }
    #perm Static { background: transparent; height: auto; }
    """

    def __init__(self, run: Run, tool: str, inp: dict, key: str | None):
        super().__init__()
        self.run, self.tool, self.inp, self.key = run, tool, inp, key

    def compose(self) -> ComposeResult:
        if self.tool == "Bash":
            detail = str(self.inp.get("command", ""))
            if self.inp.get("description"):
                detail += f"\n\n{self.inp['description']}"
        else:
            detail = json.dumps(self.inp, indent=2, ensure_ascii=False)[:1500]
        who = f"{self.run.label} ({self.run.agent})"
        why = danger(detail) if self.tool == "Bash" else None
        opts = Text.assemble(("[y] allow once", "bold green"), "    ",
                             (f"[a] always allow '{self.key}' for this run", "bold cyan") if self.key
                             else ("[a] n/a: only offered for simple single commands", "grey50"),
                             "    ", ("[n] deny", "bold red"), (" (esc)", "grey50"))
        with Vertical(id="perm"):
            yield Static(Text.assemble((f"{who} wants to use ", "bold"), (self.tool, "bold yellow"),
                                       (f"   in {self.run.cwd}", "grey50")))
            if why:
                yield Static(Text(f"⚠ {why}: asked every time", style="bold red"))
            yield Static(Text(detail))
            yield Static(opts)

    def action_answer(self, answer: str):
        if answer == "always" and not self.key:
            return
        self.dismiss(answer)


class MdBlock:
    """A run of agent text shown as one Markdown widget that grows as lines arrive.

    Markdown.update() is asynchronous and overlapping calls clobber each other, so updates are
    coalesced: at most one in flight, and it always ends on the latest text."""

    def __init__(self, log: VerticalScroll):
        self.log, self.text, self.widget = log, "", None
        self.mounted = None
        self.dirty = self.busy = False

    def append(self, line: str):
        self.text = f"{self.text}\n{line}" if self.text else line
        self.dirty = True

    def kick(self):
        if self.widget is None:  # first render: the widget parses everything gathered so far
            self.widget = Markdown(self.text)
            self.mounted = self.log.mount(self.widget)
            self.dirty = False
        elif self.dirty and not self.busy:
            self.busy = True
            asyncio.create_task(self._work())

    async def _work(self):
        try:
            await self.mounted
            while self.dirty:
                self.dirty = False
                await self.widget.update(self.text)
        finally:
            self.busy = False


class RunPane(Vertical, can_focus=True):
    """Live output for one agent on one task."""
    BINDINGS = [
        Binding("d", "diff", "diff"),
        Binding("m", "merge", "merge"),
        Binding("x", "kill", "kill"),
        Binding("f", "follow", "follow-up"),
        Binding("D", "discard", "discard"),
        Binding("up,k", "scroll(-3)", show=False),
        Binding("down,j", "scroll(3)", show=False),
        Binding("pageup", "scroll(-20)", show=False),
        Binding("pagedown", "scroll(20)", show=False),
        Binding("end,G", "scroll_end", show=False),
    ]

    def __init__(self, run: Run):
        super().__init__()
        self.run = run
        self.shown = 0
        self.follow_tail = True
        self.block: MdBlock | None = None  # the agent-text block still being appended to
        self._discard_armed = 0.0

    def compose(self) -> ComposeResult:
        # Plain widgets (Markdown for agent text, Static for the rest) instead of a RichLog:
        # they render tables and the text in them can be selected with the mouse.
        yield Static(id="head")
        yield VerticalScroll(id="log")
        yield Static(id="foot")

    def on_mount(self):
        r = self.run
        self.border_title = f"{r.parent.label} → {r.label}" if r.parent else r.label
        self._starred = False
        self.styles.border = ("round", ADAPTERS[self.run.agent].color)
        self.refresh_all()

    def refresh_all(self):
        """Idempotent: re-render header/footer and append any new lines."""
        r = self.run
        icon, color = DOT[r.status]
        meta = " · ".join(x for x in (r.model, r.effort, fmt_time(r.elapsed), fmt_tokens(r.tokens),
                                      f"${r.cost:.3f}" if r.cost else "",
                                      "merged" if r.merged else "") if x)
        if r.parent is None and not self._starred and any(c.parent is r for c in r.task.runs.values()):
            self._starred = True  # it has delegated: mark it as the primary
            self.border_title = "★ " + self.border_title
        trust = ADAPTERS[r.agent].trust
        head = Text.assemble((f"{icon} {r.status}", color), (f"  {meta}", "grey62"),
                             (f"  ({trust})" if trust else "", "grey50"))
        if r.waiting:
            head.append(f"  ⏸ waiting for you: {r.waiting[:60]}", style="bold yellow")
        self.query_one("#head", Static).update(head)
        log = self.query_one("#log", VerticalScroll)
        new = r.lines[self.shown:]
        for kind, text in new:
            if kind == "text" and not text.startswith("──"):
                # consecutive text lines are one markdown document, so tables and lists survive
                self.block = self.block or MdBlock(log)
                self.block.append(text)
                continue
            if self.block:
                self.block.kick()  # its widget must be mounted before whatever comes after it
                self.block = None
            line = (Text("▸ " + text, style="grey62 italic") if kind == "tool"
                    else Text("✗ " + text, style="red") if kind == "error"
                    else Text(text, style="cyan"))
            log.mount(Static(line, classes="entry"))
        if self.block:
            self.block.kick()
        if new:
            self.shown = len(r.lines)
            if self.follow_tail:
                self.call_after_refresh(log.scroll_end, animate=False)
        self.query_one("#foot", Static).update(Text(r.summary.replace("\n", " ⏎ ")[:300], style="grey62"))

    # -- actions ---------------------------------------------------------------
    def action_scroll(self, n: int):
        self.follow_tail = False
        self.query_one("#log", VerticalScroll).scroll_relative(y=n, animate=False)

    def action_scroll_end(self):
        self.follow_tail = True
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    async def action_diff(self):
        self.app.push_screen(DiffScreen(await self.app.hub.diff(self.run)))

    async def action_merge(self):
        self.app.flash(await self.app.hub.merge(self.run))
        self.refresh_all()

    def action_kill(self):
        self.app.hub.kill(self.run)

    def action_follow(self):
        self.app.begin_follow_up(self.run)

    async def action_discard(self):
        if time.time() - self._discard_armed < 4:
            self.app.flash(await self.app.hub.discard(self.run))
            self.refresh_all()
        else:
            self._discard_armed = time.time()
            self.app.flash("press D again to delete this worktree + branch")


class DeckApp(App):
    TITLE = "deck"
    CSS = """
    Screen, Header, Footer, Horizontal, Vertical, Container, Static, Input, OptionList, RichLog,
    RunPane, ModalScreen { background: transparent; }
    Input, Input:focus { border: tall $primary-darken-2; padding: 0 1; }
    Input:focus { border: tall $accent; }
    Footer { background: transparent; }
    FooterKey, FooterLabel { background: transparent; }
    #side { width: 34; border-right: solid $primary-darken-2; }
    #tasks { height: 1fr; background: transparent; }
    #grid { layout: grid; grid-gutter: 0 1; height: 1fr; }
    RunPane { border: round grey; height: 100%; }
    OptionList > .option-list--option-highlighted { background: $primary 30%; }
    RunPane:focus { border-title-style: bold reverse; }
    RunPane #head { height: 1; padding: 0 1; }
    RunPane #log { height: 1fr; padding: 0 1; background: transparent; overflow-x: hidden; }
    RunPane .entry { height: auto; background: transparent; }
    RunPane Markdown { height: auto; margin: 0; padding: 0; background: transparent; }
    RunPane Markdown > * { background: transparent; }
    * { scrollbar-size-vertical: 1; scrollbar-background: transparent; scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent; scrollbar-color: $primary 40%; scrollbar-corner-color: transparent; }
    RunPane #foot { height: auto; max-height: 2; padding: 0 1; }
    #chips { height: 1; padding: 0 1; }
    #usage { height: auto; padding: 0 1; }
    #usage.hidden { display: none; }
    #slash { display: none; height: auto; max-height: 6; padding: 0 1; border: round $primary-darken-2; }
    #slash.open { display: block; }
    #flash { height: 1; padding: 0 1; color: $warning; }
    .empty { content-align: center middle; height: 1fr; color: $text-muted; }
    """
    BINDINGS = [
        Binding("escape", "focus_input", "input", show=False),
        Binding("ctrl+t", "focus_tasks", "tasks"),
        Binding("tab", "next_pane", "next pane", priority=True),  # priority: Screen's own tab = focus_next
        Binding("down", "menu_move(1)", show=False),
        Binding("up", "menu_move(-1)", show=False),
        Binding("ctrl+o", "pick_model", "model"),
        Binding("alt+e", "pick_effort", "effort"),
        Binding("ctrl+w", "toggle_wt", "worktrees"),
        Binding("ctrl+q", "quit", "quit"),
        Binding("alt+r", "rescan", "rescan"),
        Binding("alt+u", "toggle_usage", "usage"),
        Binding("alt+h", "toggle_finished", "finished panes"),
        Binding("alt+p", "pick_panes", "pick panes"),
        *[Binding(f"alt+{i + 1}", f"toggle_agent({i})", show=False) for i in range(9)],
    ]

    def __init__(self):
        super().__init__(ansi_color=True)  # use the terminal's own colours/background
        self.theme = "ansi-dark"
        self.cfg = load_config()
        ADAPTERS.clear()
        ADAPTERS.update(make_adapters())  # now includes any [agents.*] from the config
        self.hub = Hub(self.cfg, self._on_run_change)
        self.hub.asker = self.ask_permission
        self._ask_lock = asyncio.Lock()  # one question on screen at a time, the rest queue
        self.chosen: set[str] = set()      # empty = auto-route
        self.avail = self.hub.available()
        self.current: Task | None = None
        self.panes: dict[int, dict[str, RunPane]] = {}
        self.follow: Run | None = None
        self.slash_items: list[Cmd] = []
        self.usage_full = True  # alt+u: one line per agent, or everything on one line
        self.show_finished = False
        self.pinned_panes: set[int] = set()
        self._dirty_tasks = True

    # -- layout ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="side"):
                yield OptionList(id="tasks")
            with Vertical():
                yield Container(Static(EMPTY, classes="empty"), id="grid")
                yield Static(id="flash")
                yield OptionList(id="slash")
                yield Static(id="usage")
                yield Static(id="chips")
                yield Input(placeholder="describe a task…", id="prompt")
        yield Footer()

    async def on_mount(self):
        await self.hub.serve()
        self.query_one("#prompt", Input).focus()
        self.set_interval(1.0, self._tick)
        self.set_interval(20.0, self._render_usage)  # countdowns
        self.set_interval(300.0, lambda: self.run_worker(self._refresh_usage_bg(), exclusive=False))
        self.run_worker(self._refresh_usage_bg(), exclusive=False)
        self._render_chips()
        self._render_usage()
        self._update_target()
        wt = "on" if self.cfg["worktrees"] else "off"
        problems = self.cfg.get("problems", [])
        self.flash(("config: " + "; ".join(problems)) if problems else
                   f"cwd: {os.getcwd()} · worktrees {wt} · " + self.found_summary())

    # -- usage bars ----------------------------------------------------------------------
    def _colors(self) -> dict[str, str]:
        return {n: a.color for n, a in ADAPTERS.items()}

    def _render_usage(self):
        book, agents = self.hub.usage, self.agents()
        shown = [n for n in agents if n in book.data]
        box = self.query_one("#usage", Static)
        if not shown:
            box.add_class("hidden")
            return
        box.remove_class("hidden")
        if self.usage_full:
            box.update(Text("\n").join(usage_view.lines(book, shown, self._colors(), bar_width=14)))
        else:
            box.update(usage_view.compact(book, shown, self._colors()))

    def action_toggle_usage(self):
        self.usage_full = not self.usage_full
        self._render_usage()

    async def _refresh_usage_bg(self):
        await self.hub.refresh_usage()
        self._render_usage()

    # -- replying: typing continues the conversation you're looking at -------------------------
    def primary_run(self, task: Task | None = None) -> Run | None:
        """The task's first run: the one you started it with, and the one that delegates to the others."""
        task = task or self.current
        return next((r for r in task.runs.values() if r.parent is None), None) if task else None

    def reply_target(self) -> Run | None:
        return self.focused_run() or self.primary_run()

    def _default_placeholder(self) -> str:
        t = self.reply_target()
        if self.current and t:
            return f"reply to {t.label} · @agent to ask another · /new for a new task"
        return "describe a task…"

    def _update_target(self):
        self.query_one("#prompt", Input).placeholder = self._default_placeholder()
        self._render_chips()

    def on_descendant_focus(self, e):
        self._update_target()

    async def reply(self, text: str):
        """Send `text` into the task on screen: to the focused pane's run, else the primary run.
        @agent (or @label) sends it to that run instead; an agent not in the task yet joins it."""
        task = self.current
        tags = re.findall(r"(?<!\S)@([\w-]+)", text)
        body = re.sub(r"(?<!\S)@[\w-]+\s*", "", text).strip() or text
        said = []
        if not tags:
            run = self.reply_target()
            return self.flash(self.hub.reply(run, body)) if run else self.flash("nothing to reply to")
        for tag in dict.fromkeys(tags):
            runs = list(task.runs.values()) if tag == "all" else (
                [task.runs[tag]] if tag in task.runs else [r for r in task.runs.values() if r.agent == tag][:1])
            if runs:
                said += [self.hub.reply(r, body) for r in runs]
            elif tag in self.avail:
                await self.hub.start_in_task(task, tag, body)
                said.append(f"started {tag} in this task")
            else:
                said.append(f"{tag} isn't installed")
        self.flash("; ".join(said))

    # -- slash command menu ----------------------------------------------------------
    def on_input_changed(self, e: Input.Changed):
        if e.input.id != "prompt":
            return
        v = e.value
        self.slash_items = matches(v) if v.startswith("/") and " " not in v else []
        ol = self.query_one("#slash", OptionList)
        ol.clear_options()
        for c in self.slash_items:
            ol.add_option(Option(Text.assemble((f"{c.name:9}", "bold"), (f"{c.args:24}", "grey62"),
                                               (c.desc, "grey50"))))
        ol.set_class(bool(self.slash_items), "open")
        if self.slash_items:
            ol.highlighted = 0

    def action_menu_move(self, d: int):
        ol = self.query_one("#slash", OptionList)
        if self.slash_items:
            ol.highlighted = ((ol.highlighted or 0) + d) % len(self.slash_items)

    def _menu_choice(self) -> str:
        ol = self.query_one("#slash", OptionList)
        return self.slash_items[ol.highlighted or 0].name

    def on_unmount(self):
        for run in list(self.hub.runs.values()):  # don't leave agents running after deck quits
            self.hub.kill(run)
        self.hub.close()

    # -- chips / input -------------------------------------------------------------
    def agents(self) -> list[str]:
        """The agents this machine has, in display order. Everything else is simply not offered."""
        return [n for n in ORDER if n in self.avail]

    def _render_chips(self):
        t = Text()
        for i, n in enumerate(self.agents()):
            on = n in self.chosen
            mdl = "·".join(x for x in (self.hub.models.get(n), self.hub.efforts.get(n)) if x)
            label = f"{'[x]' if on else '[ ]'} {i + 1}:{n}" + (f"({mdl})" if mdl else "")
            t.append(label + "  ", style=f"bold {ADAPTERS[n].color}" if on else "grey62")
        if not self.agents():
            t.append("no coding agents found. install one (claude, codex, opencode, agy) then /rescan",
                     style="bold yellow")
        elif self.current and (target := self.reply_target()):
            t.append(f"↩ replies go to {target.label}", style="italic grey62")
        elif not self.chosen:
            t.append(f"→ {route('', self.cfg, self.avail)[0]}", style="italic grey62")
        self.query_one("#chips", Static).update(t)

    def action_toggle_agent(self, i: int):
        agents = self.agents()
        if i >= len(agents):
            return self.flash("no agent in that slot (alt+r rescans for newly installed ones)")
        self.chosen ^= {agents[i]}
        self._render_chips()

    def action_rescan(self):
        self.rescan()

    def rescan(self):
        """Look for installed agents again (after you install or remove one) and refresh everything."""
        new, gone = self.hub.rescan()
        self.avail = self.hub.available()
        self.chosen &= self.avail
        self._render_chips()
        self.flash(self.found_summary(new, gone))

    def found_summary(self, new: list[str] | None = None, gone: list[str] | None = None) -> str:
        found = self.agents()
        msg = ("agents: " + (", ".join(found) if found else "none found")
               + (f" · new: {', '.join(new)}" if new else "") + (f" · gone: {', '.join(gone)}" if gone else ""))
        extra = detect.scan_unsupported(make_adapters())
        if extra:
            msg += " · also installed but not supported yet: " + ", ".join(extra) + " (see [agents.*] in the README)"
        return msg

    async def _slash(self, text: str) -> bool:
        """deck's own commands (see commands.py). Anything else, e.g. a Claude skill like /review,
        returns False and is sent to the agent as a normal prompt."""
        word, _, rest = text.partition(" ")
        cmd = lookup(word)
        if not cmd:
            return False
        await cmd.run(self, rest.strip())
        return True

    async def ask_permission(self, run: Run, tool: str, inp: dict, key: str | None) -> str:
        async with self._ask_lock:
            done: asyncio.Future = asyncio.get_running_loop().create_future()
            self.push_screen(PermissionScreen(run, tool, inp, key),
                             lambda answer: done.done() or done.set_result(answer or "deny"))
            self.bell()
            return await done

    # -- helpers the commands use ----------------------------------------------------
    def focused_run(self) -> Run | None:
        return self.focused.run if isinstance(self.focused, RunPane) else None

    def target_run(self) -> Run | None:
        """The pane a command means: the focused one, else the task's first live (or first) run."""
        if (r := self.focused_run()):
            return r
        runs = list(self.current.runs.values()) if self.current else []
        return next((r for r in runs if r.live), runs[0] if runs else None)

    def show_text(self, title: str, text, diff: bool = False):
        self.push_screen(DiffScreen(text, diff=diff, heading=title))

    def show_empty(self):
        """Back to the blank 'new task' view without deleting anything."""
        self.current = None
        self.follow = None
        grid = self.query_one("#grid", Container)
        grid.remove_children()
        grid.styles.grid_size_columns = 1
        grid.mount(Static(EMPTY, classes="empty"))
        self.title = "deck"
        self._update_target()
        self.query_one("#prompt", Input).focus()

    def open_resume(self, everywhere: bool):
        self.push_screen(SessionScreen(os.getcwd(), everywhere), self._resume_picked)

    def _resume_picked(self, s):
        if not s:
            return
        run = self.hub.attach(s.agent, s.id, s.cwd, s.title, transcript(s), task=self.current)
        # Explicitly opened conversations stay visible together, including idle ones.
        self.pinned_panes.add(run.rid)
        self.show_task(run.task)
        self._dirty_tasks = True
        self.begin_follow_up(run)
        self.flash(f"opened {run.label}: {s.title} · type to continue · /resume adds another conversation")

    def action_pick_model(self):
        agents = self.agents()
        self.push_screen(ModelScreen(agents, self.hub.models, self._model_picked))

    def _model_picked(self, choice):
        if choice:
            agent, model = choice
            self.hub.set_model(agent, model)
            self._render_chips()
            self.flash(f"{agent} → {model or 'CLI default'} (next run; follow-ups too)")

    def action_pick_effort(self):
        agents = self.agents()
        self.push_screen(EffortScreen(self.hub.efforts, self.hub.models, agents, self._effort_picked))

    def _effort_picked(self, choice):
        if choice:
            agent, lvl = choice
            self.hub.set_effort(agent, lvl)
            self._render_chips()
            hint = " (opencode needs a model set too)" if agent == "opencode" and not self.hub.models.get(agent) else ""
            self.flash(f"{agent} effort → {lvl or 'CLI default'} (next run; follow-ups too){hint}")

    def action_toggle_wt(self):
        self.cfg["worktrees"] = not self.cfg["worktrees"]
        self.flash(f"worktrees {'on' if self.cfg['worktrees'] else 'off'} (applies to next task)")

    def flash(self, msg: str):
        self.query_one("#flash", Static).update(escape(msg))

    def begin_follow_up(self, run: Run):
        if run.live or not run.session:
            return self.flash("can only follow up on a finished run that has a session")
        self.follow = run
        inp = self.query_one("#prompt", Input)
        inp.placeholder = f"follow-up to {run.agent}…  (esc cancels)"
        inp.focus()

    def action_focus_input(self):
        if self.slash_items:
            self.query_one("#prompt", Input).value = ""
        self.follow = None
        inp = self.query_one("#prompt", Input)
        inp.focus()
        self._update_target()

    def action_focus_tasks(self):
        self.query_one("#tasks", OptionList).focus()

    def action_next_pane(self):
        if len(self.screen_stack) > 1:  # a picker/diff screen is open
            return
        if self.slash_items:  # tab completes the highlighted command, like a shell
            inp = self.query_one("#prompt", Input)
            inp.value = self._menu_choice() + " "
            inp.cursor_position = len(inp.value)
            return
        panes = [p for p in self.panes.get(self.current.id, {}).values() if p.display] if self.current else []
        if not panes:
            return
        f = self.focused
        i = panes.index(f) + 1 if f in panes else 0
        panes[i % len(panes)].focus()

    async def on_input_submitted(self, e: Input.Submitted):
        if e.input.id != "prompt":
            return
        text = e.value.strip()
        if self.slash_items and " " not in text:
            text = self._menu_choice()  # enter on a partial command runs the highlighted one
        if not text:
            return
        e.input.value = ""
        if text.startswith("/") and await self._slash(text):
            return
        if self.follow and not re.search(r"(?<!\S)@[\w-]+", text):
            run, self.follow = self.follow, None
            self._update_target()
            await self.hub.follow_up(run, text)
            return
        if self.current and self.current.runs:
            self.follow = None
            return await self.reply(text)  # use /new (or /task) to start something separate
        await self.submit_task(text)

    async def submit_task(self, text: str, plan: bool = False):
        """Start a task. @agent tags win, then the toggled chips, then the default (claude)."""
        tags = re.findall(r"(?<!\S)@(\w+)", text)
        prompt = re.sub(r"(?<!\S)@\w+\s*", "", text).strip() or text
        if "all" in tags:
            agents = self.agents()
        elif tags:
            bad = [t for t in tags if t not in self.avail]
            if bad:
                return self.flash(f"unavailable: {', '.join(bad)}")
            agents = [t for t in ORDER if t in tags]
        elif self.chosen:
            agents = [n for n in ORDER if n in self.chosen]
        else:
            agents = route(prompt, self.cfg, self.avail)
            self.flash(f"auto-routed to {', '.join(agents)}")
        if not agents:
            return self.flash("no agents available")
        task = await self.hub.start_task(prompt, agents, os.getcwd(), plan=plan)
        self._dirty_tasks = True
        self.show_task(task)

    # -- task list -------------------------------------------------------------------
    def _task_prompt(self, t: Task) -> Text:
        s = Text()
        s.append(f"#{t.id} ", style="grey50")
        s.append(t.title[:26] + "\n")
        for n, r in t.runs.items():
            icon, color = ("⏸", "bold yellow") if r.waiting else DOT[r.status]
            s.append(f"{icon}", style=color)
            s.append(f"{n[:6]} ", style="grey62")
        return s

    def _render_tasks(self):
        ol = self.query_one("#tasks", OptionList)
        hi = ol.highlighted
        ol.clear_options()
        for t in reversed(self.hub.tasks):
            ol.add_option(Option(self._task_prompt(t), id=str(t.id)))
        if hi is not None and ol.option_count:
            ol.highlighted = min(hi, ol.option_count - 1)

    def on_option_list_option_highlighted(self, e: OptionList.OptionHighlighted):
        # only when the user is moving through the list: re-rendering it also fires this event
        if e.option_id and e.option_list.has_focus:
            t = next((t for t in self.hub.tasks if str(t.id) == e.option_id), None)
            if t and t is not self.current:
                self.show_task(t)

    def show_task(self, task: Task):
        self.current = task
        grid = self.query_one("#grid", Container)
        grid.remove_children()
        self._set_columns(len(task.runs))
        panes = {}
        for name, run in task.runs.items():
            panes[name] = RunPane(run)
        self.panes[task.id] = panes
        grid.mount(*panes.values())
        self._sync_pane_visibility()
        self.call_after_refresh(self._sync_pane_visibility)
        self.title = f"deck — #{task.id} {task.title}"
        self._update_target()

    def _set_columns(self, n: int):
        self.query_one("#grid", Container).styles.grid_size_columns = 1 if n <= 1 else 3 if n in (3, 5, 6) else 2

    def action_toggle_finished(self):
        self.show_finished = not self.show_finished
        self._sync_pane_visibility()

    def action_pick_panes(self):
        if not self.current:
            return self.flash("pick a task first")
        self.push_screen(PaneScreen(list(self.current.runs.values()), self.pinned_panes,
                                   self._toggle_pane_pin))

    def _toggle_pane_pin(self, run: Run):
        self.pinned_panes.symmetric_difference_update({run.rid})
        self._sync_pane_visibility()

    def _sync_pane_visibility(self):
        if not self.current:
            return
        panes = list(self.panes.get(self.current.id, {}).values())
        active = any(p.run.live or p.run.waiting for p in panes)
        # Keep the main conversation readable when all agents are idle, including
        # sessions opened through /resume. Only collapse it while another run works.
        fallback = self.primary_run() or (panes[0].run if panes else None)
        visible = 0
        for pane in panes:
            show = (self.show_finished or pane.run.rid in self.pinned_panes
                    or pane.run.live or bool(pane.run.waiting)
                    or (not active and pane.run is fallback))
            if not show and (pane.has_focus or pane.has_focus_within):
                self.query_one("#prompt", Input).focus()
            pane.display = show
            visible += bool(show)
        self._set_columns(visible)

    # -- updates from the hub ----------------------------------------------------------
    def _on_run_change(self, run: Run):
        self._dirty_tasks = True
        if self.current and run.task is self.current:
            panes = self.panes.setdefault(self.current.id, {})
            if run.label not in panes:  # a delegated run just appeared
                panes[run.label] = RunPane(run)
                self.query_one("#grid", Container).mount(panes[run.label])
                self.flash(f"{run.parent.label if run.parent else '?'} delegated to {run.label}")
            elif panes[run.label].is_mounted:
                panes[run.label].refresh_all()
            self._sync_pane_visibility()
        if not run.live:
            if not run.task.live:
                self.bell()

    def _tick(self):
        if self.current:
            for p in self.panes.get(self.current.id, {}).values():
                if p.is_mounted and p.run.live:
                    p.refresh_all()
        if self._dirty_tasks:
            self._dirty_tasks = False
            self._render_tasks()


HELP = """usage: deck [--agents | --version | --update | --help]

  (no option)   open the dashboard in the current folder
  --agents      list the coding agents deck found on this machine, then exit
  --version     print the version
  --update      fast-forward this install to the latest commit (a git checkout)

Inside deck, type / for commands. See the README."""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        DeckApp().run()
        return 0
    flag = args[0]
    if flag in ("-h", "--help"):
        print(HELP)
    elif flag in ("-V", "--version"):
        print(f"deck {__version__}")
    elif flag == "--agents":
        load_config()  # registers [agents.*] from config.toml
        adapters = make_adapters()
        found = detect.scan(adapters)
        for name in ORDER:
            if name in found:
                print(f"{name:<10}{found[name].path}")
        if not found:
            print("none found: install one of " + ", ".join(ORDER[:5]) + " and log in")
        else:
            print("\nnot installed: " + (", ".join(n for n in ORDER if n not in found) or "none"))
        extra = detect.scan_unsupported(adapters)
        if extra:
            print("\nalso installed, not supported yet: " + ", ".join(extra))
    elif flag == "--update":
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], text=True)
        return r.returncode
    else:
        print(f"deck: unknown option {flag}\n\n{HELP}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
