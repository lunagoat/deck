"""Usage limits per agent, shown as progress bars.

Where the numbers come from (nothing here calls an undocumented web API):
  claude  - the `rate_limit_event` in its stream-json output (5-hour and 7-day windows). It arrives with
            every run, so the bars are as fresh as your last claude run; `/usage refresh` makes one tiny run.
  codex   - `account/rateLimits/read` on `codex app-server`, polled while deck is open and pushed live
            via `account/rateLimits/updated` during a run.
  others  - agy and opencode don't expose limits through their CLIs, so deck says so instead of guessing.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from rich.text import Text

from .paths import STATE

USAGE_FILE = STATE / "usage.json"
NOT_EXPOSED = {
    "agy": "limits aren't exposed by the agy CLI (they live inside its language server)",
    "opencode": "pay-per-use through your provider: no limit windows to show",
    "copilot": "premium-request quota isn't exposed by the CLI",
}
STALE = 30 * 60  # seconds before a reading is shown as old


@dataclass
class Window:
    label: str
    used: float                  # 0..1
    resets_at: float | None = None


@dataclass
class AgentUsage:
    windows: list[Window] = field(default_factory=list)
    note: str = ""
    updated: float = 0.0


def window_label(minutes: int | None, key: str = "") -> str:
    if key:
        return {"five_hour": "5h", "seven_day": "7d"}.get(key, key.replace("_", " "))
    if not minutes:
        return "?"
    return f"{minutes // 1440}d" if minutes % 1440 == 0 and minutes >= 1440 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def claude_windows(info: dict) -> list[Window]:
    """From a stream-json rate_limit_event's rate_limit_info."""
    out = []
    for key, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict) and w.get("utilization") is not None:
            out.append(Window(window_label(None, key), float(w["utilization"]), w.get("resetsAt")))
    return out


def codex_windows(limits: dict) -> tuple[list[Window], str]:
    """From codex's `rateLimits` object (primary + secondary window, optional credits)."""
    out = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if isinstance(w, dict) and w.get("usedPercent") is not None:
            out.append(Window(window_label(w.get("windowDurationMins")), w["usedPercent"] / 100, w.get("resetsAt")))
    credits = limits.get("credits") or {}
    parts = [limits["planType"]] if limits.get("planType") else []
    if credits.get("hasCredits") and not credits.get("unlimited") and credits.get("balance"):
        parts.append(f"{float(credits['balance']):,.0f} credits")
    return out, " · ".join(parts)


class UsageBook:
    """Latest reading per agent, remembered across restarts."""

    def __init__(self):
        self.data: dict[str, AgentUsage] = {}
        try:
            raw = json.loads(USAGE_FILE.read_text())
            for agent, u in raw.items():
                self.data[agent] = AgentUsage([Window(**w) for w in u["windows"]], u.get("note", ""), u.get("updated", 0))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def set(self, agent: str, windows: list[Window], note: str = ""):
        if not windows:
            return
        self.data[agent] = AgentUsage(windows, note, time.time())
        try:
            USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            USAGE_FILE.write_text(json.dumps({a: {"windows": [w.__dict__ for w in u.windows], "note": u.note,
                                                  "updated": u.updated} for a, u in self.data.items()}))
        except OSError:
            pass


def bar(frac: float, width: int = 20) -> Text:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    color = "green" if frac < 0.6 else "yellow" if frac < 0.85 else "red"
    return Text.assemble(("█" * filled, color), ("░" * (width - filled), "grey35"))


def countdown(resets_at: float | None, now: float | None = None) -> str:
    if not resets_at:
        return ""
    left = int(resets_at - (now or time.time()))
    if left <= 0:
        return "reset"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d{h}h" if d else f"{h}h{m:02d}m" if h else f"{m}m"


def lines(book: UsageBook, agents: list[str], colors: dict[str, str], *, bar_width: int = 20,
          explain: bool = False, now: float | None = None) -> list[Text]:
    """One line per agent: `claude  5h ████░░░ 63% resets 2h14m   7d ...`. `explain` adds the agents with no data."""
    now = now or time.time()
    out = []
    for agent in agents:
        u = book.data.get(agent)
        head = (f"{agent:<9}", f"bold {colors.get(agent, 'white')}")
        if not u:
            if explain:
                out.append(Text.assemble(head, (NOT_EXPOSED.get(agent, "no usage data yet (run it once)"), "grey50")))
            continue
        t = Text.assemble(head)
        for w in u.windows:
            t.append(f"{w.label:>3} ")
            t.append_text(bar(w.used, bar_width))
            t.append(f" {w.used * 100:>3.0f}%", style="bold")
            if (cd := countdown(w.resets_at, now)):
                t.append(f" resets {cd}", style="grey50")
            t.append("   ")
        age = now - u.updated
        if age > STALE:
            t.append(f"(as of {int(age // 3600)}h ago)" if age >= 3600 else f"(as of {int(age // 60)}m ago)", style="yellow")
        if u.note:
            t.append(f" {u.note}", style="grey50")
        out.append(t)
    return out


def compact(book: UsageBook, agents: list[str], colors: dict[str, str], *, now: float | None = None) -> Text:
    """Everything on one line: `claude 5h ██████░░ 63%   codex 5h ██░░░░░░ 25%` (first window of each agent)."""
    now = now or time.time()
    t = Text()
    for agent in agents:
        u = book.data.get(agent)
        if not u or not u.windows:
            continue
        w = u.windows[0]
        t.append(f"{agent} ", style=f"bold {colors.get(agent, 'white')}")
        t.append(f"{w.label} ")
        t.append_text(bar(w.used, 8))
        t.append(f" {w.used * 100:.0f}%", style="bold")
        t.append("   ")
    return t
