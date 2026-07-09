#!/usr/bin/env python3
"""
publish_session.py — Render a Claude Code session transcript to a self-contained HTML page.

Claude Code stores each session as a JSONL file under:
    ~/.claude/projects/<project-slug>/<session-id>.jsonl

This tool parses one of those files and writes a single, styled, self-contained
HTML page (no external requests) suitable for serving behind Caddy basic auth.

Usage:
    # Render a specific transcript file
    python3 publish_session.py path/to/session.jsonl -o /var/www/sessions/mysession.html

    # Pick the most recent session for the current project (cwd) interactively
    python3 publish_session.py --latest -o /var/www/sessions/latest.html

    # List available sessions for the current project
    python3 publish_session.py --list

    # Give the page a title
    python3 publish_session.py session.jsonl -o out.html --title "Auth refactor walkthrough"

Options:
    -o, --output   Output HTML path (default: <session-id>.html in cwd)
    --title        Page title / heading (default: derived from summary or session id)
    --latest       Use the most recently modified session in ~/.claude/projects
    --project DIR  Limit --latest/--list to the project whose cwd is DIR (default: current dir)
    --list         List sessions and exit
    --no-thinking  Omit assistant "thinking" blocks from the output
    --standalone   (default) inline everything; there is no non-standalone mode, kept for clarity
"""

import argparse
import datetime as _dt
import glob
import html
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import redact_entries, summarize  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"


# --------------------------------------------------------------------------- #
# Discovery helpers
# --------------------------------------------------------------------------- #
def _project_slug(cwd: str) -> str:
    """Claude Code slugifies the project cwd by replacing non-alnum with '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def find_sessions(project_dir: str | None):
    """Return list of (path, mtime) for session jsonl files, newest first."""
    if project_dir:
        slug = _project_slug(os.path.abspath(project_dir))
        search = PROJECTS_DIR / slug / "*.jsonl"
    else:
        search = PROJECTS_DIR / "*" / "*.jsonl"
    files = [Path(p) for p in glob.glob(str(search))]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def load_jsonl(path: Path):
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # Tolerate the occasional truncated/partial line.
                continue
    return entries


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        # Claude Code uses ISO 8601 with a trailing Z.
        dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts


def _as_text(content) -> str:
    """A tool_result's content may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif "text" in b:
                    parts.append(b["text"])
                else:
                    parts.append(json.dumps(b, indent=2))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content)


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
_REDACT_RX = re.compile(r"\[REDACTED:([a-z0-9_]+)\]")


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def hl(escaped: str) -> str:
    """Make [REDACTED:...] markers visually obvious. Input must already be escaped."""
    return _REDACT_RX.sub(
        lambda m: f'<mark class="redacted">REDACTED {m.group(1)}</mark>', escaped
    )


