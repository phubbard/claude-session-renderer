#!/usr/bin/env python3
"""
cc_collect.py — Aggregate Claude Code transcripts across machines into a
password-protected, date-ordered browsable index, then deploy it.

Pipeline:
    1. fetch    For each configured host: probe platform, pull ~/.claude/projects/**/*.jsonl
    2. render   Render every session to a self-contained HTML page (via publish_session.py)
    3. index    Build index.html ordered by date, with inferred descriptions
                and machine/platform annotations
    4. deploy   scp the whole tree to the configured target

Requires: ssh + scp on this machine, and working key-based auth to each host.
Run it from wherever your SSH agent lives (your laptop), NOT from a sandbox.

Quick start:
    python3 cc_collect.py --dry-run           # show what would happen
    python3 cc_collect.py --no-deploy         # build locally into ./_site
    python3 cc_collect.py                     # full run + scp to the deploy target

Config:
    Everything lives in hosts.conf (see --hosts-file). It is gitignored, so real
    hostnames stay out of the repo.

        # name        ssh-target              (use "local" for this machine)
        web           you@web.example.com
        axiom         you@axiom.example.com
        laptop        local

        # where to publish the built site
        deploy        web.example.com:sessions

    Blank lines and #-comments ignored. Override hosts with repeated
    --host name=target, and the deploy target with --deploy-to HOST:PATH.
    There is no built-in deploy default: if none is configured, the run stops
    rather than scp'ing somewhere you didn't intend.
"""

import argparse
import concurrent.futures as cf
import datetime as _dt
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the renderer/parsers from publish_session.py (must sit alongside this file).
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from publish_session import load_jsonl, build_html, extract_meta, esc, _fmt_ts
    from redact import redact_entries, summarize
except ImportError as _e:
    sys.exit(f"error: publish_session.py and redact.py must sit beside cc_collect.py ({_e})")

DEFAULT_HOSTS = [("web", "web.example.com"), ("axiom", "axiom.example.com")]
REMOTE_GLOB = "~/.claude/projects"
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def parse_hosts_file(path: Path):
    """Parse hosts.conf. Returns (hosts, deploy_target).

    Two line forms:
        <name> <ssh-target>       a machine to collect transcripts from
        deploy <host[:path]>      where to scp the built site  (at most one)

    "deploy" is a reserved first word, so a machine cannot be named "deploy".
    Anything after # is a comment.

    The deploy target lives here, not in this file, because hosts.conf is
    gitignored -- your real hostname never reaches the public repo.
    """
    hosts = []
    deploy_target = ""
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].lower() == "deploy":
            if len(parts) < 2:
                sys.exit(f"{path}:{lineno}: 'deploy' needs a target, e.g.\n"
                         f"    deploy web.example.com:sessions")
            if deploy_target:
                sys.exit(f"{path}:{lineno}: more than one 'deploy' line")
            deploy_target = parts[1]
            continue
        if len(parts) == 1:
            hosts.append((parts[0], parts[0]))
        else:
            hosts.append((parts[0], parts[1]))
    return hosts, deploy_target


# --------------------------------------------------------------------------- #
# Step 1: fetch
# --------------------------------------------------------------------------- #
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def clean_dir(d: Path):
    """Empty a build dir, tolerating filesystems that refuse unlink (FUSE mounts, etc.).

    Falls back to truncating/overwriting in place rather than dying, so a rebuild
    on an odd filesystem still produces a correct tree.
    """
    if not d.exists():
        return
    try:
        shutil.rmtree(d)
        return
    except (PermissionError, OSError) as exc:
        print(f"  note: could not remove {d} ({exc.__class__.__name__}); "
              f"overwriting in place", file=sys.stderr)
    # Best-effort: drop stale .html so removed sessions don't linger.
    for p in sorted(d.rglob("*.html"), reverse=True):
        try:
            p.unlink()
        except OSError:
            pass


def probe_platform(target: str) -> dict:
    """Return {'os':..., 'kernel':..., 'arch':..., 'pretty':...} for a host."""
    if target == "local":
        uname = run(["uname", "-s", "-r", "-m"]).stdout.strip()
        pretty = ""
        if sys.platform == "darwin":
            ver = run(["sw_vers", "-productVersion"]).stdout.strip()
            name = run(["sw_vers", "-productName"]).stdout.strip()
            pretty = f"{name} {ver}".strip()
        elif Path("/etc/os-release").exists():
            pretty = _os_release(Path("/etc/os-release").read_text())
    else:
        script = (
            "uname -s -r -m; "
            "if [ -r /etc/os-release ]; then cat /etc/os-release; "
            "elif command -v sw_vers >/dev/null; then "
            'echo "PRETTY_NAME=\\"$(sw_vers -productName) $(sw_vers -productVersion)\\""; fi'
        )
        r = run(["ssh", *SSH_OPTS, target, script])
        if r.returncode != 0:
            return {"os": "?", "kernel": "", "arch": "", "pretty": "unreachable",
                    "error": r.stderr.strip()[:200]}
        lines = r.stdout.splitlines()
        uname = lines[0] if lines else ""
        pretty = _os_release("\n".join(lines[1:]))

    bits = uname.split()
    osname = bits[0] if bits else "?"
    kernel = bits[1] if len(bits) > 1 else ""
    arch = bits[2] if len(bits) > 2 else ""
    if not pretty:
        pretty = {"Darwin": "macOS", "Linux": "Linux"}.get(osname, osname)
    return {"os": osname, "kernel": kernel, "arch": arch, "pretty": pretty}


