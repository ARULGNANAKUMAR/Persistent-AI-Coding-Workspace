"""
files.py
--------
All filesystem operations for a connected project workspace.
Every function takes `workspace_root` and treats it as the security boundary.
"""

import os
import shutil
import tempfile

from security import safe_resolve, is_blocked_file, IGNORED_DIR_NAMES, PathSecurityError

MAX_FILE_SIZE_BYTES = 512 * 1024      # 512 KB per file sent to AI context
MAX_FILES_LISTED = 5000
MAX_DIR_DEPTH = 12

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md",
    ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".sql",
    ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php",
    ".xml", ".gitignore", ".env.example",
}


def _is_ignored_dir(dirname: str) -> bool:
    return dirname in IGNORED_DIR_NAMES or dirname.startswith(".")


def get_project_tree(workspace_root: str) -> dict:
    """Build a nested dict representing the project's directory tree."""
    root = os.path.realpath(workspace_root)

    def walk(path, depth):
        node = {"name": os.path.basename(path) or path, "type": "directory", "children": []}
        if depth > MAX_DIR_DEPTH:
            return node
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return node
        for entry in entries:
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                if _is_ignored_dir(entry):
                    continue
                node["children"].append(walk(full, depth + 1))
            else:
                if is_blocked_file(entry):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                node["children"].append({"name": entry, "type": "file", "size": size})
        return node

    return walk(root, 0)


def list_files(workspace_root: str) -> list:
    """Flat list of relative file paths, respecting ignore rules and limits."""
    root = os.path.realpath(workspace_root)
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_ignored_dir(d)]
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth > MAX_DIR_DEPTH:
            dirnames[:] = []
            continue
        for f in filenames:
            if is_blocked_file(f):
                continue
            rel_path = f if rel_dir == "." else os.path.join(rel_dir, f)
            results.append(rel_path.replace(os.sep, "/"))
            if len(results) >= MAX_FILES_LISTED:
                return results
    return results


def _is_probably_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(1024)
        return b"\x00" in chunk
    except OSError:
        return True


def read_file(workspace_root: str, relative_path: str) -> dict:
    full_path = safe_resolve(workspace_root, relative_path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"File not found: {relative_path}")
    if os.path.basename(full_path) and is_blocked_file(os.path.basename(full_path)):
        raise PermissionError(f"Access to this file type is blocked: {relative_path}")
    if _is_probably_binary(full_path):
        return {"path": relative_path, "binary": True, "content": None}

    size = os.path.getsize(full_path)
    if size > MAX_FILE_SIZE_BYTES:
        return {
            "path": relative_path,
            "binary": False,
            "truncated": True,
            "content": open(full_path, "r", errors="replace").read(MAX_FILE_SIZE_BYTES),
        }

    with open(full_path, "r", errors="replace") as fh:
        content = fh.read()
    return {"path": relative_path, "binary": False, "truncated": False, "content": content}


def write_file(workspace_root: str, relative_path: str, content: str) -> None:
    """Atomic write: write to a temp file in the same dir, then replace."""
    full_path = safe_resolve(workspace_root, relative_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    dir_name = os.path.dirname(full_path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(content)
        shutil.move(tmp_path, full_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def create_file(workspace_root: str, relative_path: str, content: str = "") -> None:
    full_path = safe_resolve(workspace_root, relative_path)
    if os.path.exists(full_path):
        raise FileExistsError(f"File already exists: {relative_path}")
    write_file(workspace_root, relative_path, content)


def delete_file(workspace_root: str, relative_path: str) -> None:
    full_path = safe_resolve(workspace_root, relative_path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"File not found: {relative_path}")
    os.remove(full_path)


def rename_file(workspace_root: str, old_relative_path: str, new_relative_path: str) -> None:
    old_full = safe_resolve(workspace_root, old_relative_path)
    new_full = safe_resolve(workspace_root, new_relative_path)
    if not os.path.isfile(old_full):
        raise FileNotFoundError(f"File not found: {old_relative_path}")
    if os.path.exists(new_full):
        raise FileExistsError(f"Target already exists: {new_relative_path}")
    os.makedirs(os.path.dirname(new_full), exist_ok=True)
    shutil.move(old_full, new_full)


def search_files(workspace_root: str, query: str, max_results: int = 50) -> list:
    """Naive substring search across filenames and small text files."""
    query_lower = query.lower()
    matches = []
    for rel_path in list_files(workspace_root):
        if query_lower in rel_path.lower():
            matches.append({"path": rel_path, "match_type": "filename"})
            if len(matches) >= max_results:
                return matches

    for rel_path in list_files(workspace_root):
        ext = os.path.splitext(rel_path)[1]
        if ext not in TEXT_EXTENSIONS:
            continue
        try:
            info = read_file(workspace_root, rel_path)
        except (PathSecurityError, PermissionError, FileNotFoundError):
            continue
        if info["binary"] or not info["content"]:
            continue
        if query_lower in info["content"].lower():
            matches.append({"path": rel_path, "match_type": "content"})
            if len(matches) >= max_results:
                return matches
    return matches


def detect_languages(workspace_root: str) -> list:
    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript", ".jsx": "JavaScript", ".html": "HTML",
        ".css": "CSS", ".java": "Java", ".go": "Go", ".rs": "Rust",
        ".rb": "Ruby", ".php": "PHP", ".c": "C", ".cpp": "C++",
    }
    found = set()
    for rel_path in list_files(workspace_root):
        ext = os.path.splitext(rel_path)[1]
        if ext in ext_map:
            found.add(ext_map[ext])
    return sorted(found)


def detect_git(workspace_root: str) -> bool:
    return os.path.isdir(os.path.join(workspace_root, ".git"))
