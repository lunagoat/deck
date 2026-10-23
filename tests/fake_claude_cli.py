#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, for native image-prompt regression tests.

Logs its argv and the stream-json messages it reads on stdin to $FAKE_CLAUDE_LOG, then prints the
stream-json events deck parses (including a thinking block, to exercise thought events). No network.
"""
import json
import os
import sys

LOG = os.environ.get("FAKE_CLAUDE_LOG")


def log(kind, payload):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    log("argv", sys.argv[1:])
    messages = []
    if not os.environ.get("FAKE_CLAUDE_IGNORE_STDIN"):
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except ValueError:
                    messages.append({"unparsed": line})
    log("stdin", messages)
    emit({"type": "system", "subtype": "init", "session_id": "fake-session"})
    emit({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "I should look at the attached image."},
        {"type": "text", "text": "I received your picture."}]}})
    emit({"type": "result", "session_id": "fake-session", "is_error": False,
          "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}})


main()
