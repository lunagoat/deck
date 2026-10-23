#!/usr/bin/env python3
"""A stand-in for wl-paste / xclip that writes its payload in two delayed chunks.

Set FAKE_CLIP_TOOL=wl-paste or xclip (the tests use wrapper scripts). FAKE_CLIP_MODE picks the
payload: png-slow (default), jpeg, text, oversize, fail. A single read() of this output returns only
the first chunk, so it is exactly the partial-data case deck's clipboard reader has to handle.
"""
import base64
import os
import sys
import time

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xd9"

TOOL = os.environ.get("FAKE_CLIP_TOOL") or os.path.basename(sys.argv[0])
MODE = os.environ.get("FAKE_CLIP_MODE", "png-slow")
LOG = os.environ.get("FAKE_CLIP_LOG")
ARGS = sys.argv[1:]


def log():
    if LOG:
        with open(LOG, "a") as f:
            f.write(f"{TOOL} {MODE} {' '.join(ARGS)}\n")


def emit(data: bytes):
    """Two chunks with a pause: a single pipe read() only sees the first one."""
    mid = max(1, len(data) // 2)
    sys.stdout.buffer.write(data[:mid])
    sys.stdout.buffer.flush()
    time.sleep(0.3)
    sys.stdout.buffer.write(data[mid:])
    sys.stdout.buffer.flush()


def type_arg():
    if TOOL == "wl-paste":
        return ARGS[ARGS.index("--type") + 1] if "--type" in ARGS else None
    return ARGS[ARGS.index("-t") + 1] if "-t" in ARGS and "TARGETS" not in ARGS else None


def targets():
    if TOOL == "wl-paste":
        return "--list-types" in ARGS
    return "-t" in ARGS and "TARGETS" in ARGS


def text_request():
    if TOOL == "wl-paste":
        return not targets() and type_arg() is None
    return "-t" not in ARGS


def main():
    log()
    if MODE == "fail":
        sys.stderr.write("cannot open display\n")
        sys.exit(2)
    if targets():
        types = {"png-slow": ["text/plain", "image/png"], "jpeg": ["image/jpeg"],
                 "text": ["text/plain"], "oversize": ["image/png"]}[MODE]
        sys.stdout.write("\n".join(types) + "\n")
        return
    if MODE == "oversize":
        emit(PNG + b"\x00" * 64)
        return
    if text_request():
        emit(b"hello from the clipboard")
        return
    mime = type_arg() or ("image/jpeg" if MODE == "jpeg" else "image/png")
    emit(JPEG if mime == "image/jpeg" else PNG)


main()
