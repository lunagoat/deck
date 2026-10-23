# deck

A terminal dashboard for coding agents. Type a task, and `deck` runs it with Claude, Codex, opencode, Antigravity and
friends side by side: one pane per agent, one primary agent that hands work to the others, and a confirm/deny
dialog whenever an agent wants to do something risky.

> **Built with coding agents, and experimental.** This is a personal project that changes quickly; expect rough
> edges. The tests drive simulated agents, so not every real CLI and version has been exercised. The agents marked
> "untested" below follow their documentation and nothing more.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/lunagoat/deck/main/install.sh | sh
```

That clones deck into `~/.local/share/deck-app`, builds a private virtualenv there (the only dependency is
[`textual`](https://github.com/Textualize/textual)), and puts a `deck` launcher in `~/.local/bin`. The script is short;
[read it first](install.sh) if you like. Run it again (or `deck --update`) to update, and
`curl -fsSL https://raw.githubusercontent.com/lunagoat/deck/main/install.sh | sh -s -- --uninstall` to remove it.
Pin a version with `DECK_REF=<tag>`.

**You need:** Linux (the only platform it has been tested on), Python 3.11+, git, and at least one coding-agent CLI
that is installed and logged in. [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) is needed for the
agents deck fences in (see [Safety](#safety)); without it those agents refuse to run.

Manual install: `git clone https://github.com/lunagoat/deck && cd deck && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
then run `PYTHONPATH=/path/to/deck /path/to/deck/.venv/bin/python -m deck` from a project folder.

## Use it

```sh
cd ~/some/project
deck
```

Type a task and press Enter. It goes to the **primary agent** (Claude by default; change it with `/primary`). To involve
others, say so: *"get codex and agy to review this too"*. The primary delegates through `deck-delegate`, each delegate
gets its own pane, and when they finish deck wakes the primary with their results so it never sits waiting.

- **Pick agents yourself**: `@claude @codex ...` in the prompt, `@all`, or toggle defaults with `alt+1`, `alt+2`... (numbers follow the
  agents found on your machine).
- **Replying**: with a task on screen, plain text continues that conversation: the focused pane's agent, otherwise the primary.
  `@codex ...` sends it to codex's run (an agent not yet in the task joins it). If an agent is busy your message is queued and
  delivered when it finishes. The input's placeholder always says who you're replying to. `/new` or `/task <prompt>` start something separate.
- **Worktrees**: in a git repo each run works in its own worktree and branch (`deck/<stamp>-<id>-<agent>` under
  `~/.local/share/deck/worktrees/`), so agents can't trample each other. In a pane (`Tab` to focus, `Esc` to go back to the prompt):
  `d` diff, `m` commit and merge into your current branch, `x` stop, `f` follow-up, `D` discard the worktree and branch.
  Worktrees start from `HEAD`: uncommitted changes in your tree aren't visible to the agents.
- **Panes**: finished panes hide while something else is working and the main conversation stays open when everyone is idle.
  `alt+h` shows all, `/panes` or `alt+p` picks which stay open. Repeated `/resume` opens separate saved conversations
  side by side (`claude-2`, ... reply with `@claude-2`).
- **Text**: agent output is rendered as markdown (tables included). Drag to select, `ctrl+c` copies, `/copy` copies a whole response.

### Commands

Type `/` for a menu (up/down, `Tab` completes, `Enter` runs). Names follow Claude Code's where the idea fits.

| Command | What it does |
|---|---|
| `/new` (`/clear`, `/reset`) | blank slate; the old task stays in the list |
| `/task [@agents] <prompt>` | start a separate task while another is on screen |
| `/resume [all]` | continue an old session (Claude, Codex, opencode, and anything deck ran) |
| `/model`, `/effort` (`alt+e`) | pick models / reasoning effort per agent; stays open so you can set several |
| `/primary [agent]` | which agent new tasks start with |
| `/plan <task>` | agents propose a plan and change nothing |
| `/compact [focus]` | summarise a Claude session's context (Claude only) |
| `/diff`, `/cost`, `/export [file]`, `/copy`, `/rename`, `/stop`, `/cd <path>` | inspect and manage the current task |
| `/usage [refresh [claude]]` (`alt+u`) | limit bars for every agent |
| `/status` (`/doctor`), `/rescan` (`alt+r`), `/config`, `/help`, `/exit` | setup |

Anything else starting with `/` is sent to the agent as an ordinary prompt (a Claude skill, say). The agents' own built-in
TUI commands don't exist when they run headless, so deck reimplements the ones that make sense for a dashboard.

## Agents

deck looks for each CLI on `PATH` and in the usual install dirs (`~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`,
`~/.npm-global/bin`, Homebrew...), and only offers the ones it finds. Installed something new? `/rescan`.
`deck --agents` lists what it sees.

| Agent | How deck drives it | Permissions | Tested |
|---|---|---|---|
| Claude Code | `claude -p` stream-json, its own permission mode (`auto` by default) | its own rules; whatever it asks comes to you | yes |
| Codex | `codex app-server` (JSON-RPC), `on-request` policy in its workspace sandbox | its own rules; asks before `rm` and anything the sandbox blocks | yes |
| opencode | ACP (`opencode acp`) | its own asks reach you; also fenced, `rm` & co ask | yes |
| Antigravity (`agy`) | `agy -p` stream-json | can't ask when headless: fenced, `rm` & co ask | yes |
| Copilot CLI | `copilot -p`, plain text | can't ask when headless: fenced | barely; its JSON mode isn't used yet |
| Gemini CLI, Cursor, Qwen Code, Cline, Kiro, Goose, Kilo, Augment, Mistral Vibe, Junie | ACP (`gemini --acp`, `cursor-agent acp`, ...) | their own asks reach you; also fenced | **no** |

Popular CLIs deck can't drive yet (no ACP server mode that could be verified): Aider, Amp, Crush, Droid, Plandex, OpenHands.
`/status` tells you which of them you have.

**Add your own** in `~/.config/deck/config.toml`. If it speaks ACP (real approve/deny, resumable sessions):

```toml
[agents.myagent]
acp = ["myagent", "--acp"]     # the command that starts its ACP server on stdio
fenced = true                  # default; false turns the sandbox off for an agent whose own asking you trust
```

Otherwise give a headless command; it runs fenced, output is plain text, no resume:

```toml
[agents.mytool]
command = ["mytool", "-p", "{prompt}"]   # {prompt} and {model} are filled in
model_flag = ["--model", "{model}"]      # optional
color = "#4285f4"                        # optional
```

## Safety

deck never passes a skip-permissions or bypass flag to any agent. Where an agent has its own safeguards deck uses them
and only supplies the answers, as a dialog: `y` allow once, `a` allow that kind of thing for this run, `n` or `Esc` deny.
`a` is never offered for `rm`, `git push` and other dangerous commands (`deck/safety.py`), and a yes to an exact command is
remembered for two minutes so two layers asking about the same `rm` don't prompt twice. If deck can't ask you, it says no.

Some agents can't ask at all when run headless (Antigravity's print mode runs everything), and some default to asking
about nothing (opencode doesn't ask about `rm`). For those deck builds the rules itself (`deck/sandbox.py`):

- **bubblewrap** makes the whole filesystem read-only except the run's own folder (plus the repo's `.git` for worktrees, a private
  `/tmp`, and the agent's own state directory), so `rm -rf ~` can't touch anything that matters;
- **shims** for `rm`, `rmdir`, `shred`, `dd`, `truncate`, `sudo`, `kill`... sit first on `PATH` and ask you before running the real
  command. An agent that calls `/usr/bin/rm` by absolute path skips the shim but is still stuck inside the fence.

Claude's `auto` mode ran an explicitly requested `rm -rf` without asking in testing; if you want more stops use
`/config claude_mode=manual` (asks about nearly everything) or `acceptEdits`, or set `claude_mode` in `config.toml`.

The fence guesses where unfamiliar ACP agents keep their state (`~/.name`, `~/.config/name`, ...); an agent that needs to write
elsewhere may fail inside it.

## Usage bars

A strip above the input shows each agent's limit windows as progress bars with reset countdowns (`alt+u` collapses it to one
line, `/usage` has the detail). Claude's come from the `rate_limit_event` in its output stream, so they update whenever Claude runs
(`/usage refresh claude` makes one tiny Haiku turn, which counts against your limit). Codex's are read live from
`codex app-server`. Readings are saved between sessions. Antigravity, opencode and Copilot don't expose limits through their
CLIs, so `/usage` says so instead of guessing.

## Configuration

`~/.config/deck/config.toml` (all optional):

```toml
worktrees = true            # one git worktree per run
claude_mode = "auto"        # auto | manual | acceptEdits   (Claude's own permission modes)
default_agents = ["claude"] # the primary agent
auto_checkin = true         # wake a primary when its delegates finish
max_checkins = 10           # per run, so agents can't wake each other forever
max_depth = 2               # a delegate may delegate once more
max_delegations = 8         # per task
```

State lives in `~/.local/state/deck` (chosen models and efforts, saved sessions, usage readings).

## Development

```sh
.venv/bin/python tests/smoke_commands.py     # commands, adapters, safety rules
.venv/bin/python tests/smoke_permissions.py  # confirm/deny dialogs and the permission MCP server
.venv/bin/python tests/smoke_detect.py       # agent detection, /rescan, custom agents
.venv/bin/python tests/smoke_flow.py         # replies, queueing, check-ins, polling, usage bars
.venv/bin/python tests/smoke_acp.py          # the ACP driver, against tests/fake_acp_agent.py
.venv/bin/python tests/smoke_install.py      # install.sh against a throwaway copy (needs network)
```

The tests are headless and never start a real agent. They run in a temp dir and can't touch your deck state or repos.
The code is about a dozen small modules under `deck/`; `adapters.py` (how each agent is launched), `core.py` (runs, worktrees,
delegation) and `app.py` (the UI) are the places to start.

## License

MIT, see [LICENSE](LICENSE).
