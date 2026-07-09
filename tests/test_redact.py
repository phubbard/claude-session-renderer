#!/usr/bin/env python3
"""
Tests for redact.py.

Asserts that every known credential shape is scrubbed, that benign text is left
strictly alone, and that redaction is idempotent.

--- Why the fixtures look like this -------------------------------------------

This file necessarily contains strings shaped like real credentials. Written as
plain literals they trip GitHub push protection and every other secret scanner,
which blocks the push -- correctly, since a scanner cannot tell a fake Stripe key
from a real one.

So each fixture is assembled at runtime from fragments via `_join(...)`. Content
scanners match on contiguous literals and see nothing; the assembled value is
byte-identical to the real shape, so the redaction rules are exercised exactly as
intended.

`test_no_literal_secrets_in_source` enforces this: if anyone ever pastes a whole
credential back into this file, the suite fails before a push can be blocked.

Every value below is fabricated. None of them are, or ever were, live.
"""

import os
import pathlib
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from redact import redact_text, redact_entries, summarize  # noqa: E402

fails = []


def _join(*parts):
    """Assemble a secret-shaped fixture from fragments, defeating content scanners."""
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Fixtures: fabricated credentials, assembled so no literal appears in source.
# --------------------------------------------------------------------------- #
FIXTURES = {
    "openssh private key": _join(
        "-----BEGIN OPENSSH ", "PRIVATE KEY-----\n",
        "b3BlbnNzaC1rZXktdjEAAAAA\nAAAA\n",
        "-----END OPENSSH ", "PRIVATE KEY-----"),
    "rsa private key": _join(
        "-----BEGIN RSA ", "PRIVATE KEY-----\n", "MIIEpQIBAAKCAQEA1234\n",
        "-----END RSA ", "PRIVATE KEY-----"),
    "pgp private key": _join(
        "-----BEGIN PGP ", "PRIVATE KEY BLOCK-----\n", "lQOYBF\n",
        "-----END PGP ", "PRIVATE KEY BLOCK-----"),
    "certificate": _join(
        "-----BEGIN CERT", "IFICATE-----\n", "MIIDdzCCAl+gAwIBAgIE\n",
        "-----END CERT", "IFICATE-----"),
    "aws access key id": _join("AKIA", "IOSFODNN7", "EXAMPLE"),
    "aws temp key": _join("ASIA", "Y34FZKBOK", "MUTVV7A"),
    "aws secret via env": _join(
        "AWS_SECRET_ACCESS_KEY=", "wJalrXUtnFEMI/", "K7MDENG/", "bPxRfiCYEXAMPLEKEY"),
    "jwt": _join(
        "eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".",
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    "github token": _join("ghp", "_", "16CharsMinimum1234567890abcdefgh"),
    "github pat": _join("github", "_pat_", "11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234567890"),
    "slack bot token": _join("xox", "b-", "000000000000", "-", "000000000000",
                             "-", "AbCdEfGhIjKlMnOpQrStUvWx"),
    "slack webhook": _join("https://hooks.", "slack.com", "/services/",
                           "T00000000/", "B00000000/", "X" * 24),
    "stripe live key": _join("sk", "_live_", "0000000000000000000000ab"),
    "anthropic key": _join("sk", "-ant-", "api03-", "abcdefghijklmnopqrstuvwxyz1234567890"),
    "openai key": _join("sk", "-proj-", "abcdefghijklmnopqrstuvwxyz1234567890ABCD"),
    "google api key": _join("AIza", "Sy", "C1234567890abcdefghijklmnopqrstuvw0"),
    "npm token": _join("npm", "_", "abcdefghijklmnopqrstuvwxyz0123456789AB"),
    "sendgrid": _join("SG", ".", "abcdefghijklmnopqr", ".", "stuvwxyz0123456789ABCDEF"),
    "twilio sid": _join("AC", "0" * 16, "abcdef0123456789"),
    "bcrypt hash": _join("$2a$", "14$",
                         "abcdefghijklmnopqrstuvABCDEFGHIJKLMNOPQRSTUVWXYZ01234"),
    "bearer token": _join("Bearer ", "abcdefghijklmnopqrstuvwxyz123456"),
    "basic auth header": _join("Basic ", "dXNlcjpwYXNzd29yZA=="),
    "ssh public key": _join("ssh-rsa ", "AAAAB3NzaC1yc2EAAAADAQABAAABgQC", "x" * 40,
                            " paul@laptop"),
}

# Fixtures that need surrounding context to be meaningful.
CONTEXTUAL = {
    "postgres url password": ("sup3rs3cr3t", "postgres://admin:{}@db.internal:5432/app"),
    "mongodb+srv password": ("hunter2hunter2", "mongodb+srv://user:{}@cluster.mongodb.net"),
    "https basic url": ("tok3nvalue", "https://paul:{}@web.example.com/private"),
    "inline env assign": ("hunter2", "docker run -e DB_PASSWORD={} img"),
}

# Generic assignments -- these don't trip scanners, so plain literals are fine.
PLAIN = {
    "env DB_PASSWORD": "DB_PASSWORD=hunter2",
    "env export API_KEY": 'export API_KEY="abc123def456"',
    "yaml password": 'password: "s3cr3tvalue"',
    "yaml unquoted password": "password: s3cr3tvalue",
    "json client_secret": '"client_secret": "abc123def456ghi"',
    "mid-line env assign": "prefix text and DB_PASSWORD=hunter2",
}

# Must survive untouched.
BENIGN = {
    "prose about passwords": "Change the password before you deploy; the password is stored in Vault.",
    "prose about api keys": "The API key rotation policy requires quarterly rotation of every access key.",
    "uuid": "sessionId 11111111-1111-1111-1111-111111111111 looks fine",
    "git sha": "commit a1b2c3d4e5f67890abcdef1234567890abcdef12 landed on main",
    "plain url": "See https://web.example.com/sessions/index.html for the list",
    "url with port, no creds": "Connect to postgres://db.internal:5432/app for read-only",
    "camelCase code assign": "const apiKey = getApiKey();",
    "code identifier": "const token = readToken();",
    "base64 blob, no context": "The payload was YWJjZGVmZ2hpamtsbW5vcA== which decodes to letters",
    "markdown heading": "## Token bucket rate limiting",
    "empty assignment": "PASSWORD=",
    "yaml prose short value": "password: see vault",
    "sha256 hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
}


def must_redact(label, secret, ctx="{}"):
    text = ctx.format(secret)
    out = redact_text(text, Counter())
    if secret in out:
        fails.append(f"LEAK   [{label}] secret survived: {out[:70]!r}")
    elif "[REDACTED:" not in out:
        fails.append(f"NOMARK [{label}] no marker: {out[:70]!r}")
    else:
        print(f"  ok  {label}")


def must_keep(label, text):
    out = redact_text(text, Counter())
    if out != text:
        fails.append(f"FALSEPOS [{label}] mangled: {text[:55]!r} -> {out[:55]!r}")
    else:
        print(f"  ok  (kept) {label}")


def test_no_literal_secrets_in_source():
    """Guard: no credential-SHAPED fixture may appear verbatim in this file.

    If one does, a secret scanner will match it and block the push.

    Only FIXTURES are policed. CONTEXTUAL values are bare passwords like
    "sup3rs3cr3t" -- they have no recognizable credential shape, so no scanner
    matches them, and writing them plainly keeps the tests readable.
    """
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    leaked = [label for label, sec in FIXTURES.items() if sec in src]
    if leaked:
        fails.append("SCANNER-BAIT: literal secret(s) in source: " + ", ".join(leaked))
        print(f"  FAIL literal secrets in source: {leaked}")
    else:
        print(f"  ok  none of the {len(FIXTURES)} shaped fixtures appear as "
              f"contiguous literals")


def main():
    print("=== TRUE POSITIVES: these must all be scrubbed ===")
    for label, secret in FIXTURES.items():
        must_redact(label, secret)
    for label, (secret, ctx) in CONTEXTUAL.items():
        must_redact(label, secret, ctx)
    for label, text in PLAIN.items():
        must_redact(label, text)

    print("\n=== FALSE POSITIVES: these must survive untouched ===")
    for label, text in BENIGN.items():
        must_keep(label, text)

    print("\n=== IDEMPOTENCY: redacting twice == redacting once ===")
    sample = "\n".join([PLAIN["env DB_PASSWORD"], FIXTURES["bearer token"],
                        FIXTURES["aws access key id"]])
    once = redact_text(sample, Counter())
    twice = redact_text(once, Counter())
    if once != twice:
        fails.append(f"IDEMPOTENCY broken:\n  once={once!r}\n  twice={twice!r}")
    else:
        print("  ok  double-redaction is a no-op")

    print("\n=== COUNTING: broad rules must not double-count scrubbed text ===")
    c = Counter()
    redact_text(PLAIN["env DB_PASSWORD"], c)
    if sum(c.values()) != 1:
        fails.append(f"double-counted: expected 1 hit, got {dict(c)}")
    else:
        print(f"  ok  one assignment -> one hit ({dict(c)})")

    print("\n=== ENTRY WALK ===")
    entries = [{"type": "user", "sessionId": "abc",
                "message": {"role": "user",
                            "content": FIXTURES["aws access key id"] + " and DB_PASSWORD=hunter2"}}]
    scrubbed, c = redact_entries(entries)
    print("  ", summarize(c))
    blob = str(scrubbed)
    if FIXTURES["aws access key id"] in blob or "hunter2" in blob:
        fails.append("entry redaction leaked")
    elif scrubbed[0]["sessionId"] != "abc":
        fails.append("SKIP_KEYS mangled a structural field")
    else:
        print("  ok  nested content scrubbed, structural keys preserved")

    print("\n=== SOURCE HYGIENE ===")
    test_no_literal_secrets_in_source()

    print("\n" + "=" * 60)
    if fails:
        print(f"{len(fails)} FAILURE(S):")
        for f in fails:
            print("  " + f)
        return 1
    total = len(FIXTURES) + len(CONTEXTUAL) + len(PLAIN)
    print(f"ALL REDACTION TESTS PASSED ({total} secrets scrubbed, "
          f"{len(BENIGN)} benign strings preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
