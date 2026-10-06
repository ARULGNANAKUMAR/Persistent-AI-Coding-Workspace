"""
router.py
---------
Provider abstraction + failover routing.

Design:
- Provider config (name, model, base url, encrypted key) lives in SQLite.
- This module never persists project data; it only knows how to talk to
  AI providers and how to pick the best one.
- API keys are decrypted only in-memory, right before a request, and never
  logged or returned to the frontend.
"""

import time
import uuid
import requests
from datetime import datetime, timedelta

from database import db_session
from security import encrypt_secret, decrypt_secret, mask_key

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES_PER_PROVIDER = 1
COOLDOWN_MINUTES_BASE = 2


class ProviderError(Exception):
    pass


class AllProvidersFailedError(Exception):
    pass


# ---------------------------------------------------------------------------
# Provider CRUD
# ---------------------------------------------------------------------------

def add_provider(provider_name, model_name, api_key, api_base_url=None, priority=100):
    provider_id = str(uuid.uuid4())
    encrypted = encrypt_secret(api_key)
    with db_session() as conn:
        conn.execute(
            """INSERT INTO providers
               (id, provider_name, model_name, api_base_url, encrypted_api_key,
                key_last4, enabled, priority, status)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'ACTIVE')""",
            (provider_id, provider_name, model_name, api_base_url, encrypted,
             api_key[-4:], priority),
        )
    return provider_id


def list_providers() -> list:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM providers ORDER BY priority ASC, created_at ASC"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["masked_key"] = "•" * 10 + d.pop("key_last4")
        d.pop("encrypted_api_key", None)
        result.append(d)
    return result


def delete_provider(provider_id: str):
    with db_session() as conn:
        conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))


def set_provider_enabled(provider_id: str, enabled: bool):
    with db_session() as conn:
        conn.execute(
            "UPDATE providers SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if enabled else 0, provider_id),
        )


# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------

def _mark_success(conn, provider_id):
    conn.execute(
        """UPDATE providers
           SET status='ACTIVE', last_success=datetime('now'),
               failure_count=0, cooldown_until=NULL, updated_at=datetime('now')
           WHERE id = ?""",
        (provider_id,),
    )


def _mark_failure(conn, provider_id, failure_count):
    cooldown_minutes = COOLDOWN_MINUTES_BASE * (2 ** min(failure_count, 4))  # exponential backoff
    cooldown_until = (datetime.utcnow() + timedelta(minutes=cooldown_minutes)).isoformat()
    conn.execute(
        """UPDATE providers
           SET status='FAILED', last_failure=datetime('now'),
               failure_count=?, cooldown_until=?, updated_at=datetime('now')
           WHERE id = ?""",
        (failure_count, cooldown_until, provider_id),
    )


def _eligible_providers(conn):
    """Providers that are enabled and not currently in cooldown, in priority order."""
    rows = conn.execute(
        "SELECT * FROM providers WHERE enabled = 1 ORDER BY priority ASC, created_at ASC"
    ).fetchall()
    now = datetime.utcnow()
    eligible = []
    for r in rows:
        if r["cooldown_until"]:
            cooldown_until = datetime.fromisoformat(r["cooldown_until"])
            if now < cooldown_until:
                continue  # still cooling down
        eligible.append(r)
    return eligible


# ---------------------------------------------------------------------------
# Calling a provider (OpenAI-compatible chat completions by default)
# ---------------------------------------------------------------------------

def _call_openai_compatible(base_url, model, api_key, messages):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    if resp.status_code == 429:
        raise ProviderError("Rate limited")
    if resp.status_code >= 400:
        raise ProviderError(f"Provider returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


PROVIDER_CALLERS = {
    "openai_compatible": _call_openai_compatible,
    # Additional provider implementations can be registered here without
    # touching agent.py — e.g. "anthropic": _call_anthropic
}


def _call_provider(provider_row, messages):
    caller = PROVIDER_CALLERS.get(
        provider_row["provider_name"] if provider_row["provider_name"] in PROVIDER_CALLERS
        else "openai_compatible"
    )
    api_key = decrypt_secret(provider_row["encrypted_api_key"])
    base_url = provider_row["api_base_url"] or "https://api.openai.com/v1"
    return caller(base_url, provider_row["model_name"], api_key, messages)


# ---------------------------------------------------------------------------
# Public routing entry point
# ---------------------------------------------------------------------------

def route_chat_request(messages, mode="AUTO", preferred_provider_id=None) -> dict:
    """
    Send `messages` (OpenAI-style list of {role, content}) to the best
    available provider, failing over automatically.

    Returns: {"content": str, "provider_id": str, "provider_name": str, "model": str}
    """
    with db_session() as conn:
        candidates = _eligible_providers(conn)
        if mode == "MANUAL" and preferred_provider_id:
            candidates = [c for c in candidates if c["id"] == preferred_provider_id] or candidates

    if not candidates:
        raise AllProvidersFailedError(
            "No available AI provider. Please check configured providers/API keys."
        )

    last_error = None
    for provider_row in candidates:
        for attempt in range(MAX_RETRIES_PER_PROVIDER + 1):
            try:
                content = _call_provider(provider_row, messages)
                with db_session() as conn:
                    _mark_success(conn, provider_row["id"])
                return {
                    "content": content,
                    "provider_id": provider_row["id"],
                    "provider_name": provider_row["provider_name"],
                    "model": provider_row["model_name"],
                }
            except Exception as e:
                last_error = e
                time.sleep(min(2 ** attempt, 4))  # exponential backoff between retries
        # Mark failure in its own committed session so it survives even if
        # every remaining candidate also fails and we raise below.
        with db_session() as conn:
            _mark_failure(conn, provider_row["id"], provider_row["failure_count"] + 1)
        # move on to next candidate (do not keep retrying the same one)

    raise AllProvidersFailedError(
        f"No available AI provider. Please check configured providers/API keys. "
        f"Last error: {last_error}"
    )


def test_provider(provider_id: str) -> dict:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if not row:
            raise ProviderError("Provider not found")
        try:
            _call_provider(row, [{"role": "user", "content": "ping"}])
            _mark_success(conn, provider_id)
            return {"status": "ACTIVE"}
        except Exception as e:
            _mark_failure(conn, provider_id, row["failure_count"] + 1)
            return {"status": "FAILED", "error": str(e)}