def _os_release(text: str) -> str:
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.M)
    return m.group(1).strip() if m else ""


def list_transcripts(target: str):
    """Return remote/local paths of all session .jsonl files."""
    if target == "local":
        base = Path.home() / ".claude" / "projects"
        if not base.exists():
            return []
        return [str(p) for p in base.rglob("*.jsonl")]
    cmd = f"find {REMOTE_GLOB} -name '*.jsonl' -type f 2>/dev/null || true"
    r = run(["ssh", *SSH_OPTS, target, cmd])
    if r.returncode != 0:
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def fetch_host(name: str, target: str, staging: Path, verbose=True):
    """Copy all transcripts from one host into staging/<name>/. Returns (platform, [local paths])."""
    dest = staging / name
    dest.mkdir(parents=True, exist_ok=True)

    plat = probe_platform(target)
    if plat.get("pretty") == "unreachable":
        print(f"  [{name}] UNREACHABLE: {plat.get('error','')}", file=sys.stderr)
        return plat, []

    remote_files = list_transcripts(target)
    if not remote_files:
        print(f"  [{name}] no transcripts found under {REMOTE_GLOB}")
        return plat, []

    local_files = []
    if target == "local":
        for src in remote_files:
            out = dest / Path(src).name
            shutil.copy2(src, out)
            local_files.append(out)
    else:
        # One scp invocation with many sources is far faster than N invocations.
        # Flatten into dest/; session ids are unique so collisions are unlikely.
        quoted = " ".join(shlex.quote(f) for f in remote_files)
        # scp needs the remote paths as a single remote spec list
        specs = [f"{target}:{shlex.quote(f)}" for f in remote_files]
        r = run(["scp", *SSH_OPTS, "-q", *specs, str(dest)])
        if r.returncode != 0:
            print(f"  [{name}] scp failed: {r.stderr.strip()[:200]}", file=sys.stderr)
            return plat, []
        local_files = sorted(dest.glob("*.jsonl"))

    if verbose:
        print(f"  [{name}] {plat['pretty']} ({plat['arch']}) — {len(local_files)} sessions")
    return plat, list(local_files)


# --------------------------------------------------------------------------- #
# Step 2 + 3: render + index
# --------------------------------------------------------------------------- #
def plausible_host(cwd: str, osname: str) -> bool:
    """Could a transcript with this cwd have been recorded on this OS?

    macOS homes live under /Users; Linux under /home, /root, /srv, /opt, /var.
    A Linux path attributed to a Darwin host means the transcript was collected
    from somewhere else -- usually a shared or synced ~/.claude, or a stale
    staging directory. Cheap check, catches a genuinely confusing failure.
    """
    if not cwd or not osname:
        return True
    if osname == "Darwin":
        return not cwd.startswith(("/home/", "/srv/", "/root/"))
    if osname == "Linux":
        return not cwd.startswith("/Users/")
    return True


def dedupe_records(records, keep_duplicates=False):
    """Collapse the same session appearing on more than one host.

    A session id is globally unique, so the same id on two machines means one
    transcript reachable from both (shared home, rsync, restored backup) -- not
    two sessions. Rendering it once per host is wrong and was showing projects
    twice in the index.

    Byte-identical copies collapse to a single primary record; the other hosts
    are recorded in `also_on`. Same id with *different* content is a real
    conflict: keep both and say so.

    Primary host preference: one whose platform is consistent with the
    transcript's cwd, then the one with more turns, then alphabetical -- so the
    result is deterministic across runs.
    """
    by_id = {}
    for r in records:
        by_id.setdefault(r["session_id"], []).append(r)

    kept, dupes, conflicts = [], 0, []
    for sid, group in by_id.items():
        if len(group) == 1 or keep_duplicates:
            kept.extend(group)
            continue

        hashes = {r["content_hash"] for r in group}
        if len(hashes) > 1:
            # Same id, different bytes. Don't silently pick one.
            conflicts.append((sid, sorted(r["host"] for r in group)))
            for r in group:
                r["conflict"] = True
            kept.extend(group)
            continue

        primary = sorted(group, key=lambda r: (
            not plausible_host(r["cwd"], r["os"]),   # plausible first
            -r["turns"],                             # then richer
            r["host"],                               # then stable
        ))[0]
        primary["also_on"] = sorted(r["host"] for r in group if r is not primary)
        dupes += len(group) - 1
        kept.append(primary)

    kept.sort(key=lambda r: r["first_ts"] or "", reverse=True)
    return kept, {"removed": dupes, "conflicts": conflicts}


