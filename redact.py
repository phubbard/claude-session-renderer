#!/usr/bin/env python3
"""
redact.py — Scrub credentials out of Claude Code transcripts before publishing.

Redaction is ON BY DEFAULT in publish_session.py and cc_collect.py. This module
does the work and reports what it found.

Design notes:
  * Rules are ordered most-specific first. Structural secrets (private key blocks)
    are removed before token-shaped rules, which run before generic
    KEY=value / "key": "value" rules.
  * Replacements are idempotent: a value already containing a [REDACTED:...]
    marker is never re-redacted, so running twice is a no-op.
  * We deliberately do NOT do generic entropy detection — it produces too many
    false positives on hashes, UUIDs, base64 blobs, and minified code.

This is a safety net, NOT a guarantee. A secret with no recognizable shape
(a bare password echoed by a command, a customer's PII) will pass straight
through. Always skim before publishing.
"""

import re
from collections import Counter

MARKER = "[REDACTED:{}]"

# Keys whose values are structural, never secret-bearing. Skipping them avoids
# nonsense matches on uuids/timestamps.
SKIP_KEYS = {
    "uuid", "parentUuid", "sessionId", "timestamp", "type", "role",
    "version", "tool_use_id", "id", "isMeta", "isSidechain", "userType",
}


def _rule(name, pattern, flags=0):
    return (name, re.compile(pattern, flags))


