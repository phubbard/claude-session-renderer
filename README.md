# claude-session-renderer

Render [Claude Code](https://code.claude.com) session transcripts into self-contained
HTML pages, aggregate them across machines, and serve the result behind a password.

A self-hosted stand-in for Claude Code's shared Artifacts feature, which is currently
beta and limited to Team/Enterprise plans with organization-only sharing. This needs
none of that: your transcripts, your web server, your auth.

**Credentials are scrubbed by default.**

## What you get

- `publish_session.py` — one transcript → one styled, self-contained HTML page.
- `cc_collect.py` — SSH to N machines, render everything, build a date-ordered index, deploy.
- `redact.py` — the secret scrubber. Runs by default in both tools.
- `Caddyfile.example` — Caddy2 config with `basic_auth`.

Generated pages make zero external requests (CSS and JS are inlined), so they work
behind auth, offline, and under a strict CSP.

## Where transcripts live

Claude Code writes one JSONL file per session:

```
~/.claude/projects/<slugified-project-path>/<session-id>.jsonl
```

You rarely need to know this — the tools find them.

## Quick start

Publish a single session:

```bash
python3 publish_session.py --latest --project . -o /var/www/sessions/auth.html \
  --title "Auth refactor walkthrough"
```

Aggregate every session from every machine:

```bash
cp hosts.conf.example hosts.conf   # then edit
python3 cc_collect.py --dry-run    # show every ssh/scp, touch nothing
python3 cc_collect.py --no-deploy  # build into ./_site and inspect
python3 cc_collect.py              # full run, scp → your deploy target
```

`cc_collect.py` must run somewhere your SSH agent works — your laptop, not a sandbox.
It uses `BatchMode=yes`, so key-based auth to each host is required.

### hosts.conf

All configuration lives here — the machines to collect from, and where to publish.

```
# name    ssh-target        ("local" = the machine running the script)
web       you@web.example.com
axiom     you@axiom.example.com
laptop    local

# where to publish the built site
deploy    web.example.com:sessions
```

Gitignored, because it holds real hostnames. That's deliberate: the deploy target
lives here rather than as a default inside `cc_collect.py`, so your infrastructure
names never reach the public repo.

`deploy` is a reserved first word (no machine may be named `deploy`), and at most one
`deploy` line is allowed. A bare path is relative to the remote user's home, so
`web.example.com:sessions` lands in `~/sessions`; use an absolute path like
`web.example.com:/var/www/sessions` if your web root is elsewhere.

**There is no built-in deploy default.** With no `deploy` line and no `--deploy-to`,
the run stops with an error rather than scp'ing somewhere you didn't intend.

Override ad hoc: `--host name=user@target` (repeatable) for machines, and
`--deploy-to HOST:PATH` for the target. CLI beats `hosts.conf`.

## What the pipeline does

1. **fetch** — per host, probe platform (`uname`, `/etc/os-release`, `sw_vers`), find
   `~/.claude/projects/**/*.jsonl`, `scp` them into staging. Hosts run in parallel
   (`--jobs`, default 4). Unreachable hosts are reported and skipped, never fatal.
2. **render** — each transcript → `_site/<host>/<session-id>.html`. Redaction runs here.
   Empty/aborted sessions (zero turns) are dropped.
3. **index** — `_site/index.html`, newest first, grouped by day. Live search box,
   per-machine filter, main-vs-subagent filter.
4. **deploy** — `ssh mkdir -p` then `scp -r` to your web root.

## Redaction

On by default, in both tools. `redact.py` walks every string in the transcript and
replaces anything matching a known credential shape with `[REDACTED:<rule>]`, rendered
as a highlighted mark. Each page carries a banner reporting what was scrubbed.

Covered: private key and certificate blocks, SSH public keys, JWTs, AWS access key IDs,
GitHub tokens and PATs, Slack tokens and webhooks, Stripe keys, Anthropic and OpenAI
keys, Google API keys, npm and PyPI tokens, SendGrid keys, Twilio SIDs, bcrypt hashes,
`Bearer`/`Basic` auth headers, credentials embedded in URLs (`postgres://user:pw@host`),
`UPPERCASE_SECRET=value` assignments, and quoted or unquoted `password:` style keys.

Deliberately **not** covered: generic entropy detection. It false-positives on hashes,
UUIDs, base64 blobs, and minified code, which trains you to ignore the output.

Rules are ordered most-specific first and replacements are idempotent, so running twice
is a no-op and counts aren't inflated by broader rules re-matching scrubbed text.

Run the test suite — 33 secret types must be caught, 13 benign strings must survive:

```bash
python3 tests/test_redact.py
```

### This is a safety net, not a guarantee

A secret with no recognizable shape passes straight through: a bare password echoed by a
command, a customer's PII in a log dump, an internal hostname, a proprietary algorithm.
Redaction cannot know those are sensitive.

The risk scales with `cc_collect.py`. Publishing one transcript you just read is very
different from bulk-publishing every session from a production host. **Do a `--no-deploy`
run and skim `_site/index.html` before your first real publish.** `--no-redact` exists,
prints a warning, and should stay unused.

## Subagent sessions

Claude Code marks subagent turns with `isSidechain`. A transcript whose only user turns
are sidechain turns is a subagent run with no top-level prompt. Rather than showing these
as "(no description)", descriptions fall back through: the session `summary` → the first
top-level user prompt → the spawning `Task` call's description/prompt → the first
sidechain turn (the agent's own instructions) → the first assistant prose → a labeled
placeholder. Subagent rows get a badge, a left border, and their own index filter.

## Serving it

`Caddyfile.example` roots at `/var/www/sessions`. Deploying to `web:sessions/` lands in
`~/sessions` instead — point `root *` at one or the other, or pass
`--deploy-to host:/var/www/sessions`. Generate the password hash with
`caddy hash-password`. Drop `file_server browse` if you don't want a raw directory
listing next to `index.html`.

Pages set `noindex`, but auth is what actually keeps them private.

## Useful flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print every ssh/scp; change nothing |
| `--no-deploy` | Build locally into `./_site` |
| `--no-fetch` | Rebuild from an existing `--staging` dir; no SSH |
| `--no-thinking` | Omit Claude's thinking blocks |
| `--no-redact` | Publish raw. Warns loudly. Don't. |
| `--staging DIR` | Keep fetched JSONL around |
| `--deploy-to H:P` | scp destination; overrides the `deploy` line in `hosts.conf` |
| `--jobs N` | Parallel host fetches |

## Scheduling

```cron
30 2 * * * cd /home/you/claude-session-renderer && /usr/bin/python3 cc_collect.py >> publish.log 2>&1
```

## Requirements

Python 3.9+, `ssh`/`scp`, and a web server. No third-party Python packages.

## License

MIT