def _uuid_graph(entries):
    """Return (own_uuids, external_parent_uuid).

    Entries in a transcript chain to each other via parentUuid. A sidechain root
    -- the first turn of a subagent -- chains to an entry that lives in the
    SPAWNING session, i.e. a uuid this transcript does not contain. That dangling
    reference is what lets us reattach a subagent to its parent.
    """
    own = {e["uuid"] for e in entries if e.get("uuid")}
    external = None
    for e in entries:
        p = e.get("parentUuid")
        if p and p not in own:
            external = p
            break
    return own, external


def render_all(host_data, site: Path, include_thinking=True, do_redact=True,
               skip_subagents=False, keep_duplicates=False):
    """host_data: {name: (platform, [paths])}.

    Returns (records, Counter, uuid_index, stats). uuid_index maps
    (host, uuid) -> session_id so subagents can be reattached to their parent.

    Deduplication happens before rendering, so a session reachable from two
    machines produces one page, not two.
    """
    from collections import Counter
    grand = Counter()
    uuid_index = {}
    pending = []  # (meta, entries, note, plat, name, path)

    for name, (plat, paths) in host_data.items():
        for p in paths:
            p = Path(p)
            raw = p.read_bytes()
            entries = load_jsonl(p)
            if not entries:
                continue

            note, n_red = "", 0
            if do_redact:
                entries, counter = redact_entries(entries)
                grand.update(counter)
                note = summarize(counter)
                n_red = sum(counter.values())

            meta = extract_meta(entries, p)
            if meta["turns"] == 0:
                continue  # skip empty/aborted sessions
            if skip_subagents and meta["is_subagent"]:
                continue

            own, external = _uuid_graph(entries)
            for u in own:
                uuid_index[(name, u)] = meta["session_id"]

            meta.update({
                "parent_uuid": external,
                "host": name,
                "platform": plat.get("pretty", "?"),
                "os": plat.get("os", "?"),
                "arch": plat.get("arch", ""),
                "redactions": n_red,
                "content_hash": hashlib.sha256(raw).hexdigest(),
                "also_on": [],
                "conflict": False,
            })
            pending.append((meta, entries, note, name, p))

    metas = [m for m, *_ in pending]
    kept, dstats = dedupe_records(metas, keep_duplicates=keep_duplicates)
    keep_ids = {id(m) for m in kept}

    mismatched = [m for m in kept if not plausible_host(m["cwd"], m["os"])]

    # Render after dedupe and after the uuid index is complete, so nothing
    # depends on file iteration order.
    records = []
    for meta, entries, note, name, p in pending:
        if id(meta) not in keep_ids:
            continue
        (site / name).mkdir(parents=True, exist_ok=True)
        doc = build_html(entries, meta["description"], f"{name}:{p.name}",
                         include_thinking=include_thinking,
                         redaction_note=note, redacted=do_redact)
        rel = f"{name}/{meta['session_id']}.html"
        (site / rel).write_text(doc, encoding="utf-8")
        meta["href"] = rel
        meta["bytes"] = (site / rel).stat().st_size
        records.append(meta)

    records.sort(key=lambda r: r["first_ts"] or "", reverse=True)
    dstats["mismatched"] = mismatched
    return records, grand, uuid_index, dstats


