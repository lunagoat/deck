#!/usr/bin/env python3
"""An ACP agent for image tests: advertises image prompts and emits a thought chunk.

Logs every request (like tests/fake_acp_agent.py) to $FAKE_ACP_LOG, so a test can inspect the
`session/prompt` payload deck sent and decode the attached images. Never calls a real agent.
"""
import json
import os
import sys

LOG = os.environ.get("FAKE_ACP_LOG")


def log(kind, payload):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")


def send(obj):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
    sys.stdout.flush()


def update(sid, **u):
    send({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def main():
    while (m := read()) is not None:
        method, params, mid = m.get("method"), m.get("params") or {}, m.get("id")
        if method:
            log(method, params)
        if method == "initialize":
            send({"id": mid, "result": {
                "protocolVersion": 1, "authMethods": [], "agentInfo": {"name": "fake-imaging"},
                "agentCapabilities": {"loadSession": False, "promptCapabilities": {"image": True,
                                                                                  "audio": False,
                                                                                  "embeddedContext": True}}}})
        elif method == "session/new":
            send({"id": mid, "result": {"sessionId": "img-1", "configOptions": []}})
        elif method == "session/prompt":
            sid = params["sessionId"]
            update(sid, sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "pondering "})
            update(sid, sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "the image\n"})
            update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "I see it.\n"})
            send({"id": mid, "result": {"stopReason": "end_turn", "usage": {"totalTokens": 21}}})
        elif mid is not None:
            send({"id": mid, "error": {"code": -32601, "message": "unknown method"}})


main()