# Each rule either matches the whole secret, or names a group `v` = the part to
# replace (keeping surrounding context like `Bearer ` or `PASSWORD=` intact).
RULES = [
    # ---- structural blocks ------------------------------------------------
    _rule("private_key",
          r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
          r".*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----", re.S),
    _rule("ssh_public_key", r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]{40,}"),
    _rule("certificate",
          r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S),

    # ---- token shapes (unambiguous prefixes) ------------------------------
    _rule("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    _rule("aws_access_key_id",
          r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    _rule("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    _rule("slack_token", r"\bxox[abprse]-[A-Za-z0-9-]{10,}\b"),
    _rule("slack_webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_-]{10,}"),
    _rule("stripe_key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    _rule("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    _rule("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    _rule("google_api_key", r"\bAIza[0-9A-Za-z_-]{35,}"),
    _rule("npm_token", r"\bnpm_[A-Za-z0-9]{36,}"),
    _rule("pypi_token", r"\bpypi-[A-Za-z0-9_-]{16,}\b"),
    _rule("sendgrid_key", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    _rule("twilio_sid", r"\bAC[0-9a-fA-F]{32}\b"),
    _rule("bcrypt_hash", r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),

    # ---- auth headers -----------------------------------------------------
    _rule("bearer_token",
          r"(?P<k>\bBearer\s+)(?P<v>[A-Za-z0-9._~+/-]{16,}={0,2})", re.I),
    _rule("basic_auth_header",
          r"(?P<k>\bBasic\s+)(?P<v>[A-Za-z0-9+/]{12,}={0,2})", re.I),

    # ---- credentials embedded in URLs ------------------------------------
    # scheme://user:PASSWORD@host   (covers postgres, mysql, mongodb, redis, amqp, https)
    _rule("url_credentials",
          r"(?P<k>\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]{1,64}:)(?P<v>[^\s@/]{1,128})(?P<t>@)"),

    # ---- generic assignments (last: broadest) ----------------------------
    # Uppercase env-var convention, matched ANYWHERE on a line (inline in shell
    # commands, docker run -e, etc). Case-SENSITIVE on purpose: this must not
    # fire on camelCase source code like `const apiKey = getApiKey();`.
    _rule("env_assignment",
          r"(?<![A-Za-z0-9_])(?P<k>(?:export[^\S\n]+)?[A-Z0-9_]*"
          r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_KEY|PRIVATE_KEY|"
          r"CREDENTIALS?|AUTH_?KEY|SESSION_KEY)[A-Z0-9_]*"
          r"[^\S\n]*=[^\S\n]*[\"']?)(?P<v>[^\s\"'#]{4,})"),
    # Same idea but line-anchored and case-insensitive, for .env / dotfiles
    # where `api_key = abc123` sits alone on a line.
    _rule("env_assignment_line",
          r"(?P<k>^[^\S\n]*(?:export[^\S\n]+)?[A-Za-z0-9_]*"
          r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_KEY|PRIVATE_KEY|"
          r"CREDENTIALS?|AUTH_?KEY|SESSION_KEY)[A-Za-z0-9_]*"
          r"[^\S\n]*=[^\S\n]*[\"']?)(?P<v>[^\s\"'#]{4,})",
          re.I | re.M),
    # "password": "hunter2"  /  client_secret = "abc"   (quoted value)
    _rule("kv_secret",
          r"(?P<k>[\"']?\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
          r"client[_-]?secret|private[_-]?key|auth[_-]?token)\b[\"']?"
          r"[^\S\n]*[:=][^\S\n]*[\"'])(?P<v>[^\"'\n]{4,})(?P<t>[\"'])",
          re.I),
    # password: hunter2   (unquoted YAML). Colon only -- NOT `=`, which would
    # swallow ordinary assignments in source code. Value must be a bare token
    # of 6+ chars, so prose like `password: see vault` is left alone.
    _rule("kv_secret_unquoted",
          r"(?P<k>\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
          r"client[_-]?secret|auth[_-]?token)\b"
          r"[^\S\n]*:[^\S\n]*)(?P<v>[^\s\"'#,;{}]{6,})",
          re.I),
]


def _replacement(name, m):
    """Rebuild the match with the secret portion replaced."""
    gd = m.groupdict()
    if "v" not in gd or gd["v"] is None:
        return MARKER.format(name)
    # Idempotency guard: never re-redact.
    if "[REDACTED:" in gd["v"]:
        return m.group(0)
    pre = gd.get("k") or ""
    post = gd.get("t") or ""
    return f"{pre}{MARKER.format(name)}{post}"


def redact_text(text, counter=None):
    """Redact a single string. Returns the scrubbed string."""
    if not text:
        return text
    if counter is None:
        counter = Counter()
    for name, rx in RULES:
        def _sub(m, _n=name):
            rep = _replacement(_n, m)
            # Only count a hit if we actually changed something. Later, broader
            # rules routinely re-match text an earlier rule already scrubbed;
            # the idempotency guard returns it unchanged, and it must not be
            # double-counted.
            if rep != m.group(0):
                counter[_n] += 1
            return rep
        text = rx.sub(_sub, text)
    return text


def redact_obj(obj, counter):
    """Recursively redact every string value in a parsed JSONL entry."""
    if isinstance(obj, str):
        return redact_text(obj, counter)
    if isinstance(obj, list):
        return [redact_obj(x, counter) for x in obj]
    if isinstance(obj, dict):
        return {
            k: (v if k in SKIP_KEYS else redact_obj(v, counter))
            for k, v in obj.items()
        }
    return obj


def redact_entries(entries):
    """Redact a whole transcript. Returns (entries, Counter of rule -> hits)."""
    counter = Counter()
    scrubbed = [redact_obj(e, counter) for e in entries]
    return scrubbed, counter


def summarize(counter) -> str:
    """'3 secrets redacted (aws_access_key_id x2, jwt x1)' — or '' if clean."""
    total = sum(counter.values())
    if not total:
        return ""
    bits = ", ".join(f"{k} x{v}" for k, v in counter.most_common())
    noun = "secret" if total == 1 else "secrets"
    return f"{total} {noun} redacted ({bits})"


if __name__ == "__main__":
    import sys
    data = sys.stdin.read()
    c = Counter()
    out = redact_text(data, c)
    sys.stdout.write(out)
    if sum(c.values()):
        print(f"\n--- {summarize(c)}", file=sys.stderr)
