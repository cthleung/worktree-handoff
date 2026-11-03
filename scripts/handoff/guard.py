"""The secrets guard: file names and file contents that never leave this computer."""

from __future__ import annotations

import fnmatch
import re

SECRET_NAMES = [
    ".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc",
    ".netrc", "*.tfvars", "*.tfstate", "credentials", "credentials.*", "*credentials*.json",
    "*secret*", "*.ovpn", ".htpasswd", "service-account*.json",
]


SECRET_CONTENT = [
    re.compile(p) for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bAKIA[0-9A-Z]{16}\b",                 # AWS access key id
        r"\bgh[pousr]_[A-Za-z0-9]{36,}",         # GitHub tokens
        r"\bgithub_pat_[A-Za-z0-9_]{50,}",
        r"\bsk-ant-[A-Za-z0-9_-]{20,}",          # Anthropic
        r"\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{32,}",  # OpenAI, OpenRouter
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",       # Slack
        r"\bAIza[0-9A-Za-z_-]{35}\b",            # Google API key
        r"\bglpat-[A-Za-z0-9_-]{20,}",           # GitLab
    )
]


def secret_hit(text: str) -> str | None:
    for pat in SECRET_CONTENT:
        m = pat.search(text)
        if m:
            return pat.pattern
    return None


def secret_name(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    for pat in SECRET_NAMES:
        if fnmatch.fnmatch(name, pat):
            return pat
    return None
