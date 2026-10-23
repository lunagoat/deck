#!/bin/sh
# deck installer
#
#   curl -fsSL https://raw.githubusercontent.com/lunagoat/deck/main/install.sh | sh
#
# It clones deck into ~/.local/share/deck-app, builds a private virtualenv there (the only dependency
# is `textual`), and puts a `deck` launcher in ~/.local/bin. Nothing else on your system is touched.
# Run it again to update; run `sh install.sh --uninstall` to remove it. Read it first if you like: it is short.
#
# Environment overrides:
#   DECK_HOME     where to install        (default: ${XDG_DATA_HOME:-~/.local/share}/deck-app)
#   DECK_BIN_DIR  where the launcher goes (default: ~/.local/bin)
#   DECK_REF      branch or tag to install (default: main)
#   DECK_REPO     git URL to clone         (default: https://github.com/lunagoat/deck.git)
#   DECK_FORCE=1  overwrite an existing `deck` launcher that this installer didn't create
set -eu

REPO="${DECK_REPO:-https://github.com/lunagoat/deck.git}"
REF="${DECK_REF:-main}"
DEST="${DECK_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/deck-app}"
BIN="${DECK_BIN_DIR:-$HOME/.local/bin}"
LAUNCHER="$BIN/deck"
MARK="# deck launcher (written by install.sh)"

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

ours() { [ -f "$LAUNCHER" ] && grep -qF "$MARK" "$LAUNCHER" 2>/dev/null; }

if [ "${1:-}" = "--uninstall" ]; then
  if ours; then rm -f "$LAUNCHER"; say "removed $LAUNCHER"; fi
  if [ -d "$DEST/.git" ] && [ -f "$DEST/deck/__main__.py" ]; then rm -rf "$DEST"; say "removed $DEST"; fi
  say "kept your settings and history in ${XDG_CONFIG_HOME:-~/.config}/deck and ${XDG_STATE_HOME:-~/.local/state}/deck"
  exit 0
fi

command -v git >/dev/null 2>&1 || die "git is required"
command -v python3 >/dev/null 2>&1 || die "python3 (3.11 or newer) is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 is $(python3 -V 2>&1); deck needs 3.11 or newer"
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || die "python3's venv module is missing (on Debian/Ubuntu: sudo apt install python3-venv)"

[ "$(uname -s)" = "Linux" ] || say "note: deck has only been tested on Linux; some agents are fenced with bubblewrap, which is Linux-only."

if [ -e "$LAUNCHER" ] && ! ours && [ "${DECK_FORCE:-}" != "1" ]; then
  die "$LAUNCHER already exists and isn't deck's. Remove it, set DECK_BIN_DIR, or set DECK_FORCE=1."
fi

if [ -d "$DEST/.git" ]; then
  say "updating deck in $DEST ($REF)"
  git -C "$DEST" fetch -q --depth 1 origin "$REF"
  git -C "$DEST" reset -q --hard FETCH_HEAD
else
  [ ! -e "$DEST" ] || [ -z "$(ls -A "$DEST" 2>/dev/null)" ] || die "$DEST exists and isn't a deck install; set DECK_HOME"
  say "installing deck into $DEST ($REF)"
  git clone -q --depth 1 --branch "$REF" "$REPO" "$DEST"
fi

[ -d "$DEST/.venv" ] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$DEST/requirements.txt"

mkdir -p "$BIN"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
$MARK
D="$DEST"
PYTHONPATH="\$D\${PYTHONPATH:+:\$PYTHONPATH}" exec "\$D/.venv/bin/python" -m deck "\$@"
EOF
chmod +x "$LAUNCHER"

say ""
say "installed: $("$LAUNCHER" --version)"
say ""
say "coding agents found on this machine:"
"$LAUNCHER" --agents | sed 's/^/  /'
say ""
command -v bwrap >/dev/null 2>&1 || say "note: bubblewrap (bwrap) isn't installed. Agents that deck fences in (opencode, agy, copilot, other ACP agents) won't run without it."
case ":$PATH:" in *":$BIN:"*) ;; *) say "note: $BIN isn't on your PATH. Add it to your shell profile to run \`deck\` from anywhere." ;; esac
say "next: cd into a project and run \`deck\`. Update later with \`deck --update\` or by running this installer again."
