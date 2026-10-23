"""Test install.sh end to end against a throwaway copy of this checkout. Needs network (pip fetches textual).

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_install.py
"""
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import _env

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, **kw)


# a git repo holding exactly what's in the working tree (committed or not), so the test sees your edits
src = _env.BASE / "src"
for f in run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT).stdout.splitlines():
    if (ROOT / f).exists():
        (src / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, src / f, follow_symlinks=False)
git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
run(["git", "init", "-q", "-b", "main"], cwd=src)
run(["git", "add", "-A"], cwd=src)
run([*git, "commit", "-qm", "test"], cwd=src)

dest, bin_dir = _env.BASE / "app", _env.BASE / "bin"
env = {**os.environ, "DECK_REPO": f"file://{src}", "DECK_HOME": str(dest), "DECK_BIN_DIR": str(bin_dir)}

out = run(["sh", str(ROOT / "install.sh")], env=env)
check("the installer succeeds", out.returncode == 0, out.stdout + out.stderr)
check("it says what it installed and what it found", "installed: deck " in out.stdout and "coding agents found" in out.stdout, out.stdout)
launcher = bin_dir / "deck"
check("a launcher is created and executable", launcher.exists() and os.access(launcher, os.X_OK))
ver = run([str(launcher), "--version"], cwd="/")
want = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
check("the launcher runs from any folder, and the version matches pyproject", ver.stdout.strip() == f"deck {want}", ver.stdout + ver.stderr)
check("--agents works", run([str(launcher), "--agents"], cwd="/").returncode == 0)
check("a private venv holds textual", (dest / ".venv" / "bin" / "python").exists()
      and run([str(dest / ".venv" / "bin" / "python"), "-c", "import textual"]).returncode == 0)
check("it didn't write into the source repo or anywhere else", not (src / ".venv").exists())

again = run(["sh", str(ROOT / "install.sh")], env=env)
check("running it again updates in place", again.returncode == 0 and "updating deck" in again.stdout, again.stdout + again.stderr)

piped = run(["sh", "-c", f"cat '{ROOT / 'install.sh'}' | sh"], env=env)
check("it works the way people run it: piped into sh", piped.returncode == 0 and "updating deck" in piped.stdout, piped.stdout + piped.stderr)

foreign = _env.BASE / "bin2"
foreign.mkdir()
(foreign / "deck").write_text("#!/bin/sh\necho not ours\n")
refused = run(["sh", str(ROOT / "install.sh")], env={**env, "DECK_BIN_DIR": str(foreign), "DECK_HOME": str(_env.BASE / "app2")})
check("it refuses to overwrite a `deck` it didn't create", refused.returncode != 0 and (foreign / "deck").read_text().endswith("not ours\n"), refused.stdout + refused.stderr)

occupied = _env.BASE / "occupied"
occupied.mkdir()
(occupied / "keep.txt").write_text("mine")
refused = run(["sh", str(ROOT / "install.sh")], env={**env, "DECK_HOME": str(occupied), "DECK_BIN_DIR": str(_env.BASE / "bin3")})
check("it refuses to install over an unrelated folder", refused.returncode != 0 and (occupied / "keep.txt").exists(), refused.stdout + refused.stderr)

gone = run(["sh", "-c", f"cat '{ROOT / 'install.sh'}' | sh -s -- --uninstall"], env=env)  # the README's exact form
check("--uninstall removes the app and launcher", gone.returncode == 0 and not dest.exists() and not launcher.exists(), gone.stdout + gone.stderr)
check("--uninstall leaves other files alone", (foreign / "deck").exists() and (occupied / "keep.txt").exists())

bad_py = run(["sh", "-c", "PATH=/nonexistent /bin/sh " + str(ROOT / "install.sh")], env=env)
check("a missing git/python is a clear error, not a crash", bad_py.returncode != 0 and "required" in bad_py.stderr, bad_py.stderr)

bad = [n for ok, n in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
