"""
app.py
------
Main Flask application. Thin route layer — business logic lives in
agent.py / router.py / memory.py / files.py / security.py.
"""

import os
import uuid
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

from database import init_db, db_session
import files
import memory
import router
import agent
from security import PathSecurityError

app = Flask(__name__, static_folder=None)
init_db()


def error_response(message, status=400):
    return jsonify({"error": message}), status


def get_project_or_404(project_id):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY last_opened DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/projects/connect", methods=["POST"])
def api_connect_project():
    data = request.get_json(force=True) or {}
    local_path = data.get("local_path", "").strip()
    name = data.get("name", "").strip()

    if not local_path or not os.path.isdir(local_path):
        return error_response("local_path must be an existing directory on this machine.")

    local_path = os.path.realpath(local_path)
    if not name:
        name = os.path.basename(local_path.rstrip(os.sep)) or local_path

    with db_session() as conn:
        existing = conn.execute(
            "SELECT * FROM projects WHERE local_path = ?", (local_path,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE projects SET last_opened = datetime('now') WHERE id = ?",
                (existing["id"],),
            )
            project = dict(existing)
        else:
            project_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO projects (id, name, local_path) VALUES (?, ?, ?)",
                (project_id, name, local_path),
            )
            project = {"id": project_id, "name": name, "local_path": local_path}

    return jsonify({
        "project": project,
        "files_count": len(files.list_files(local_path)),
        "languages": files.detect_languages(local_path),
        "git": files.detect_git(local_path),
    })


