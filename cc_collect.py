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
def render_all(host_data, site: Path, include_thinking=True, do_redact=True):
    """host_data: {name: (platform, [paths])}. Returns (records, total Counter)."""
    from collections import Counter
    records = []
    grand = Counter()
    for name, (plat, paths) in host_data.items():
        outdir = site / name
        outdir.mkdir(parents=True, exist_ok=True)
        for p in paths:
            p = Path(p)
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
            title = meta["description"]
            doc = build_html(entries, title, f"{name}:{p.name}",
                             include_thinking=include_thinking,
                             redaction_note=note, redacted=do_redact)
            rel = f"{name}/{meta['session_id']}.html"
            (site / rel).write_text(doc, encoding="utf-8")
            meta.update({
                "host": name,
                "platform": plat.get("pretty", "?"),
                "os": plat.get("os", "?"),
                "arch": plat.get("arch", ""),
                "href": rel,
                "bytes": (site / rel).stat().st_size,
                "redactions": n_red,
            })
            records.append(meta)
    # Newest first, by session start.
    records.sort(key=lambda r: r["first_ts"] or "", reverse=True)
    return records, grand


INDEX_CSS = """
:root{--bg:#faf9f7;--fg:#1f1f1d;--muted:#6b6a66;--line:#e6e3dd;--card:#fff;--accent:#c15f3c;}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a18;--fg:#e8e6df;--muted:#9a978f;
--line:#33322e;--card:#1f1e1c;}}
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
.group-date{margin:26px 0 8px;font-size:12px;font-weight:700;color:var(--muted);
text-transform:uppercase;letter-spacing:.06em;}
.row{display:block;text-decoration:none;color:inherit;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0;
transition:border-color .12s,transform .12s;}
.row:hover{border-color:var(--accent);transform:translateY(-1px);}
.row .desc{font-weight:600;margin-bottom:6px;overflow-wrap:anywhere;}
.row .facts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
color:var(--muted);font-size:12px;}
.badge{display:inline-block;font-size:11px;font-weight:650;padding:2px 8px;border-radius:20px;
letter-spacing:.02em;color:#fff;}
.host-web{background:#2f6f9f;}.host-axiom{background:#7a4f9c;}
.host-laptop{background:#4a7c59;}.host-other{background:#7a7a72;}
.agent-badge{background:#7a4f9c;}
.redact-badge{background:transparent;border:1px solid #b3453a;color:#b3453a;}
.row.subagent{border-left:3px solid #7a4f9c;}
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


def build_index(records, host_data) -> str:
    hosts = sorted({r["host"] for r in records})
    generated = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    host_opts = "".join(f'<option value="{esc(h)}">{esc(h)}</option>' for h in hosts)

    # Per-host summary line (includes hosts with zero sessions / unreachable).
    sums = []
    for name, (plat, paths) in sorted(host_data.items()):
        n = sum(1 for r in records if r["host"] == name)
        p = plat.get("pretty", "?")
        arch = plat.get("arch", "")
        arch = f" · {esc(arch)}" if arch else ""
        sums.append(f"<span><strong>{esc(name)}</strong>: {esc(p)}{arch} · {n} sessions</span>")

    rows, last_group = [], None
    for r in records:
        day = (r["first_ts"] or "")[:10]
        group = day or "undated"
        if group != last_group:
            label = group
            if day:
                try:
                    label = _dt.date.fromisoformat(day).strftime("%A, %d %B %Y")
                except ValueError:
                    pass
            rows.append(f'<div class="group-date" data-group>{esc(label)}</div>')
            last_group = group

        host_cls = f"host-{r['host']}" if r["host"] in ("web", "axiom", "laptop") else "host-other"
        os_cls = f"os-{r['os']}"
        started = _fmt_ts(r["first_ts"])[-8:] if r["first_ts"] else ""
        proj = f"<code>{esc(r['project'])}</code>" if r["project"] else ""
        tools = f"{r['tool_calls']} tool calls" if r["tool_calls"] else ""

        agent = ""
        if r.get("is_subagent"):
            at = f' {esc(r["agent_type"])}' if r.get("agent_type") else ""
            agent = f'<span class="badge agent-badge">subagent{at}</span>'
        red = ""
        if r.get("redactions"):
            n = r["redactions"]
            red = (f'<span class="badge redact-badge" '
                   f'title="{n} secret{"s" if n != 1 else ""} scrubbed">'
                   f'🛡 {n} redacted</span>')

        facts = " · ".join(x for x in [
            f'<span class="badge {host_cls}">{esc(r["host"])}</span>',
            f'<span class="badge {os_cls}">{esc(r["platform"])}</span>',
            agent, red,
            started, proj, f"{r['turns']} turns", tools,
            f"<code>{esc(r['session_id'][:8])}</code>",
        ] if x)

        kind = "subagent" if r.get("is_subagent") else "main"
        search_blob = esc(" ".join([
            r["description"], r["host"], r["platform"], r["project"],
            r["session_id"], kind, r.get("agent_type", ""),
        ]).lower())

        rows.append(
            f'<a class="row{" subagent" if kind == "subagent" else ""}" '
            f'href="{esc(r["href"])}" data-host="{esc(r["host"])}" '
            f'data-kind="{kind}" data-search="{search_blob}">'
            f'<div class="desc">{esc(r["description"])}</div>'
            f'<div class="facts">{facts}</div></a>'
        )

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
<div class="sub">{len(records)} sessions across {len(host_data)} machines · generated {esc(generated)}</div>
<div class="hostsum">{"".join(sums)}</div>

<div class="controls">
  <input type="search" id="q" placeholder="Search descriptions, projects, session ids…" autocomplete="off">
  <select id="hostf"><option value="">All machines</option>{host_opts}</select>
  <select id="kindf">
    <option value="">Main + subagents</option>
    <option value="main">Main sessions only</option>
    <option value="subagent">Subagents only</option>
  </select>
</div>

<div id="list">
{"".join(rows) if rows else '<div class="empty">No sessions found.</div>'}
</div>

<footer>Self-hosted transcript index · no external requests · serve behind auth</footer>
</div>
<script>
(function(){{
  var q=document.getElementById('q'), hf=document.getElementById('hostf'),
      kf=document.getElementById('kindf');
  var rows=[].slice.call(document.querySelectorAll('.row'));
  var groups=[].slice.call(document.querySelectorAll('[data-group]'));
  function apply(){{
    var t=q.value.trim().toLowerCase(), h=hf.value, k=kf.value;
    rows.forEach(function(r){{
      var ok=(!t||r.dataset.search.indexOf(t)>-1)
           &&(!h||r.dataset.host===h)
           &&(!k||r.dataset.kind===k);
      r.style.display=ok?'':'none';
    }});
    // Hide date headers whose rows are all hidden.
    groups.forEach(function(g){{
      var n=g.nextElementSibling, any=false;
      while(n&&!n.hasAttribute('data-group')){{
        if(n.classList.contains('row')&&n.style.display!=='none') any=true;
        n=n.nextElementSibling;
      }}
      g.style.display=any?'':'none';
    }});
  }}
  q.addEventListener('input',apply); hf.addEventListener('change',apply);
  kf.addEventListener('change',apply);
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
    records, grand = render_all(host_data, site,
                                include_thinking=not args.no_thinking,
                                do_redact=not args.no_redact)
    n_agents = sum(1 for r in records if r.get("is_subagent"))
    print(f"  rendered {len(records)} non-empty sessions ({n_agents} subagent runs)")
    if not args.no_redact:
        s = summarize(grand)
        print(f"  redaction: {s or 'no known secret patterns found'}")

    print("\n[3/4] index")
    (site / "index.html").write_text(build_index(records, host_data), encoding="utf-8")
    span = ""
    if records:
        span = f" ({records[-1]['first_ts'][:10]} → {records[0]['first_ts'][:10]})"
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
