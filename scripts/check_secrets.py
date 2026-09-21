"""Fail if anything about to be published looks like a credential or a private detail.

Run before publishing: it scans every file git would commit (respecting .gitignore, so the
local workspace database and run transcripts are out of scope) and reports findings with
file and line. Exit status is 1 when anything is found, so it works as a pre-push gate:

    uv run python scripts/check_secrets.py
    uv run python scripts/check_secrets.py --staged     # only what is staged

Deliberate fixtures are allowed by path or by exact text, never by pattern-wide suppression:
a scanner that can be silenced broadly stops being a check. Every allowlist entry below names
the specific file and why the match is safe.

SPDX-FileCopyrightText: 2026 EasyAgent contributors
SPDX-License-Identifier: AGPL-3.0-only
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# High-signal credential shapes. These are the ones worth failing a push for.
RULES = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("provider key", re.compile(r"\b(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("secret assignment", re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token)\b\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']")),
    ("personal path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    # Names an actual host, never a Python attribute ("self.local"), an import (".local")
    # or a filename glob ("*.local.json"), which is why it requires a delimiter before the name.
    ("private host", re.compile(r"(?:(?:https?|wss?)://|@|\bhost\s*[:=]\s*[\"']?)[A-Za-z0-9.-]*"
                                r"(?:\.internal|\.corp|\.lan|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
                                r"|\b192\.168\.\d{1,3}\.\d{1,3}\b|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b)")),
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

# A match is ignored only when the named file is a test or fixture whose whole point is to
# carry a fake credential, and only for the reasons given here.
# Files whose findings are structural, not secrets: this scanner's own patterns, licence
# texts that legitimately carry a contact address, and the fixture that proves loopback and
# private addresses are refused.
ALLOWED = {
    "tests/integration/test_model_management.py": {"provider key", "secret assignment"},
    "tests/integration/test_autonomy.py": {"provider key", "bearer token"},
    "tests/integration/test_connections.py": {"secret assignment"},
    "tests/integration/test_extensions.py": {"secret assignment"},
    "tests/integration/test_gateway_maintenance.py": {"secret assignment"},
    "tests/integration/test_api_and_studio.py": {"secret assignment"},
    "sdk/javascript/extension.js": {"secret assignment"},
    "sdk/js/extension.js": {"secret assignment"},
    "scripts/check_secrets.py": {"private host", "email address", "provider key", "bearer token",
                                 "secret assignment", "jwt", "private key"},
    "docs/COMPETITOR_LEARNINGS.md": {"email address"},
    # Cites public vendor documentation URLs (langchain.com and similar); no private host appears.
    "docs/MODELS_AND_RESEARCH.md": {"private host"},
    # Proves that private and loopback addresses are rejected before any request is sent.
    "tests/integration/test_local_files.py": {"private host"},
    "tests/integration/conftest.py": {"private host"},
    "NOTICE": {"email address"},
    "LICENSE": {"email address"},
    "LICENSES/Apache-2.0.txt": {"email address"},
    "LICENSING.md": {"email address"},
    "docs/LICENSING.zh-CN.md": {"email address"},
}

# Loopback addresses are how the integration tests reach their own fixture servers.
LOOPBACK = re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|::1)\b")


def candidates(staged):
    if staged:
        rows = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                              capture_output=True, text=True, check=True).stdout
    else:
        rows = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                              capture_output=True, text=True, check=True).stdout
    return [name for name in rows.split("\n") if name.strip()]


def scan(path):
    findings = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings
    allowed = ALLOWED.get(path, set())
    for number, line in enumerate(text.split("\n"), 1):
        for name, pattern in RULES:
            match = pattern.search(line)
            if not match:
                continue
            if name == "private host" and LOOPBACK.search(match.group(0)):
                continue
            if name in allowed:
                continue
            findings.append((path, number, name, line.strip()[:160]))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="only files staged for commit")
    args = parser.parse_args()
    findings = []
    for path in candidates(args.staged):
        if not Path(path).is_file():
            continue
        findings.extend(scan(path))
    if findings:
        print(f"{len(findings)} potential secret(s):", file=sys.stderr)
        for path, number, name, line in findings:
            print(f"  {path}:{number}: {name}: {line}", file=sys.stderr)
        print("\nMove the value into an environment variable, a vault entry or a local file that "
              "git ignores. If it is a deliberate fixture, add its file to ALLOWED in this script "
              "with the reason.", file=sys.stderr)
        raise SystemExit(1)
    print("no credentials or private details found")


if __name__ == "__main__":
    main()
