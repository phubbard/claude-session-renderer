#!/usr/bin/env python3
"""
chat_export.py — Convert a claude.ai data export into Claude Code-style
transcripts that publish_session.py / cc_collect.py can render.

claude.ai chat conversations live only on Anthropic's servers; the account
data export (Settings → Privacy → Export data) is the one way to get them.
It arrives as a zip (data-...-batch-0000.zip) holding conversations.json.

Each conversation becomes one JSONL file of synthesized entries:
    {"type": "summary", "summary": <chat title>}
    {"type": "user"/"assistant", "uuid": ..., "sessionId": <conversation uuid>,
     "timestamp": ..., "cwd": "claude.ai", "message": {"role": ..., "content": [blocks]}}

Blocks are slimmed to the shapes the renderer knows (text, thinking,
tool_use, tool_result); attachment text rides along as a collapsible result
block so redaction covers it too.

Standalone use:
    python3 chat_export.py                 # newest export in ~/Downloads → ./chat-transcripts/
    python3 chat_export.py export.zip -o outdir

cc_collect.py uses this via the reserved host target "chats".
"""

import argparse
import json
import sys
import zipfile
from pathlib import Path


def _has_conversations(z: zipfile.ZipFile) -> str:
    return next((m for m in z.namelist() if m.endswith("conversations.json")), "")


def find_export(dirs):
    """Newest claude.ai export in `dirs`: a data-*.zip, an unzipped export
    folder, or a bare conversations.json. Returns a Path or None."""
    best = None
    for d in dirs:
        d = Path(d).expanduser()
        if not d.is_dir():
            continue
        for p in d.iterdir():
            cand = None
            if p.is_dir() and (p / "conversations.json").is_file():
                cand = p / "conversations.json"
            elif p.suffix == ".zip" and p.name.startswith("data-"):
                try:
                    with zipfile.ZipFile(p) as z:
                        if _has_conversations(z):
                            cand = p
                except (zipfile.BadZipFile, OSError):
                    continue
            elif p.name == "conversations.json":
                cand = p
            if cand and (best is None or cand.stat().st_mtime > best.stat().st_mtime):
                best = cand
    return best


def load_conversations(src: Path):
    if src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            member = _has_conversations(z)
            return json.loads(z.read(member)) if member else []
    return json.loads(src.read_text(encoding="utf-8"))


def _result_text(content) -> str:
    """Flatten a tool_result's content (string or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, dict):
                parts.append(json.dumps(b, ensure_ascii=False)[:2000])
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content) if content else ""


def _slim_block(b: dict):
    """Reduce an export content block to the shape the renderer expects.
    Drops signatures, display metadata, and block types with nothing to show
    (flag, token_budget, ...)."""
    t = b.get("type")
    if t == "text":
        txt = b.get("text", "")
        return {"type": "text", "text": txt} if txt.strip() else None
    if t == "thinking":
        th = b.get("thinking", "")
        return {"type": "thinking", "thinking": th} if th.strip() else None
    if t == "tool_use":
        return {"type": "tool_use", "name": b.get("name", "tool"),
                "input": b.get("input") or {}}
    if t == "tool_result":
        txt = _result_text(b.get("content"))
        if not txt.strip():
            return None
        return {"type": "tool_result", "content": txt,
                "is_error": bool(b.get("is_error"))}
    return None


def conversation_to_entries(conv: dict):
    """One export conversation → a list of Claude Code-style entries.
    Returns [] for conversations with nothing visible."""
    title = (conv.get("name") or "").strip() or (conv.get("summary") or "").strip()
    entries = []
    if title:
        entries.append({"type": "summary", "summary": title})

    for m in conv.get("chat_messages") or []:
        role = "user" if m.get("sender") == "human" else "assistant"
        blocks = []
        for b in m.get("content") or []:
            sb = _slim_block(b) if isinstance(b, dict) else None
            if sb:
                blocks.append(sb)
        # Old-style messages carry only a flat text field.
        if not blocks and (m.get("text") or "").strip():
            blocks.append({"type": "text", "text": m["text"]})
        for a in m.get("attachments") or []:
            name = a.get("file_name") or "attachment"
            blocks.append({"type": "text", "text": f"[attachment: {name}]"})
            extracted = (a.get("extracted_content") or "")
            if extracted.strip():
                blocks.append({"type": "tool_result", "content": extracted})
        for f in m.get("files") or []:
            blocks.append({"type": "text", "text": f"[file: {f.get('file_name') or 'file'}]"})
        if not blocks:
            continue
        entries.append({
            "type": role,
            "uuid": m.get("uuid"),
            "sessionId": conv.get("uuid"),
            "timestamp": m.get("created_at"),
            "cwd": "claude.ai",
            "message": {"role": role, "content": blocks},
        })

    # A summary with no turns is an empty conversation.
    return entries if len(entries) > (1 if title else 0) else []


def convert_export(src: Path, outdir: Path):
    """Write chat-<uuid>.jsonl per conversation into outdir, mirroring the
    export: stale chat-*.jsonl (deleted conversations) are removed.
    Returns (written, skipped_empty)."""
    outdir.mkdir(parents=True, exist_ok=True)
    convs = load_conversations(src)
    written, skipped = 0, 0
    keep = set()
    for conv in convs:
        uuid = conv.get("uuid")
        if not uuid:
            skipped += 1
            continue
        entries = conversation_to_entries(conv)
        if not entries:
            skipped += 1
            continue
        name = f"chat-{uuid}.jsonl"
        keep.add(name)
        out = outdir / name
        out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                       encoding="utf-8")
        written += 1
    for p in outdir.glob("chat-*.jsonl"):
        if p.name not in keep:
            p.unlink()
    return written, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert a claude.ai data export "
                                 "to renderable transcripts.")
    ap.add_argument("export", nargs="?",
                    help="Export zip, unzipped export dir, or conversations.json "
                         "(default: newest export found in ~/Downloads)")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("chat-transcripts"))
    args = ap.parse_args(argv)

    if args.export:
        src = Path(args.export).expanduser()
        if src.is_dir():
            src = src / "conversations.json"
        if not src.exists():
            sys.exit(f"error: {src} not found")
    else:
        src = find_export([Path.home() / "Downloads"])
        if not src:
            sys.exit("error: no claude.ai export found in ~/Downloads "
                     "(Settings → Privacy → Export data)")

    written, skipped = convert_export(src, args.outdir)
    print(f"{src} → {args.outdir}/: {written} conversations"
          + (f" ({skipped} empty, skipped)" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