def render_tool_use(block) -> str:
    name = esc(block.get("name", "tool"))
    tinput = block.get("input", {})
    try:
        pretty = json.dumps(tinput, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        pretty = str(tinput)
    # Show a compact one-liner hint in the summary where obvious.
    hint = ""
    for key in ("command", "file_path", "path", "pattern", "query", "url", "description"):
        if isinstance(tinput, dict) and key in tinput:
            hint = f" · {hl(esc(str(tinput[key])[:80]))}"
            break
    return (
        f'<details class="tool">'
        f'<summary><span class="badge tool-badge">tool</span> '
        f'<span class="tool-name">{name}</span>{hint}</summary>'
        f'<pre class="code">{hl(esc(pretty))}</pre>'
        f"</details>"
    )


def render_tool_result(block) -> str:
    text = _as_text(block.get("content", ""))
    is_error = block.get("is_error", False)
    cls = "tool-result error" if is_error else "tool-result"
    label = "error" if is_error else "result"
    truncated = ""
    if len(text) > 20000:
        text = text[:20000]
        truncated = '<div class="truncated">… output truncated …</div>'
    return (
        f'<details class="tool {cls}">'
        f'<summary><span class="badge result-badge">{label}</span> output</summary>'
        f'<pre class="code">{hl(esc(text))}</pre>{truncated}'
        f"</details>"
    )


def render_message(entry, include_thinking=True) -> str:
    msg = entry.get("message") or {}
    role = msg.get("role") or entry.get("type", "")
    content = msg.get("content", "")
    ts = _fmt_ts(entry.get("timestamp"))

    # Normalize content into a list of blocks.
    blocks = []
    if isinstance(content, str):
        if content.strip():
            blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content

    # Determine whether this is a pure tool-result carrier (user role with only tool_result).
    body_parts = []
    has_visible = False
    for b in blocks:
        if not isinstance(b, dict):
            body_parts.append(f'<div class="text">{esc(b)}</div>')
            has_visible = True
            continue
        btype = b.get("type")
        if btype == "text":
            txt = b.get("text", "")
            if txt.strip():
                body_parts.append(f'<div class="text">{md_lite(txt)}</div>')
                has_visible = True
        elif btype == "thinking":
            if include_thinking:
                think = b.get("thinking", "") or b.get("text", "")
                if think.strip():
                    body_parts.append(
                        f'<details class="thinking"><summary>'
                        f'<span class="badge think-badge">thinking</span></summary>'
                        f'<div class="text muted">{md_lite(think)}</div></details>'
                    )
                    has_visible = True
        elif btype == "tool_use":
            body_parts.append(render_tool_use(b))
            has_visible = True
        elif btype == "tool_result":
            body_parts.append(render_tool_result(b))
            has_visible = True
        elif btype == "image":
            body_parts.append('<div class="text muted">[image]</div>')
            has_visible = True

    if not has_visible:
        return ""

    role_label = {"user": "You", "assistant": "Claude"}.get(role, esc(role))
    role_cls = "user" if role == "user" else "assistant"
    ts_html = f'<span class="ts">{esc(ts)}</span>' if ts else ""
    return (
        f'<div class="turn {role_cls}">'
        f'<div class="turn-head"><span class="role">{esc(role_label)}</span>{ts_html}</div>'
        f'<div class="turn-body">{"".join(body_parts)}</div>'
        f"</div>"
    )


def md_lite(text: str) -> str:
    """Very small, safe Markdown-ish rendering: escape first, then add code + bold + line breaks."""
    # Fenced code blocks
    parts = re.split(r"```", text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # inside a code fence
            # strip optional language on first line
            lines = part.split("\n")
            if lines and re.match(r"^[a-zA-Z0-9_+-]*$", lines[0].strip()):
                lines = lines[1:]
            out.append(f'<pre class="code">{hl(esc(chr(10).join(lines)))}</pre>')
        else:
            e = esc(part)
            e = re.sub(r"`([^`]+)`", r'<code>\1</code>', e)
            e = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", e)
            e = e.replace("\n", "<br>")
            out.append(hl(e))
    return "".join(out)


CSS = """
:root{--bg:#faf9f7;--fg:#1f1f1d;--muted:#6b6a66;--line:#e6e3dd;--user:#f0ede6;
--assistant:#ffffff;--accent:#c15f3c;--code-bg:#1e1e1c;--code-fg:#e8e6df;--think:#8a6d3b;}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a18;--fg:#e8e6df;--muted:#9a978f;
--line:#33322e;--user:#242320;--assistant:#1f1e1c;--code-bg:#111110;--code-fg:#d8d6cf;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:880px;margin:0 auto;padding:32px 20px 80px;}
header.page{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px;}
header.page h1{font-size:22px;margin:0 0 6px;}
header.page .meta{color:var(--muted);font-size:13px;}
.turn{border:1px solid var(--line);border-radius:10px;margin:14px 0;overflow:hidden;}
.turn.user{background:var(--user);}
.turn.assistant{background:var(--assistant);}
.turn-head{display:flex;justify-content:space-between;align-items:center;
padding:8px 14px;border-bottom:1px solid var(--line);font-size:13px;}
.role{font-weight:650;}
.turn.user .role{color:var(--accent);}
.ts{color:var(--muted);font-size:12px;}
.turn-body{padding:12px 14px;}
.text{margin:4px 0;white-space:normal;overflow-wrap:anywhere;}
.muted{color:var(--muted);}
code{background:rgba(127,127,127,.15);padding:1px 5px;border-radius:4px;
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
pre.code{background:var(--code-bg);color:var(--code-fg);padding:12px 14px;border-radius:8px;
overflow-x:auto;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
white-space:pre;margin:8px 0;}
details.tool,details.thinking{margin:8px 0;border:1px solid var(--line);border-radius:8px;
background:rgba(127,127,127,.04);}
details.tool>summary,details.thinking>summary{cursor:pointer;padding:8px 12px;font-size:13px;
list-style:none;user-select:none;}
details>summary::-webkit-details-marker{display:none}
details.tool>summary:before,details.thinking>summary:before{content:"▸ ";color:var(--muted);}
details[open]>summary:before{content:"▾ ";}
details.tool pre.code,details.thinking .text{margin:0 12px 12px;}
.badge{display:inline-block;font-size:11px;font-weight:650;padding:1px 7px;border-radius:20px;
text-transform:uppercase;letter-spacing:.03em;vertical-align:middle;}
.tool-badge{background:#2f6f9f;color:#fff;}
.result-badge{background:#4a7c59;color:#fff;}
.think-badge{background:var(--think);color:#fff;}
.tool-name{font-weight:600;}
.tool-result.error{border-color:#b3453a;}
.tool-result.error .result-badge{background:#b3453a;}
.truncated{color:var(--muted);font-size:12px;padding:0 12px 10px;font-style:italic;}
.banner{border-radius:8px;padding:9px 12px;font-size:13px;margin:0 0 18px;}
.banner.ok{background:rgba(74,124,89,.12);border:1px solid rgba(74,124,89,.45);}
.banner.warn{background:rgba(179,69,58,.12);border:1px solid rgba(179,69,58,.5);}
mark.redacted{background:rgba(179,69,58,.18);color:#b3453a;border:1px solid rgba(179,69,58,.4);
border-radius:4px;padding:0 4px;font:12px ui-monospace,Menlo,Consolas,monospace;
font-weight:600;white-space:nowrap;}
footer.page{margin-top:40px;color:var(--muted);font-size:12px;text-align:center;
border-top:1px solid var(--line);padding-top:16px;}
"""


def _msg_text(entry) -> str:
    """Extract plain prose from an entry's message, ignoring tool_result carriers."""
    msg = entry.get("message") or {}
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts).strip()
    return ""


def _usable(text: str) -> bool:
    """Is this prose worth using as a description?"""
    if not text:
        return False
    # Skip system-injected blocks and bare slash-commands.
    if text.startswith("<") or text.startswith("/"):
        return False
    return True


def session_kind(entries) -> dict:
    """Detect whether a transcript is a subagent (sidechain) session, and its type.

    Claude Code marks subagent turns with isSidechain=true. A transcript whose only
    user turns are sidechain turns IS a subagent run, and has no top-level prompt --
    which is exactly why these used to render as "(no description)".
    """
    saw_sidechain = False
    saw_mainline_user = False
    agent_type = ""

    for e in entries:
        if e.get("isSidechain"):
            saw_sidechain = True
        if e.get("type") == "user" and not e.get("isMeta") and not e.get("isSidechain"):
            if _usable(_msg_text(e)):
                saw_mainline_user = True
        # Agent type may appear on the entry, or on the spawning Task tool_use.
        agent_type = agent_type or e.get("agentType") or e.get("subagent_type") or ""
        msg = e.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") == "Task":
                    agent_type = agent_type or (b.get("input") or {}).get("subagent_type", "")

    return {
        "is_subagent": bool(saw_sidechain and not saw_mainline_user),
        "agent_type": agent_type or "",
    }


def own_session_id(entries) -> str:
    """The dominant sessionId in a transcript.

    A file should describe exactly one session, but resumed or compacted
    transcripts can carry stray entries. The modal sessionId is the true owner.
    """
    counts = {}
    for e in entries:
        sid = e.get("sessionId")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def foreign_session_ids(entries) -> set:
    """sessionIds present in the file that are not its owner."""
    own = own_session_id(entries)
    return {e.get("sessionId") for e in entries
            if e.get("sessionId") and e.get("sessionId") != own}


def infer_description(entries, max_len=140) -> str:
    return _infer_description(entries, max_len)[0]


def _infer_description(entries, max_len=140):
    """Best-effort one-line description. Returns (description, source_rung).

    Preference order (each falls through to the next):
      1. An explicit `summary` entry (Claude Code writes these).
      2. The first real top-level user prompt.
      3. A spawning `Task` tool call's description/prompt  -- subagent sessions.
      4. The first sidechain user turn -- i.e. the agent's own instructions.
      5. The first assistant prose.
      6. Empty string (caller supplies a fallback label).

    Steps 3-5 exist because subagent transcripts have no top-level prompt and
    would otherwise be indexed as "(no description)".

    Two guards stop a neighbouring conversation's text from bleeding in:
      * a `summary` is trusted only if it has no leafUuid, or its leafUuid names
        an entry this transcript actually contains;
      * prompts are read only from entries belonging to the file's own session.
    """
    own_uuids = {e["uuid"] for e in entries if e.get("uuid")}
    own_sid = own_session_id(entries)

    def mine(e):
        sid = e.get("sessionId")
        return (not sid) or (not own_sid) or sid == own_sid

    def clip(t):
        return _clip(re.sub(r"\s+", " ", t), max_len)

    # 1. explicit summary -- only if it describes THIS transcript
    for e in entries:
        if e.get("type") != "summary" or not e.get("summary"):
            continue
        leaf = e.get("leafUuid")
        if leaf and own_uuids and leaf not in own_uuids:
            continue  # a summary of some other conversation
        return _clip(str(e["summary"]), max_len), "summary"

    # 2. first mainline user prompt
    for e in entries:
        if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
            continue
        if not mine(e):
            continue
        text = _msg_text(e)
        if _usable(text):
            return clip(text), "user-prompt"

    # 3. the Task tool call that spawned an agent
    for e in entries:
        if not mine(e):
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Task":
                inp = b.get("input") or {}
                cand = inp.get("description") or inp.get("prompt") or ""
                if cand.strip():
                    return clip(cand), "task-call"

    # 4. first sidechain user turn = the agent's own instructions
    for e in entries:
        if e.get("type") == "user" and e.get("isSidechain") and not e.get("isMeta"):
            text = _msg_text(e)
            if _usable(text):
                return clip(text), "sidechain-prompt"

    # 5. first assistant prose
    for e in entries:
        if e.get("type") == "assistant" and mine(e):
            text = _msg_text(e)
            if _usable(text):
                return clip(text), "assistant-prose"

    return "", "none"


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def extract_meta(entries, path=None) -> dict:
    """Pull index-worthy metadata out of a parsed transcript."""
    session_id = cwd = version = None
    first_ts = last_ts = None
    turns = 0
    tool_calls = 0

    for e in entries:
        session_id = session_id or e.get("sessionId")
        cwd = cwd or e.get("cwd")
        version = version or e.get("version")
        ts = e.get("timestamp")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        if e.get("type") in ("user", "assistant") and not e.get("isMeta"):
            turns += 1
        msg = e.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_calls += 1

    kind = session_kind(entries)
    # Trust the transcript's own sessionId over the filename.
    sid = own_session_id(entries) or session_id or (path.stem if path else "unknown")
    desc, desc_src = _infer_description(entries)
    if not desc:
        # Never emit a bare "(no description)" -- say something useful.
        label = "Subagent run" if kind["is_subagent"] else "Session"
        desc = f"{label} {sid[:8]} — {turns} turns, no prompt captured"

    return {
        "session_id": sid,
        "description_source": desc_src,
        "foreign_sessions": sorted(foreign_session_ids(entries)),
        "cwd": cwd or "",
        "project": os.path.basename(cwd) if cwd else "",
        "version": version or "",
        "first_ts": first_ts or "",
        "last_ts": last_ts or "",
        "turns": turns,
        "tool_calls": tool_calls,
        "description": desc,
        "is_subagent": kind["is_subagent"],
        "agent_type": kind["agent_type"],
        "source": path.name if path else "",
    }


def build_html(entries, title, source_name, include_thinking=True,
               redaction_note="", redacted=True) -> str:
    # Pull metadata + a summary if present.
    summary = None
    session_id = None
    cwd = None
    version = None
    first_ts = None
    last_ts = None
    turns_html = []
    turn_count = 0

    for e in entries:
        etype = e.get("type")
        if etype == "summary" and not summary:
            summary = e.get("summary")
            continue
        session_id = session_id or e.get("sessionId")
        cwd = cwd or e.get("cwd")
        version = version or e.get("version")
        ts = e.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if etype in ("user", "assistant") and not e.get("isMeta"):
            rendered = render_message(e, include_thinking=include_thinking)
            if rendered:
                turns_html.append(rendered)
                turn_count += 1

    page_title = title or summary or (f"Claude Code session {session_id[:8]}" if session_id else "Claude Code session")

    meta_bits = []
    if cwd:
        meta_bits.append(f"<span>project: <code>{esc(cwd)}</code></span>")
    if session_id:
        meta_bits.append(f"<span>session: <code>{esc(session_id[:8])}</code></span>")
    if first_ts:
        meta_bits.append(f"<span>{esc(_fmt_ts(first_ts))} → {esc(_fmt_ts(last_ts))}</span>")
    meta_bits.append(f"<span>{turn_count} turns</span>")
    if version:
        meta_bits.append(f"<span>cc v{esc(version)}</span>")
    generated = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    if not redacted:
        banner = ('<div class="banner warn">⚠ Published <strong>without redaction</strong>. '
                  'This page may contain credentials.</div>')
    elif redaction_note:
        banner = f'<div class="banner ok">🛡 {esc(redaction_note)}</div>'
    else:
        banner = '<div class="banner ok">🛡 Redaction ran; no known secret patterns found.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{esc(page_title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="page">
<h1>{esc(page_title)}</h1>
<div class="meta">{" · ".join(meta_bits)}</div>
</header>
{banner}
<main>
{"".join(turns_html) if turns_html else '<p class="muted">No visible turns in this transcript.</p>'}
</main>
<footer class="page">
Rendered from <code>{esc(source_name)}</code> · generated {esc(generated)} · self-contained, no external requests
</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a Claude Code session transcript to HTML.")
    ap.add_argument("transcript", nargs="?", help="Path to a session .jsonl file")
    ap.add_argument("-o", "--output", help="Output HTML path")
    ap.add_argument("--title", help="Page title/heading")
    ap.add_argument("--latest", action="store_true", help="Use the most recent session")
    ap.add_argument("--project", help="Limit --latest/--list to the project rooted at this cwd")
    ap.add_argument("--list", action="store_true", help="List available sessions and exit")
    ap.add_argument("--no-thinking", action="store_true", help="Omit assistant thinking blocks")
    ap.add_argument("--no-redact", action="store_true",
                    help="DANGEROUS: publish raw, without scrubbing credentials")
    args = ap.parse_args(argv)

    if args.list:
        sessions = find_sessions(args.project)
        if not sessions:
            print("No sessions found under", PROJECTS_DIR, file=sys.stderr)
            return 1
        for p in sessions[:50]:
            mtime = _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{mtime}  {p}")
        return 0

    if args.latest:
        sessions = find_sessions(args.project)
        if not sessions:
            print("No sessions found under", PROJECTS_DIR, file=sys.stderr)
            return 1
        path = sessions[0]
    elif args.transcript:
        path = Path(args.transcript)
    else:
        ap.error("Provide a transcript path, or use --latest or --list.")
        return 2

    if not path.exists():
        print(f"Not found: {path}", file=sys.stderr)
        return 1

    entries = load_jsonl(path)

    note = ""
    if args.no_redact:
        print("WARNING: --no-redact — publishing raw transcript, credentials included.",
              file=sys.stderr)
    else:
        entries, counter = redact_entries(entries)
        note = summarize(counter)
        print(f"redaction: {note or 'no known secret patterns found'}")

    html_doc = build_html(
        entries, args.title, path.name,
        include_thinking=not args.no_thinking,
        redaction_note=note, redacted=not args.no_redact,
    )

    out = args.output
    if not out:
        stem = path.stem
        out = f"{stem}.html"
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    print(f"Wrote {out_path}  ({len(html_doc):,} bytes, {len(entries)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
