"""
security.py
------------
Everything that keeps this app from doing something dangerous:

- API key encryption at rest (Fernet / AES via `cryptography`)
- Workspace path-traversal protection
- Command allowlist / denylist for terminal execution
- Basic secret detection so we don't ship credentials to an AI provider
"""

import os
import re
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

# ---------------------------------------------------------------------------
# API key encryption
# ---------------------------------------------------------------------------

def _get_fernet() -> Fernet:
    """
    Derive a Fernet key from APP_MASTER_KEY (env var).
    APP_MASTER_KEY can be any string; we hash it to a valid 32-byte key.
    """
    master = os.environ.get("APP_MASTER_KEY")
    if not master or master == "CHANGE_ME":
        raise RuntimeError(
            "APP_MASTER_KEY is not set. Copy .env.example to .env and set a real secret."
        )
    digest = hashlib.sha256(master.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Could not decrypt stored credential (bad master key or corrupted data).")


def mask_key(plaintext: str) -> str:
    """Return a display-safe masked version, e.g. ••••••••••ABCD"""
    if not plaintext:
        return ""
    tail = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return "•" * 10 + tail


# ---------------------------------------------------------------------------
# Workspace / path safety
# ---------------------------------------------------------------------------

class PathSecurityError(Exception):
    pass


def safe_resolve(workspace_root: str, relative_path: str) -> str:
    """
    Resolve `relative_path` against `workspace_root` and guarantee the
    result stays inside the workspace. Raises PathSecurityError otherwise.
    """
    if relative_path is None:
        raise PathSecurityError("No path provided.")

    # Reject absolute paths and obvious traversal attempts outright.
    if os.path.isabs(relative_path):
        raise PathSecurityError("Absolute paths are not allowed.")

    root = os.path.realpath(workspace_root)
    candidate = os.path.realpath(os.path.join(root, relative_path))

    # Ensure candidate is root itself or strictly inside root.
    if candidate != root and not candidate.startswith(root + os.sep):
        raise PathSecurityError(f"Path escapes workspace: {relative_path}")

    return candidate


IGNORED_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".cache", ".idea", ".vscode",
}

BLOCKED_FILE_PATTERNS = [
    re.compile(r"^\.env($|\..*)"),
    re.compile(r".*\.pem$"),
    re.compile(r".*\.key$"),
    re.compile(r"^credentials\..*"),
    re.compile(r"^secrets\..*"),
]


def is_blocked_file(filename: str) -> bool:
    return any(p.match(filename) for p in BLOCKED_FILE_PATTERNS)


# ---------------------------------------------------------------------------
# Secret detection (best-effort, before sending file content to an AI provider)
# ---------------------------------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)secret\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)password\s*[=:]\s*['\"]?\S{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                       # OpenAI-style
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                    # Google-style
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)token\s*[=:]\s*['\"]?[A-Za-z0-9_\-\.]{16,}"),
]


def contains_likely_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    redacted = text
    for p in SECRET_PATTERNS:
        redacted = p.sub("[REDACTED]", redacted)
    return redacted


# ---------------------------------------------------------------------------
# Terminal command safety
# ---------------------------------------------------------------------------

ALLOWED_COMMAND_PREFIXES = [
    "python", "python3", "pip", "pip3",
    "pytest", "npm", "npx", "node",
    "git status", "git diff", "git log", "git branch",
]

DANGEROUS_PATTERNS = [
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bshutdown\b"),
    re.compile(r"(?i)\bformat\b"),
    re.compile(r"(?i)\bmkfs\b"),
    re.compile(r"(?i)\bdel\s+/[sf]\b"),
    re.compile(r"(?i)\bdd\s+if="),
    re.compile(r"(?i)\bnetwork\s*scan"),
    re.compile(r"(?i)\bnmap\b"),
    re.compile(r"(?i)\bcurl\b.*(\||>)"),
    re.compile(r"(?i)\bwget\b.*(\||>)"),
    re.compile(r"(?i)\bpush\b"),
    re.compile(r"[;&|`$]"),   # shell chaining / substitution
]


class CommandNotAllowedError(Exception):
    pass


def classify_command(command: str) -> str:
    """
    Returns one of: 'allowed', 'needs_confirmation', 'blocked'
    """
    command = command.strip()
    if not command:
        return "blocked"

    if any(p.search(command) for p in DANGEROUS_PATTERNS):
        return "blocked"

    if any(command.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
        return "allowed"

    # Unknown command: require explicit user confirmation rather than
    # auto-blocking, so legitimate-but-unlisted commands aren't a dead end.
    return "needs_confirmation"
