"""
memory.py
---------
Persistent, project-scoped memory. Keyed by project_id, NEVER by API key
or provider — memory must survive credential/model changes untouched.
"""

import re
import uuid

from database import db_session

VALID_CATEGORIES = {
    "project_info", "architecture", "decision", "requirement",
    "constraint", "preference", "bug", "solution", "task",
    "technology", "important_context",
}


def save_memory(project_id: str, category: str, content: str, importance: int = 2) -> str:
    if category not in VALID_CATEGORIES:
        category = "important_context"
    importance = max(1, min(5, int(importance)))
    memory_id = str(uuid.uuid4())
    with db_session() as conn:
        conn.execute(
            """INSERT INTO memories (id, project_id, category, content, importance)
               VALUES (?, ?, ?, ?, ?)""",
            (memory_id, project_id, category, content, importance),
        )
    return memory_id


def list_memory(project_id: str, category: str = None) -> list:
    with db_session() as conn:
        if category:
            rows = conn.execute(
                """SELECT * FROM memories WHERE project_id = ? AND category = ?
                   ORDER BY importance DESC, updated_at DESC""",
                (project_id, category),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM memories WHERE project_id = ?
                   ORDER BY importance DESC, updated_at DESC""",
                (project_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_memory(project_id: str, memory_id: str) -> None:
    with db_session() as conn:
        conn.execute(
            "DELETE FROM memories WHERE id = ? AND project_id = ?",
            (memory_id, project_id),
        )


def get_context_memory(project_id: str, limit: int = 25) -> list:
    """
    Memory to inject into AI context for a request: critical/permanent
    items always included, plus the most recently relevant others.
    """
    with db_session() as conn:
        rows = conn.execute(
            """SELECT * FROM memories WHERE project_id = ?
               ORDER BY importance DESC, updated_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Lightweight extraction heuristics
# ---------------------------------------------------------------------------
# A full implementation would ask the AI model itself to propose structured
# memory entries (see agent.py's "memory" field in the response schema).
# These heuristics are a cheap, deterministic fallback / pre-filter so we
# don't need a model call just to catch obvious statements.

_DECISION_PATTERNS = [
    (re.compile(r"(?i)\buse\s+(flask|django|fastapi|express)\b.*\b(and|with)\b.*\b(sqlite|postgres|mysql|mongodb)\b"), "decision"),
    (re.compile(r"(?i)\bwe(?:'ll| will)? use\b"), "decision"),
    (re.compile(r"(?i)\bdo not change\b|\bmust remain unchanged\b|\bnever\b.*\b(change|modify|remove)\b"), "constraint"),
    (re.compile(r"(?i)\bmust\b|\brequire[ds]?\b"), "requirement"),
    (re.compile(r"(?i)\bbug\b|\bissue\b|\berror\b"), "bug"),
    (re.compile(r"(?i)\bfixed\b|\bresolved\b|\bworkaround\b"), "solution"),
    (re.compile(r"(?i)\bprefer\b|\bi like\b|\bi want\b"), "preference"),
]


def extract_candidate_memories(user_message: str) -> list:
    """
    Best-effort extraction of memory-worthy statements from a user message.
    Returns a list of {category, content, importance} dicts. This is a
    pre-filter — the agent should also let the AI model propose entries,
    and importance should be reviewed before treating something as
    critical/permanent (5).
    """
    candidates = []
    for pattern, category in _DECISION_PATTERNS:
        if pattern.search(user_message):
            importance = 4 if category in ("constraint", "decision") else 2
            candidates.append({
                "category": category,
                "content": user_message.strip(),
                "importance": importance,
            })
            break  # one classification per message is enough for the heuristic layer
    return candidates