def link_subagents(records, uuid_index):
    """Attach each subagent record to a parent session id, in place.

    Two strategies, in order:
      1. Exact: the subagent's dangling parentUuid resolves, on the same host,
         to a uuid owned by another session.
      2. Heuristic: same host and same project directory, and the subagent ran
         inside the parent's time window. Where several sessions qualify, the
         one that started most recently before the subagent wins.

    Anything still unattached is an orphan and gets surfaced under its project
    rather than silently dropped.
    """
    by_id = {r["session_id"]: r for r in records}
    mains = [r for r in records if not r["is_subagent"]]
    exact = heuristic = orphan = 0

    for r in records:
        r["parent_id"] = None
        r["children"] = []
    for r in records:
        if not r["is_subagent"]:
            continue

        pid = uuid_index.get((r["host"], r.get("parent_uuid")))
        if pid and pid != r["session_id"] and pid in by_id:
            r["parent_id"] = pid
            r["link"] = "exact"
            exact += 1
            continue

        cands = [
            m for m in mains
            if m["host"] == r["host"] and m["cwd"] == r["cwd"]
            and m["first_ts"] and m["last_ts"] and r["first_ts"]
            and m["first_ts"] <= r["first_ts"] <= m["last_ts"]
        ]
        if cands:
            best = max(cands, key=lambda m: m["first_ts"])
            r["parent_id"] = best["session_id"]
            r["link"] = "inferred"
            heuristic += 1
        else:
            r["link"] = "orphan"
            orphan += 1

    for r in records:
        if r["parent_id"] and r["parent_id"] in by_id:
            by_id[r["parent_id"]]["children"].append(r)
    for m in mains:
        m["children"].sort(key=lambda c: c["first_ts"] or "")

    return {"exact": exact, "inferred": heuristic, "orphan": orphan}


INDEX_CSS = """
:root{--bg:#faf9f7;--fg:#1f1f1d;--muted:#6b6a66;--line:#e6e3dd;--card:#fff;--accent:#c15f3c;
--agent:#7a4f9c;}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a18;--fg:#e8e6df;--muted:#9a978f;
--line:#33322e;--card:#1f1e1c;--agent:#a781c9;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 80px;}
h1{font-size:24px;margin:0 0 4px;}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px;}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;align-items:center;}
input[type=search]{flex:1;min-width:220px;padding:8px 12px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg);font-size:14px;}
select{padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg);font-size:14px;}

/* project -> session -> subagent hierarchy */
.project{margin:30px 0 0;}
.project-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
padding:0 0 6px;border-bottom:1px solid var(--line);margin-bottom:6px;}
.project-name{font-size:15px;font-weight:700;}
.project-path{color:var(--muted);font-size:12px;font-family:ui-monospace,Menlo,monospace;
overflow-wrap:anywhere;}
.project-meta{color:var(--muted);font-size:12px;margin-left:auto;}

.session{margin:8px 0;}
.row{display:block;text-decoration:none;color:inherit;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:12px 14px;
transition:border-color .12s,transform .12s;}
.row:hover{border-color:var(--accent);transform:translateY(-1px);}
.row .desc{font-weight:600;margin-bottom:6px;overflow-wrap:anywhere;}
.row .facts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
color:var(--muted);font-size:12px;}

/* subagents: nested, quieter, collapsed by default */
.subs{margin:2px 0 0 22px;border-left:2px solid var(--line);padding-left:12px;}
.subs>summary{cursor:pointer;list-style:none;user-select:none;
font-size:12px;color:var(--muted);padding:5px 0;}
.subs>summary::-webkit-details-marker{display:none}
.subs>summary:before{content:"\25b8 ";color:var(--agent);font-weight:700;}
.subs[open]>summary:before{content:"\25be ";}
.subs>summary:hover{color:var(--fg);}
.row.sub{background:transparent;border-style:dashed;padding:8px 10px;margin:4px 0;}
.row.sub .desc{font-weight:500;font-size:13px;color:var(--muted);margin-bottom:4px;}
.row.sub:hover .desc{color:var(--fg);}
.row.sub .facts{font-size:11px;}

.badge{display:inline-block;font-size:11px;font-weight:650;padding:2px 8px;border-radius:20px;
letter-spacing:.02em;color:#fff;}
.host-web{background:#2f6f9f;}.host-axiom{background:#7a4f9c;}
.host-laptop{background:#4a7c59;}.host-other{background:#7a7a72;}
.agent-badge{background:transparent;border:1px solid var(--agent);color:var(--agent);}
.redact-badge{background:transparent;border:1px solid #b3453a;color:#b3453a;}
.link-inferred{border:1px dashed var(--muted);color:var(--muted);background:transparent;}
.also-on{background:transparent;border:1px solid var(--muted);color:var(--muted);}
.conflict-badge{background:#b3453a;color:#fff;}
.orphan-head{color:var(--muted);}
.os-Linux{border:1px solid #d9772e;color:#d9772e;background:transparent;}
.os-Darwin{border:1px solid #6b6a66;color:var(--muted);background:transparent;}
.os-\\?{border:1px solid var(--line);color:var(--muted);background:transparent;}
code{background:rgba(127,127,127,.15);padding:1px 5px;border-radius:4px;
font:12px ui-monospace,Menlo,Consolas,monospace;}
.empty{color:var(--muted);padding:30px 0;text-align:center;}
footer{margin-top:44px;color:var(--muted);font-size:12px;text-align:center;
border-top:1px solid var(--line);padding-top:16px;}
.hostsum{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px;font-size:12px;color:var(--muted);}
"""


