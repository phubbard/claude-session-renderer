# claude-session-renderer

Render [Claude Code](https://code.claude.com) session transcripts into self-contained
HTML pages, aggregate them across machines, and serve the result behind a password.

A self-hosted stand-in for Claude Code's shared Artifacts feature, which is currently
beta and limited to Team/Enterprise plans with organization-only sharing. This needs
none of that: your transcripts, your web server, your auth.

**Credentials are scrubbed by default.**

## What you get

- `publish_session.py` — one transcript → one styled, self-contained HTML page.
- `cc_collect.py` — SSH to N machines, render everything, build a full-text search
  index (pagefind) and a date-ordered browse index, deploy.
- `redact.py` — the secret scrubber. Runs by default in both tools.
- `Caddyfile.example` — Caddy2 config with `basic_auth`.

Generated pages make zero external requests (CSS and JS are inlined), so they work
behind auth, offline, and under a strict CSP.

## Where transcripts live

Claude Code writes one JSONL file per session:

```
~/.claude/projects/<slugified-project-path>/<session-id>.jsonl
```

The Cowork desktop app (local agent mode) keeps a separate `.claude/projects`
tree per sandbox under
`~/Library/Application Support/Claude/local-agent-mode-sessions/`. Add a
machine line with the reserved target `cowork` to collect those too — Cowork
projects show up with their sandbox names (`/sessions/cool-nice-maxwell`).

You rarely need to know this — the tools find them.

## Chat conversations (claude.ai export)

claude.ai **chat** sessions — including chats in the desktop app — never write a
transcript to disk, even when they edit local files; the conversation lives only
in your claude.ai account. The one way in is the account data export
(claude.ai → Settings → Privacy → Export data), which arrives as a
`data-...-batch-0000.zip` holding `conversations.json`.

`chat_export.py` converts that export into transcripts this renderer
understands (text, thinking, tool calls, attachment text — all through the same
redaction). Use it standalone:

```bash
python3 chat_export.py                # newest export in ~/Downloads → ./chat-transcripts/
python3 chat_export.py export.zip -o outdir
```

or add the reserved target `chats` to `hosts.conf` and `cc_collect.py` will
convert the newest export it finds in `~/Downloads` or
`~/.local/state/cc_collect/chat-exports/` on every run, grouped under a
`claude.ai` project with their own badge. Conversations deleted from your
account disappear from the site at the next conversion (the converter mirrors
the export). The export is a manual download, so refresh it whenever you want
newer chats on the site; with `--changed-only`, dropping a new export in is
itself enough to make the next scheduled run publish.

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
cowork    cowork            # Cowork desktop app sessions on this Mac

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

1. **fetch** — per host, probe platform (`uname`, `/etc/os-release`, `sw_vers`), then
   `rsync` `~/.claude/projects/**/*.jsonl` into a persistent staging dir
   (`~/.local/state/cc_collect/staging`), so only new/changed transcripts cross the
   wire; falls back to a full `scp` where rsync is missing. Hosts run in parallel
   (`--jobs`, default 4). Unreachable hosts are reported and skipped, never fatal.
2. **render** — each transcript → `_site/<host>/<session-id>.html`. Redaction runs here.
   Empty/aborted sessions (zero turns) are dropped.
3. **search** — [Pagefind](https://pagefind.app) builds a full-text index of every
   rendered page into `_site/pagefind/`. Skipped with a warning if pagefind isn't
   available; the site works fine without it.
4. **index** — `_site/index.html`, grouped project → session → subagents, projects
   ordered by most recent activity. Full-text search box, live list filter,
   per-machine filter, subagent visibility toggle.
5. **deploy** — `rsync --checksum --delete` to your web root (rendered pages are
   byte-stable for unchanged inputs, so unchanged pages aren't re-sent); `scp -r`
   fallback.

## Full-text search

The index page carries a Pagefind search box that searches *inside* every transcript —
prose, thinking blocks, and tool output (tool payloads are down-weighted so
conversation text ranks first). Results come with highlighted excerpts and can be
narrowed by host, project, and session kind (main vs subagent).

The search index and UI are static files served from `_site/pagefind/` on your own
origin, so the no-external-requests property still holds. It runs before `index.html`
is written, so the index page itself never appears in results. Redaction runs before
indexing, so scrubbed secrets aren't searchable either.

Pagefind is found in this order: a `pagefind` binary on `PATH`, then `npx -y
pagefind@1` (needs Node). With neither present the step is skipped — nothing breaks,
you just don't get the search box. Note the search runs client-side over a chunked
index, so only the chunks a query touches are downloaded.

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

## Index structure

The index is a three-level hierarchy: **project → session → subagent runs.**

Projects are keyed by `(host, working directory)` and ordered by most recent activity.
Within a project, sessions are newest-first. Subagent runs are nested under the session
that spawned them, **collapsed behind a toggle**, in a dashed, quieter style — they're
rarely what you're looking for.

The `Subagents:` selector switches between collapsed (default), expanded, and hidden.
`--skip-subagents` omits them from the build entirely.

Searching matches subagents too, and auto-expands any parent whose child matched.

## Sessions on more than one machine

A session id is globally unique, so the same id appearing on two hosts means **one
transcript reachable from both** — a shared or synced `~/.claude`, an rsync'd home, a
restored backup — not two sessions. Rendering it once per host duplicated whole projects
in the index.

Transcripts are deduplicated by session id plus a SHA-256 of the file. Byte-identical
copies collapse to a single row tagged `also on <host>`. Same id with *different* bytes is
a real conflict: both are kept, both are badged `id conflict`, and the build warns.

The surviving host is chosen deterministically: one whose platform is consistent with the
transcript's working directory, then the one with more turns, then alphabetically.

`--keep-duplicates` restores the old per-host behaviour.

### Host mismatch warnings

macOS homes live under `/Users`; Linux under `/home`, `/srv`, `/root`. A transcript whose
cwd can't exist on the OS it was collected from is flagged `host mismatch` in the index and
warned about at build time:

```
WARNING: 1 transcript(s) have a working directory that can't exist on the host they came from:
  0a7bdc40  host=laptop (Darwin)  cwd=/home/pfh/code/debrid-media-manager
  -> a shared/synced ~/.claude, or a stale --staging dir.
```

Usually this means a stale `--staging` directory, or a home directory shared between
machines. The host labels are not to be trusted until it's resolved.

## Where a description came from

`--explain` prints the provenance of every description and exits:

```
$ python3 cc_collect.py --explain
9508ab88  laptop   user-prompt      'I have a USB footswitch. I want to create a mac app'
0a7bdc40  web      user-prompt      'I have a USB footswitch. I want to create a mac app'
178f6f91  web      user-prompt      'I want to modify this repo. Immediate questions:...'
```

Useful when a description looks like it belongs to a different conversation. Two sessions
can legitimately share text — e.g. you paste a prompt in the wrong repo, get one reply,
quit, and start again in the right one. `--explain` tells you whether the text came from
the transcript's own prompt (`user-prompt`) or from something inherited.

Two guards keep a neighbouring conversation's text from bleeding in: a `summary` entry is
trusted only if its `leafUuid` names an entry the transcript actually contains, and prompts
are read only from entries whose `sessionId` matches the file's own.

## Subagent sessions

Claude Code marks subagent turns with `isSidechain`. A transcript whose only user turns
are sidechain turns is a subagent run with no top-level prompt.

**Descriptions.** Rather than showing "(no description)", these fall back through: the
session `summary` → the first top-level user prompt → the spawning `Task` call's
description/prompt → the first sidechain turn (the agent's own instructions) → the first
assistant prose → a labeled placeholder.

**Parent linkage.** Entries chain to each other via `parentUuid`. A sidechain's root turn
chains to an entry in the *spawning* session — a uuid the subagent's own transcript does
not contain. That dangling reference is what reattaches a subagent to its parent. Linkage
is resolved per host, so a uuid collision across machines can't cross-link them.

Two strategies, in order:

1. **Exact** — the dangling `parentUuid` resolves, on the same host, to a uuid owned by
   another session.
2. **Inferred** — same host, same project directory, and the subagent ran inside the
   parent's time window. Where several sessions qualify, the one that started most
   recently before the subagent wins. These are tagged `inferred parent` in the index,
   because the link is a guess, not a fact.

Anything still unattached is an **orphan**, surfaced under its project in an "unattached
subagent runs" group rather than silently dropped. Older transcripts that predate `uuid`
fields land here — that's expected, not a bug.

The build prints the breakdown: `2 linked by uuid, 1 inferred from project+time, 1 unattached`.

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
| `--no-search` | Skip the pagefind full-text search index |
| `--changed-only` | Exit early unless a transcript changed since the last deploy |
| `--idle-min N` | Exit early if any transcript changed in the last N minutes |
| `--skip-subagents` | Don't render subagent runs at all |
| `--keep-duplicates` | Don't collapse a session found on several hosts |
| `--explain` | Print each description's provenance, then exit |
| `--no-redact` | Publish raw. Warns loudly. Don't. |
| `--staging DIR` | Staging dir override (default `~/.local/state/cc_collect/staging`) |
| `--deploy-to H:P` | scp destination; overrides the `deploy` line in `hosts.conf` |
| `--jobs N` | Parallel host fetches |

## Scheduling

The gating flags make automation cheap: poll aggressively, and let the tool decide
whether a run is worth it.

```bash
python3 cc_collect.py --changed-only --idle-min 60
```

exits within a few seconds unless **both** hold: some transcript changed since the
last successful deploy (`--changed-only`, tracked via a stamp file in
`~/.local/state/cc_collect/`), and no transcript changed in the last hour
(`--idle-min 60` — i.e. Claude has gone quiet, so you're not publishing
mid-session). Each gate costs one cheap `find` over SSH per host.

Runs that do proceed are incremental end to end: rsync fetches only new
transcripts into the persistent staging dir, and rsync deploys only changed pages.

**macOS:** use the LaunchAgent — see `cc-collect.launchd.example.plist`, which polls
every 30 minutes from 20:00 to 23:30. Prefer launchd over cron here: agents run in
your login session, so SSH reaches the launchd ssh-agent and keychain-stored key
passphrases (add keys with `ssh-add --apple-use-keychain`); launchd also won't start
a second copy while one is still running.

```bash
cp cc-collect.launchd.example.plist ~/Library/LaunchAgents/com.example.cc-collect.plist
# edit paths, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.cc-collect.plist
launchctl kickstart gui/$(id -u)/com.example.cc-collect   # trial run now
```

**Linux:** plain cron works, same idea:

```cron
*/30 20-23 * * * cd /home/you/claude-session-renderer && /usr/bin/python3 cc_collect.py --changed-only --idle-min 60 >> publish.log 2>&1
```

## Requirements

Python 3.9+, `ssh`/`scp`, and a web server. No third-party Python packages.

Optional: [pagefind](https://pagefind.app) for full-text search — either the
standalone binary or Node (it's run via `npx`). Without it the build still succeeds,
minus the search box. `rsync` on both ends makes fetch and deploy incremental
(macOS's bundled openrsync works); without it everything falls back to `scp`.

## License

MIT
