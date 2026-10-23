# deck

**Vibecoded, experimental, and built with coding agents.** This is a personal project that is evolving quickly;
expect rough edges. Tests cover simulated agent flows, but not every real CLI/version has been exercised.

Terminal dashboard: type a task, fan it out to several coding agents, watch them side by side.

Quick start from this checkout (Python 3.11+):

```sh
python3 -m venv .venv
.venv/bin/pip install textual
.venv/bin/python -m deck
```

To work in a different project, run the checkout's Python with the checkout on `PYTHONPATH`:

```sh
cd /path/to/your/project
PYTHONPATH=/path/to/deck /path/to/deck/.venv/bin/python -m deck
```

Agent CLIs must be installed and authenticated separately. Linux sandboxed adapters also require `bwrap` (bubblewrap).

Run `deck` from inside a project. Needs `claude`, `codex`, `opencode`, `agy` and/or `copilot` on PATH
(each already logged in).

- Pick agents: `@claude @codex` in the prompt, `@all`, `alt+1..5` to toggle defaults, or nothing = claude (which delegates when asked).
- Each run gets its own git worktree (`~/.local/share/deck/worktrees/`) on branch `deck/<stamp>-<id>-<agent>`.
- In a pane (Tab / Esc): `d` diff, `m` commit + merge into your current branch, `x` kill,
  `f` follow-up (resumes that agent's session), `D` discard worktree + branch.
- Finished panes hide automatically; queued/running agents and agents waiting for permission stay visible.
  When everyone is idle, the main conversation stays open. Resumed conversations are visible immediately.
  Follow-ups and check-ins bring panes back. `alt+h` toggles finished panes for reviewing results; history is retained.
  `/panes` or `alt+p` opens a picker: Enter toggles each conversation's keep-open setting, and Esc returns.
  Keep several finished panes open together; Tab focuses one to read it or press `f` to reply. Settings last for this deck session.
- Repeat `/resume` to open separate saved conversations side by side, including multiple sessions from the same agent.
  Each keeps its own history, session ID and folder; labels such as `claude-2` let you reply with `@claude-2`.
  Explicitly resumed panes stay open. `/new` before `/resume` starts a separate view.
- Type `/` for a command menu (up/down, tab completes, enter runs). `/model` and `/effort` stay open so you can set several agents in one visit.
  Commands (names follow Claude Code's list where the idea fits; aliases in brackets):
  `/new` (`/clear` `/reset`) fresh start, old tasks stay · `/resume [all]` · `/model` · `/effort` (alt+e) · `/plan <task>` agents plan, change nothing ·
  `/compact` (claude only) · `/diff` · `/cost` (`/usage` `/stats`) · `/stop` · `/rename` · `/export [file]` · `/copy` · `/cd <path>` ·
  `/status` (`/doctor`) · `/config [worktrees=on|off] [claude_mode=auto|manual|acceptEdits]` · `/help` · `/exit` (`/quit`).
  Anything else starting with `/` is sent to the agent as a normal prompt (e.g. a Claude skill). The agents' own built-in TUI commands
  don't exist in headless mode, so deck re-implements the ones that make sense for a dashboard. Commands live in `deck/commands.py`.
- Agent detection: on start deck looks for each supported CLI (`claude`, `codex`, `opencode`, `agy`, `copilot`) on PATH and in the usual
  install dirs (`~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/.npm-global/bin`, Homebrew...), and only offers the ones it finds. Installed
  something new? `/rescan` or `alt+r`. `/status` shows versions and paths, and lists popular agents deck can't drive yet (Gemini CLI, Aider,
  Cursor CLI, Amp, Goose, Crush, Qwen Code, Droid...).
- Any other CLI can be added in `~/.config/deck/config.toml` without code. If it speaks ACP, use `acp` (real approve/deny, resumable);
  `fenced = false` turns the sandbox off for an agent whose own asking you trust:
  ```toml
  [agents.myagent]
  acp = ["myagent", "--acp"]     # the command that starts its ACP server on stdio
  fenced = true                  # default
  ```
  Otherwise give a headless command: it runs fenced in (same sandbox as agy), output as plain text, no resume:
  ```toml
  [agents.gemini]
  command = ["gemini", "-p", "{prompt}"]   # {prompt} and {model} are filled in
  model_flag = ["--model", "{model}"]      # optional
  color = "#4285f4"                        # optional
  ```
- Tests: `.venv/bin/python tests/smoke_commands.py`, `smoke_permissions.py`, `smoke_detect.py`, `smoke_flow.py`, `smoke_acp.py` (uses `tests/fake_acp_agent.py`) (headless, never call a real agent).
- **Replying**: with a task on screen, plain text continues *that* conversation (the primary agent's, or the focused pane's). `@codex ...`
  sends it to codex's run (an agent not in the task yet joins it). If the run is busy your message is queued and delivered when it finishes.
  `/new` (blank slate) or `/task <prompt>` start something separate. The input's placeholder always says who you're replying to.
- **Primary agent + delegation**: new tasks start with the primary agent (`claude`; change with `/primary <agent>`), which hands work to the
  others through `bin/deck-delegate` (it's told how in its system prompt). It can wait for an answer, or `--start` a delegate and keep
  working, `--poll` for progress, or simply end its turn: when its delegates finish, deck **checks in** by resuming the primary with their
  results ("── check-in: codex done"), so it never has to sit and wait. `auto_checkin` / `max_checkins` in config.toml. Delegates get
  the full answer (long ones keep both ends).
- **Usage bars**: a strip above the input shows each agent's limit windows as progress bars with reset countdowns (`alt+u` collapses it to one line,
  `/usage` has the detail): claude from the `rate_limit_event` in its stream (updates whenever claude runs; `/usage refresh claude` makes one tiny
  haiku turn), codex read live from `codex app-server` (5-hour and weekly windows, plan, credits), saved between sessions. agy, opencode and
  copilot don't expose limits through their CLIs, and `/usage` says so rather than guessing.
- Config: `~/.config/deck/config.toml` — `worktrees`, `default_agents`, `[[rules]]` (match regex -> agents).
- Permissions: deck never passes a skip-permissions / bypass flag. Where an agent has its own safeguards, deck uses them and only adds
  the answering: a confirm/deny dialog (`y` allow once, `a` allow that kind of thing for this run, `n`/esc deny; `a` is never offered for
  `rm`, `git push` and other dangerous commands, see `deck/safety.py`).
  - **claude**: its own permission mode, `auto` by default (the same classifier you use in the terminal), with anything it escalates
    answered through `--permission-prompt-tool` (`bin/deck-permission-mcp`). In my tests `auto` ran everything I asked for without
    a question, including an explicitly requested `rm -rf`, so for more stops use `/config claude_mode=manual` (asks about edits,
    shell writes and `rm`, and is noisy) or `acceptEdits`. Set it permanently with `claude_mode = "manual"` in config.toml.
  - **codex**: run through `codex app-server` (JSON-RPC) instead of `codex exec`, with its own `on-request` policy in the workspace-write
    sandbox. It asks before `rm` and anything its sandbox blocks (`deck/codexserver.py`). `exec` can't ask at all.
  - **ACP agents** (opencode built in; Gemini CLI, Cursor, Qwen Code, Cline, Kiro, Goose, Kilo, Augment, Mistral Vibe and Junie when installed):
    driven over the Agent Client Protocol (`deck/acp.py`). An ACP agent sends `session/request_permission` before a tool call it isn't sure
    about, and deck answers with the same dialog, replying with an option id the agent offered. The agent keeps its own safeguards; deck
    only supplies the answers. They also run **fenced in** (below) as a backstop, because a default config may not ask about anything
    (opencode's doesn't ask about `rm`). If both fire for the same command you're asked once: a yes to an exact command is remembered for 2 minutes.
    Only opencode is tested against the real thing; the others follow their docs and are labelled "untested" in the pane header.
  - **agy, copilot**: no ACP server and their headless modes never ask (agy's print mode reports `permission_mode: always-proceed` whatever
    `--mode` says, and `--mode auto` isn't available on this account). So deck builds the rules itself (`deck/sandbox.py`): bubblewrap makes
    everything read-only except the run's folder, and `rm`, `rmdir`, `shred`, `dd`, `truncate`, `sudo`, `kill`... on PATH are shims that
    ask you first. Copilot is untested (not installed).
- Mouse: drag to select text in a pane (agent text is markdown, tables render); `ctrl+c` copies the selection. `/copy` copies a whole response.
- Worktrees start from HEAD: uncommitted changes in your tree are not visible to the agents.