def _host_cls(host):
    return f"host-{host}" if host in ("web", "axiom", "laptop") else "host-other"


def _facts(r, compact=False):
    bits = [f'<span class="badge {_host_cls(r["host"])}">{esc(r["host"])}</span>']
    if not compact:
        bits.append(f'<span class="badge os-{esc(r["os"])}">{esc(r["platform"])}</span>')

    if r.get("also_on"):
        others = ", ".join(r["also_on"])
        bits.append(f'<span class="badge also-on" title="Identical transcript also on: '
                    f'{esc(others)}">also on {esc(others)}</span>')
    if r.get("conflict"):
        bits.append('<span class="badge conflict-badge" title="Same session id but '
                    'different content on another host">id conflict</span>')
    if not plausible_host(r.get("cwd", ""), r.get("os", "")):
        bits.append(f'<span class="badge conflict-badge" title="This working directory '
                    f'cannot exist on a {esc(r.get("os",""))} host; the host label is '
                    f'probably wrong">host mismatch</span>')

    if r.get("is_subagent"):
        at = f' {esc(r["agent_type"])}' if r.get("agent_type") else ""
        bits.append(f'<span class="badge agent-badge">subagent{at}</span>')
        if r.get("link") == "inferred":
            bits.append('<span class="badge link-inferred" '
                        'title="Parent inferred from project and time window, '
                        'not an explicit link">inferred parent</span>')

    if r.get("redactions"):
        n = r["redactions"]
        bits.append(f'<span class="badge redact-badge" '
                    f'title="{n} secret{"s" if n != 1 else ""} scrubbed">'
                    f'&#128737; {n}</span>')

    started = _fmt_ts(r["first_ts"])[:16] if r["first_ts"] else ""
    bits += [x for x in [
        started,
        f"{r['turns']} turns",
        f"{r['tool_calls']} tool calls" if r["tool_calls"] else "",
        f"<code>{esc(r['session_id'][:8])}</code>",
    ] if x]
    return " &middot; ".join(bits)


def _search_blob(r):
    return esc(" ".join([
        r["description"], r["host"], r["platform"], r["project"], r["cwd"],
        r["session_id"], "subagent" if r["is_subagent"] else "main",
        r.get("agent_type", ""),
    ]).lower())


def _row(r, sub=False):
    cls = "row sub" if sub else "row"
    kind = "subagent" if r["is_subagent"] else "main"
    return (
        f'<a class="{cls}" href="{esc(r["href"])}" data-host="{esc(r["host"])}" '
        f'data-kind="{kind}" data-search="{_search_blob(r)}">'
        f'<div class="desc">{esc(r["description"])}</div>'
        f'<div class="facts">{_facts(r, compact=sub)}</div></a>'
    )


def _subs_block(children):
    """Collapsed <details> holding a session's subagent runs."""
    if not children:
        return ""
    n = len(children)
    label = f"{n} subagent run{'s' if n != 1 else ''}"
    inner = "".join(_row(c, sub=True) for c in children)
    return (f'<details class="subs"><summary data-subsummary>{label}</summary>'
            f'{inner}</details>')