@app.route("/api/projects/<project_id>", methods=["GET"])
def api_get_project(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    return jsonify({
        "project": project,
        "tree": files.get_project_tree(project["local_path"]),
        "languages": files.detect_languages(project["local_path"]),
        "git": files.detect_git(project["local_path"]),
    })


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/files", methods=["GET"])
def api_list_files(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    return jsonify({"tree": files.get_project_tree(project["local_path"])})


@app.route("/api/projects/<project_id>/file", methods=["GET"])
def api_read_file(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    rel_path = request.args.get("path", "")
    try:
        return jsonify(files.read_file(project["local_path"], rel_path))
    except (PathSecurityError, PermissionError) as e:
        return error_response(str(e), 403)
    except FileNotFoundError as e:
        return error_response(str(e), 404)


@app.route("/api/projects/<project_id>/file", methods=["POST"])
def api_write_file(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    data = request.get_json(force=True) or {}
    rel_path = data.get("path", "")
    content = data.get("content", "")
    create_new = data.get("create", False)
    try:
        if create_new:
            files.create_file(project["local_path"], rel_path, content)
        else:
            files.write_file(project["local_path"], rel_path, content)
        return jsonify({"status": "ok", "path": rel_path})
    except (PathSecurityError, PermissionError) as e:
        return error_response(str(e), 403)
    except FileExistsError as e:
        return error_response(str(e), 409)


@app.route("/api/projects/<project_id>/file", methods=["DELETE"])
def api_delete_file(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    rel_path = request.args.get("path", "")
    try:
        files.delete_file(project["local_path"], rel_path)
        return jsonify({"status": "ok"})
    except (PathSecurityError, PermissionError) as e:
        return error_response(str(e), 403)
    except FileNotFoundError as e:
        return error_response(str(e), 404)


@app.route("/api/projects/<project_id>/search", methods=["GET"])
def api_search_files(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    query = request.args.get("q", "")
    return jsonify({"results": files.search_files(project["local_path"], query)})


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/chat", methods=["POST"])
def api_chat(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    data = request.get_json(force=True) or {}
    message = data.get("message", "").strip()
    session_id = data.get("session_id", "default")
    mode = data.get("mode", "AUTO")
    preferred_provider_id = data.get("provider_id")
    if not message:
        return error_response("message is required")

    result = agent.handle_chat_message(
        project_id=project_id,
        workspace_root=project["local_path"],
        session_id=session_id,
        user_message=message,
        mode=mode,
        preferred_provider_id=preferred_provider_id,
    )
    return jsonify(result)


@app.route("/api/projects/<project_id>/chat/approve", methods=["POST"])
def api_chat_approve(project_id):
    project = get_project_or_404(project_id)
    if not project:
        return error_response("Project not found", 404)
    data = request.get_json(force=True) or {}
    action = data.get("action")
    if not action:
        return error_response("action is required")
    result = agent.approve_pending_action(project["local_path"], project_id, action)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/memory", methods=["GET"])
def api_list_memory(project_id):
    category = request.args.get("category")
    return jsonify({"memory": memory.list_memory(project_id, category)})


@app.route("/api/projects/<project_id>/memory", methods=["POST"])
def api_save_memory(project_id):
    data = request.get_json(force=True) or {}
    memory_id = memory.save_memory(
        project_id,
        data.get("category", "important_context"),
        data.get("content", ""),
        data.get("importance", 2),
    )
    return jsonify({"id": memory_id})


@app.route("/api/projects/<project_id>/memory/<memory_id>", methods=["DELETE"])
def api_delete_memory(project_id, memory_id):
    memory.delete_memory(project_id, memory_id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@app.route("/api/providers", methods=["GET"])
def api_list_providers():
    return jsonify({"providers": router.list_providers()})


@app.route("/api/providers", methods=["POST"])
def api_add_provider():
    data = request.get_json(force=True) or {}
    required = ["provider_name", "model_name", "api_key"]
    if not all(data.get(f) for f in required):
        return error_response(f"Required fields: {', '.join(required)}")
    provider_id = router.add_provider(
        provider_name=data["provider_name"],
        model_name=data["model_name"],
        api_key=data["api_key"],
        api_base_url=data.get("api_base_url"),
        priority=data.get("priority", 100),
    )
    return jsonify({"id": provider_id})


@app.route("/api/providers/<provider_id>", methods=["DELETE"])
def api_delete_provider(provider_id):
    router.delete_provider(provider_id)
    return jsonify({"status": "ok"})


@app.route("/api/providers/<provider_id>/test", methods=["POST"])
def api_test_provider(provider_id):
    return jsonify(router.test_provider(provider_id))


@app.route("/api/providers/<provider_id>/toggle", methods=["POST"])
def api_toggle_provider(provider_id):
    data = request.get_json(force=True) or {}
    router.set_provider_enabled(provider_id, data.get("enabled", True))
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/tasks", methods=["GET"])
def api_list_tasks(project_id):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority DESC, created_at ASC",
            (project_id,),
        ).fetchall()
    return jsonify({"tasks": [dict(r) for r in rows]})


@app.route("/api/projects/<project_id>/tasks", methods=["POST"])
def api_create_task(project_id):
    data = request.get_json(force=True) or {}
    task_id = str(uuid.uuid4())
    with db_session() as conn:
        conn.execute(
            """INSERT INTO tasks (id, project_id, title, description, status, priority)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, project_id, data.get("title", ""), data.get("description", ""),
             data.get("status", "TODO"), data.get("priority", 2)),
        )
    return jsonify({"id": task_id})


@app.route("/api/projects/<project_id>/tasks/<task_id>", methods=["POST"])
def api_update_task(project_id, task_id):
    data = request.get_json(force=True) or {}
    fields, values = [], []
    for key in ("title", "description", "status", "priority"):
        if key in data:
            fields.append(f"{key} = ?")
            values.append(data[key])
    if not fields:
        return error_response("No fields to update")
    values.extend([task_id, project_id])
    with db_session() as conn:
        conn.execute(
            f"UPDATE tasks SET {', '.join(fields)}, updated_at = datetime('now') WHERE id = ? AND project_id = ?",
            values,
        )
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@app.route("/api/projects/<project_id>/activity", methods=["GET"])
def api_activity(project_id):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_logs WHERE project_id = ? ORDER BY created_at DESC LIMIT 200",
            (project_id,),
        ).fetchall()
    return jsonify({"activity": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    print("Persistent AI Coding Workspace")
    print(f"Running at: http://{host}:{port}")
    app.run(host=host, port=port, debug=os.environ.get("FLASK_ENV") == "development")
