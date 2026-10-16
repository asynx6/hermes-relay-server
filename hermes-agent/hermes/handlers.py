"""Hook into the AI agent logic.

Replace the body of on_message with whatever the agent should do when a
message arrives (call an LLM, write to a queue, auto-reply, etc.).

IMPORTANT: message content is untrusted input. If it is passed to an LLM or
system commands, treat it as data (not instructions) to prevent prompt
injection. The safe_text() function is used for display so that ANSI escape
sequences from the sender cannot forge log output.
"""
from .safe_text import safe_text


def on_message(msg: dict) -> None:
    """Called for every incoming message. Default: print to stdout."""
    if msg["msg_type"] == "file" and msg.get("file"):
        f = msg["file"]
        body = f"[file {safe_text(f['original_name'], 120)} {f['size_bytes']} byte]"
        if msg.get("content"):
            body = f"{safe_text(msg['content'])} {body}"
    else:
        body = safe_text(msg.get("content"))
    where = f"grup {safe_text(msg['group'], 64)}" if msg["group"] else "direct"
    print(
        f"[{msg['created_at']}] {safe_text(msg['from'], 64)} ({where}): {body}",
        flush=True,
    )