def build_index(records, host_data, link_stats=None) -> str:
    hosts = sorted({r["host"] for r in records})
    generated = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    host_opts = "".join(f'<option value="{esc(h)}">{esc(h)}</option>' for h in hosts)

    mains = [r for r in records if not r["is_subagent"]]
    subs = [r for r in records if r["is_subagent"]]
    orphans = [r for r in subs if not r.get("parent_id")]

    # Per-host summary (includes hosts with zero sessions / unreachable).
    sums = []
    for name, (plat, paths) in sorted(host_data.items()):
        n = sum(1 for r in mains if r["host"] == name)
        a = sum(1 for r in subs if r["host"] == name)
        p = plat.get("pretty", "?")
        arch = plat.get("arch", "")
        arch = f" &middot; {esc(arch)}" if arch else ""
        extra = f" (+{a} subagent)" if a else ""
        sums.append(f"<span><strong>{esc(name)}</strong>: {esc(p)}{arch} &middot; "
                    f"{n} sessions{extra}</span>")

    # ---- group into projects, keyed by (host, cwd) ------------------------
    projects = {}
    for r in mains + orphans:
        projects.setdefault((r["host"], r["cwd"]), {"mains": [], "orphans": []})
    for r in mains:
        projects[(r["host"], r["cwd"])]["mains"].append(r)
    for r in orphans:
        projects[(r["host"], r["cwd"])]["orphans"].append(r)

    def last_activity(bucket):
        ts = [m["last_ts"] or m["first_ts"] or "" for m in bucket["mains"]]
        ts += [o["last_ts"] or o["first_ts"] or "" for o in bucket["orphans"]]
        return max(ts) if ts else ""

    ordered = sorted(projects.items(), key=lambda kv: last_activity(kv[1]), reverse=True)

    sections = []
    for (host, cwd), bucket in ordered:
        bucket["mains"].sort(key=lambda m: m["first_ts"] or "", reverse=True)
        bucket["orphans"].sort(key=lambda m: m["first_ts"] or "", reverse=True)

        name = os.path.basename(cwd) or cwd or "(unknown project)"
        n_sess = len(bucket["mains"])
        n_sub = sum(len(m["children"]) for m in bucket["mains"]) + len(bucket["orphans"])
        last = last_activity(bucket)[:10]

        body = []
        for m in bucket["mains"]:
            body.append(f'<div class="session" data-session>'
                        f'{_row(m)}{_subs_block(m["children"])}</div>')

        if bucket["orphans"]:
            n = len(bucket["orphans"])
            inner = "".join(_row(o, sub=True) for o in bucket["orphans"])
            body.append(
                f'<div class="session" data-session>'
                f'<details class="subs"><summary data-subsummary class="orphan-head">'
                f'{n} unattached subagent run{"s" if n != 1 else ""} '
                f'&mdash; no parent session found</summary>{inner}</details></div>'
            )

        meta = f"{n_sess} session{'s' if n_sess != 1 else ''}"
        if n_sub:
            meta += f" &middot; {n_sub} subagent"
        if last:
            meta += f" &middot; last {esc(last)}"

        sections.append(
            f'<section class="project" data-project data-host="{esc(host)}">'
            f'<div class="project-head">'
            f'<span class="project-name">{esc(name)}</span>'
            f'<span class="badge {_host_cls(host)}">{esc(host)}</span>'
            f'<span class="project-path">{esc(cwd)}</span>'
            f'<span class="project-meta">{meta}</span>'
            f'</div>{"".join(body)}</section>'
        )

    stats = ""
    if link_stats and subs:
        bits = [f"{link_stats['exact']} linked"]
        if link_stats["inferred"]:
            bits.append(f"{link_stats['inferred']} inferred")
        if link_stats["orphan"]:
            bits.append(f"{link_stats['orphan']} unattached")
        stats = f" &middot; {len(subs)} subagent runs ({', '.join(bits)})"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Claude Code sessions</title>
<style>{INDEX_CSS}</style>
</head>
<body>
<div class="wrap">
<h1>Claude Code sessions</h1>
<div class="sub">{len(mains)} sessions across {len(host_data)} machines{stats} &middot;
generated {esc(generated)}</div>
<div class="hostsum">{"".join(sums)}</div>

<div class="controls">
  <input type="search" id="q" placeholder="Search descriptions, projects, session ids&hellip;" autocomplete="off">
  <select id="hostf"><option value="">All machines</option>{host_opts}</select>
  <select id="subf">
    <option value="collapsed">Subagents: collapsed</option>
    <option value="expanded">Subagents: expanded</option>
    <option value="hidden">Subagents: hidden</option>
  </select>
</div>

<div id="list">
{"".join(sections) if sections else '<div class="empty">No sessions found.</div>'}
</div>

<footer>Self-hosted transcript index &middot; no external requests &middot; serve behind auth</footer>
</div>
<script>
(function(){{
  var q=document.getElementById('q'), hf=document.getElementById('hostf'),
      sf=document.getElementById('subf');
  var projects=[].slice.call(document.querySelectorAll('[data-project]'));
  var sessions=[].slice.call(document.querySelectorAll('[data-session]'));

  function apply(){{
    var t=q.value.trim().toLowerCase(), h=hf.value, mode=sf.value;

    sessions.forEach(function(sess){{
      var main=sess.querySelector('.row:not(.sub)');
      var subs=[].slice.call(sess.querySelectorAll('.row.sub'));
      var det=sess.querySelector('details.subs');

      var mainOk = main ? ((!t||main.dataset.search.indexOf(t)>-1)
                          &&(!h||main.dataset.host===h)) : false;
      var subHits=0;
      subs.forEach(function(s){{
        var ok=(!t||s.dataset.search.indexOf(t)>-1)&&(!h||s.dataset.host===h);
        s.style.display=(t&&!ok)?'none':'';
        if(ok) subHits++;
      }});

      if(det){{
        det.style.display=(mode==='hidden'||subHits===0)?'none':'';
        det.open=(mode==='expanded')||(!!t&&subHits>0);
      }}
      var keep = mainOk || (subHits>0 && mode!=='hidden');
      if(!main) keep = subHits>0 && mode!=='hidden';
      sess.style.display=keep?'':'none';
    }});

    projects.forEach(function(p){{
      var any=[].slice.call(p.querySelectorAll('[data-session]'))
                 .some(function(s){{return s.style.display!=='none';}});
      var hostOk=!h||p.dataset.host===h;
      p.style.display=(any&&hostOk)?'':'none';
    }});
  }}

  q.addEventListener('input',apply);
  hf.addEventListener('change',apply);
  sf.addEventListener('change',apply);
  apply();
}})();
</script>
</body>
</html>
"""



# --------------------------------------------------------------------------- #
# Step 4: deploy
# --------------------------------------------------------------------------- #
def deploy(site: Path, target: str, dry_run=False):
    """scp the built tree to e.g. web.example.com:sessions/"""
    host, _, remote_path = target.partition(":")
    remote_path = remote_path or "sessions"
    mk = ["ssh", *SSH_OPTS, host, f"mkdir -p {shlex.quote(remote_path)}"]
    cp = ["scp", *SSH_OPTS, "-q", "-r", *[str(p) for p in site.iterdir()],
          f"{host}:{shlex.quote(remote_path)}/"]
    if dry_run:
        print("  would run:", " ".join(mk))
        print("  would run:", " ".join(cp[:6]), f"... ({len(list(site.iterdir()))} items)")
        return True
    r = run(mk)
    if r.returncode != 0:
        print(f"  mkdir failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    r = run(cp)
    if r.returncode != 0:
        print(f"  scp failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return False
    print(f"  deployed to {host}:{remote_path}/")
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hosts-file", type=Path, default=Path("hosts.conf"))
    ap.add_argument("--host", action="append", default=[],
                    metavar="NAME=TARGET", help="Add/override a host (repeatable)")
    ap.add_argument("--site", type=Path, default=Path("_site"), help="Local build dir")
    ap.add_argument("--staging", type=Path, default=None,
                    help="Where fetched .jsonl land (default: temp dir)")
    ap.add_argument("--deploy-to", default=None,
                    help="scp destination, overriding the 'deploy' line in hosts.conf")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Reuse an existing --staging dir instead of SSHing")
    ap.add_argument("--no-deploy", action="store_true", help="Build locally, don't scp")
    ap.add_argument("--dry-run", action="store_true", help="Show actions, change nothing remote")
    ap.add_argument("--no-thinking", action="store_true", help="Omit thinking blocks")
    ap.add_argument("--skip-subagents", action="store_true",
                    help="Don't render subagent runs at all; omit them from the index")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="Don't collapse a session found on more than one host")
    ap.add_argument("--explain", action="store_true",
                    help="Print where each session's description came from, then exit")
    ap.add_argument("--no-redact", action="store_true",
                    help="DANGEROUS: publish raw transcripts without scrubbing credentials")
    ap.add_argument("--jobs", type=int, default=4, help="Parallel host fetches")
    args = ap.parse_args(argv)

    if args.no_redact and not args.no_deploy:
        print("WARNING: --no-redact with deploy enabled. Raw credentials from every\n"
              "         machine will be published. Ctrl-C now if that isn't intended.",
              file=sys.stderr)

    # Resolve host list + deploy target.
    hosts, cfg_deploy = [], ""
    if args.hosts_file.exists():
        hosts, cfg_deploy = parse_hosts_file(args.hosts_file)
    elif not args.host:
        hosts = DEFAULT_HOSTS
        print(f"note: {args.hosts_file} not found; using defaults "
              f"({', '.join(n for n, _ in hosts)})")
    overrides = dict(h.split("=", 1) for h in args.host if "=" in h)
    hosts = [(n, overrides.pop(n, t)) for n, t in hosts]
    hosts += list(overrides.items())
    if not hosts:
        sys.exit("error: no hosts configured")

    # CLI beats hosts.conf. No silent fallback to a placeholder hostname.
    deploy_target = args.deploy_to or cfg_deploy
    if not deploy_target and not (args.no_deploy or args.dry_run):
        sys.exit(
            "error: no deploy target configured.\n"
            f"  Add a line to {args.hosts_file}:\n"
            "      deploy web.example.com:sessions\n"
            "  or pass --deploy-to HOST:PATH, or build locally with --no-deploy."
        )

    staging = args.staging or Path(tempfile.mkdtemp(prefix="cc-staging-"))
    staging.mkdir(parents=True, exist_ok=True)
    site = args.site

    print(f"hosts:   {', '.join(f'{n}({t})' for n, t in hosts)}")
    print(f"staging: {staging}")
    print(f"site:    {site}")

    # ---- 1. fetch
    host_data = {}
    if args.no_fetch:
        print("\n[1/4] fetch — skipped (--no-fetch), reading staging dir")
        for name, target in hosts:
            d = staging / name
            paths = sorted(d.glob("*.jsonl")) if d.exists() else []
            plat_file = d / "_platform.json"
            plat = json.loads(plat_file.read_text()) if plat_file.exists() \
                else {"pretty": "unknown", "os": "?", "arch": ""}
            host_data[name] = (plat, paths)
            print(f"  [{name}] {len(paths)} sessions from staging")
    else:
        print(f"\n[1/4] fetch — probing {len(hosts)} hosts over SSH")
        if args.dry_run:
            for name, target in hosts:
                if target == "local":
                    print(f"  [{name}] would scan {Path.home() / '.claude' / 'projects'} (local)")
                else:
                    print(f"  [{name}] would ssh {target}, then "
                          f"scp {target}:{REMOTE_GLOB}/**/*.jsonl")
            if args.no_deploy:
                tail = "skip deploy"
            elif deploy_target:
                tail = f"scp → {deploy_target}/"
            else:
                tail = "NO DEPLOY TARGET (add a 'deploy' line to hosts.conf)"
            print(f"  would render → {args.site}, then {tail}")
            return 0
        with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(fetch_host, n, t, staging): n for n, t in hosts}
            for fut in cf.as_completed(futs):
                name = futs[fut]
                plat, paths = fut.result()
                host_data[name] = (plat, paths)
                (staging / name).mkdir(parents=True, exist_ok=True)
                (staging / name / "_platform.json").write_text(json.dumps(plat))

    total = sum(len(p) for _, p in host_data.values())
    if total == 0:
        print("\nNo transcripts found on any host. Nothing to do.", file=sys.stderr)
        return 1

    # ---- 2 + 3. render + index
    print(f"\n[2/4] render — {total} sessions"
          f"{'' if not args.no_redact else '  (REDACTION DISABLED)'}")
    clean_dir(site)
    site.mkdir(parents=True, exist_ok=True)
    records, grand, uuid_index, dstats = render_all(
        host_data, site,
        include_thinking=not args.no_thinking,
        do_redact=not args.no_redact,
        skip_subagents=args.skip_subagents,
        keep_duplicates=args.keep_duplicates)
    n_main = sum(1 for r in records if not r["is_subagent"])
    n_agents = len(records) - n_main
    print(f"  rendered {n_main} sessions"
          + (f" + {n_agents} subagent runs" if n_agents else ""))

    if dstats["removed"]:
        print(f"  deduped {dstats['removed']} transcript(s) reachable from more "
              f"than one host")
    for sid, hosts in dstats["conflicts"]:
        print(f"  WARNING: session {sid[:8]} differs between {', '.join(hosts)}; "
              f"keeping both", file=sys.stderr)
    if dstats["mismatched"]:
        print(f"  WARNING: {len(dstats['mismatched'])} transcript(s) have a working "
              f"directory that can't exist on the host they came from:", file=sys.stderr)
        for m in dstats["mismatched"][:5]:
            print(f"    {m['session_id'][:8]}  host={m['host']} ({m['os']})  "
                  f"cwd={m['cwd']}", file=sys.stderr)
        print("    -> a shared/synced ~/.claude, or a stale --staging dir. "
              "Check before trusting the host labels.", file=sys.stderr)

    if args.explain:
        print("\nDescription provenance:")
        for r in sorted(records, key=lambda r: (r["host"], r["first_ts"] or "")):
            foreign = f"  foreign-sessions={len(r['foreign_sessions'])}" if r["foreign_sessions"] else ""
            print(f"  {r['session_id'][:8]}  {r['host']:8} "
                  f"{r['description_source']:16} {r['description'][:52]!r}{foreign}")
        return 0

    if not args.no_redact:
        s = summarize(grand)
        print(f"  redaction: {s or 'no known secret patterns found'}")

    print("\n[3/4] index")
    link_stats = link_subagents(records, uuid_index)
    if n_agents:
        print(f"  subagents: {link_stats['exact']} linked by uuid, "
              f"{link_stats['inferred']} inferred from project+time, "
              f"{link_stats['orphan']} unattached")
    (site / "index.html").write_text(
        build_index(records, host_data, link_stats), encoding="utf-8")
    n_proj = len({(r["host"], r["cwd"]) for r in records if not r["is_subagent"]})
    span = ""
    if records:
        span = f" ({records[-1]['first_ts'][:10]} → {records[0]['first_ts'][:10]})"
    span += f", {n_proj} projects"
    print(f"  index.html — {len(records)} sessions{span}")

    # ---- 4. deploy
    print("\n[4/4] deploy")
    if args.no_deploy:
        print(f"  skipped (--no-deploy). Built tree: {site.resolve()}")
    else:
        ok = deploy(site, deploy_target, dry_run=args.dry_run)
        if not ok:
            return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
