"""DocJSON viewer / editor routes.

Extracted from ``viz/server.py`` during Phase 4 split.  Route bodies copied
verbatim from the original file; module-globals live in ``viz.server``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from axiom_graph.docjson.api import get_doc_diff
from axiom_graph.index import db

logger = logging.getLogger(__name__)

docs_router = APIRouter()


class _DocSaveRequest(BaseModel):
    doc: dict


class _DocCreateRequest(BaseModel):
    title: str
    doc_id: str | None = None
    subdirectory: str | None = None  # e.g. "prds" or "design/sub"


class _DocImportRequest(BaseModel):
    doc: dict
    filename: str | None = None


class _MkdirRequest(BaseModel):
    path: str  # relative to docs/, e.g. "prds/sub"


class _DocMoveRequest(BaseModel):
    destination: str | None = None
    filename: str | None = None


@docs_router.get("/api/docs")
def list_docs_api() -> dict:
    """List all indexed DocJSON documents with section counts."""
    from axiom_graph.viz import server

    docs = db.list_docs(server._db())
    items = []
    for d in docs:
        sections = db.get_doc_sections(server._db(), d["id"])
        items.append(
            {
                "id": d["id"],
                "title": d["title"],
                "tags": json.loads(d["tags"]) if d.get("tags") else [],
                "file_path": d.get("file_path", ""),
                "section_count": len(sections),
            }
        )
    return {"docs": items}


@docs_router.get("/api/docs/sections")
def query_sections_api(tags: str, match: str = "any") -> dict:
    """Cross-document section query by tag."""
    from axiom_graph.viz import server

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if not tag_list:
        raise HTTPException(status_code=422, detail="tags parameter is required")

    match_all = match.lower() == "all"
    rows = db.query_doc_sections_by_tags(server._db(), tag_list, match_all=match_all)

    grouped: dict[str, dict] = {}
    for sec in rows:
        doc_id = sec["doc_id"]
        if doc_id not in grouped:
            grouped[doc_id] = {
                "doc_id": doc_id,
                "doc_title": sec.get("doc_title", ""),
                "sections": [],
            }
        grouped[doc_id]["sections"].append(
            {
                "id": sec["id"],
                "heading": sec["heading"],
                "content": sec.get("content") or "",
                "level": sec.get("level") or 2,
                "tags": json.loads(sec["tags"]) if sec.get("tags") else [],
                "position": sec["position"],
            }
        )

    return {
        "tags_queried": tag_list,
        "match": "all" if match_all else "any",
        "doc_count": len(grouped),
        "section_count": sum(len(g["sections"]) for g in grouped.values()),
        "docs": list(grouped.values()),
    }


@docs_router.get("/api/docs/{doc_id:path}/raw")
def get_doc_raw(doc_id: str) -> dict:
    """Return the raw JSON content of a DocJSON file for editing."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    doc_node = db.get_node(server._db(), doc_id)
    if doc_node is None:
        raise HTTPException(status_code=404, detail=f"Doc not found: {doc_id!r}")

    json_path = server._PROJECT_ROOT / doc_node.location
    if not json_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {doc_node.location}")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read doc: {exc}") from exc

    return {"doc": data, "file_path": doc_node.location}


@docs_router.get("/api/docs/{doc_id:path}/render")
def render_doc_api(doc_id: str) -> dict:
    """Render a DocJSON document as Markdown with linked node info."""
    from axiom_graph.viz import server

    doc_node = db.get_node(server._db(), doc_id)
    if doc_node is None:
        raise HTTPException(status_code=404, detail=f"Doc not found: {doc_id!r}")

    sections = db.get_doc_sections(server._db(), doc_id)

    from axiom_graph.docjson.render_agent import _render_doc_markdown  # noqa: PLC0415

    markdown = _render_doc_markdown(server._db(), doc_id, doc_node.title, sections)

    structured_sections = []
    for sec in sections:
        sec_id = sec["id"]
        linked_nodes = []
        with db._connect(server._db()) as conn:
            rows = conn.execute(
                """
                SELECT e.to_id, n.level_0, n.level_1, n.node_type
                FROM edges e
                LEFT JOIN nodes n ON n.id = e.to_id
                WHERE e.from_id = ?
                  AND e.edge_type = 'documents'
                  AND e.to_id != ?
                ORDER BY e.to_id
                """,
                (sec_id, doc_id),
            ).fetchall()
            linked_nodes = [
                {
                    "node_id": r["to_id"],
                    "summary": r["level_1"] or r["level_0"] or "",
                    "node_type": r["node_type"] or "",
                }
                for r in rows
            ]

        structured_sections.append(
            {
                "id": sec["id"],
                "heading": sec["heading"],
                "content": sec.get("content") or "",
                "level": sec.get("level") or 2,
                "tags": json.loads(sec["tags"]) if sec.get("tags") else [],
                "linked_nodes": linked_nodes,
                "parent_id": sec.get("parent_id"),
                "depth": sec.get("depth", 0),
            }
        )

    return {
        "doc_id": doc_id,
        "title": doc_node.title,
        "markdown": markdown,
        "sections": structured_sections,
    }


@docs_router.get("/api/docs/{doc_id:path}/diff")
def get_doc_diff_endpoint(doc_id: str, sha: str | None = None) -> dict:
    """Return section-level diff of a DocJSON file against a baseline commit."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    result = get_doc_diff(server._db(), server._PROJECT_ROOT, doc_id, baseline_sha=sha)
    if "error" in result:
        return result
    return {
        "doc_id": doc_id,
        **result,
    }


@docs_router.post("/api/docs/rescan")
def rescan_docs() -> dict:
    """Re-scan the docs/ directory to pick up new or changed JSON files."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    roots = [r for r in server._docs_roots() if r.is_dir()]
    if not roots:
        return {"ok": True, "scanned": 0, "message": "No configured docs roots found"}

    from axiom_graph.docjson import parse as json_doc_scanner  # noqa: PLC0415

    scanned = 0
    renames: dict[str, str] = {}
    try:
        nodes: list = []
        edges: list = []
        doc_recs: list = []
        sec_recs: list = []
        for docs_dir in roots:
            _n, _e, _d, _s, _ = json_doc_scanner.scan_json_docs(docs_dir, server._PROJECT_ROOT, server._PROJECT_ID)
            nodes.extend(_n)
            edges.extend(_e)
            doc_recs.extend(_d)
            sec_recs.extend(_s)

        new_id_by_path: dict[str, str] = {}
        for rec in doc_recs:
            new_id_by_path[rec["file_path"]] = rec["id"]

        with db._connect(server._db()) as conn:
            for file_path, new_doc_id in new_id_by_path.items():
                old_ids = db.get_doc_ids_by_filepath(server._db(), file_path)
                for old_id in old_ids:
                    if old_id != new_doc_id:
                        db.record_doc_rename(server._db(), old_id, new_doc_id, file_path)
                        renames[old_id] = new_doc_id
                        db.delete_doc_by_id(conn, old_id)

        for node in nodes:
            db.upsert_node(server._db(), node)
        for edge in edges:
            db.upsert_edge(server._db(), edge)
        with db._connect(server._db()) as conn:
            for rec in doc_recs:
                db.upsert_doc(conn, rec)
            for rec in sec_recs:
                db.upsert_doc_section(conn, rec)
        scanned = len(doc_recs)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"ok": True, "scanned": scanned, "renames": renames}


@docs_router.post("/api/docs/import")
def import_doc_json(body: _DocImportRequest) -> dict:
    """Import raw JSON as a new DocJSON file."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    data = body.doc

    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="JSON must be an object")

    title = data.get("title", "").strip() if isinstance(data.get("title"), str) else ""
    if not title:
        raise HTTPException(status_code=422, detail='JSON must have a non-empty "title" field')

    raw_id = data.get("id", "").strip() if isinstance(data.get("id"), str) else ""
    if not raw_id:
        raw_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not raw_id:
        raise HTTPException(status_code=422, detail="Could not derive a filename slug")

    if "sections" not in data or not isinstance(data["sections"], list):
        data["sections"] = []

    for i, sec in enumerate(data["sections"]):
        if not isinstance(sec, dict):
            continue
        if not sec.get("id"):
            heading = sec.get("heading", f"section-{i + 1}")
            sec["id"] = re.sub(r"[^a-z0-9]+", "-", str(heading).lower()).strip("-") or f"section-{i + 1}"
        if not sec.get("heading"):
            sec["heading"] = sec["id"].replace("-", " ").title()
        if "content" not in sec:
            sec["content"] = ""
        if "links" not in sec:
            sec["links"] = []

    if "tags" not in data:
        data["tags"] = []

    data.pop("id", None)

    filename = body.filename.strip() if body.filename else ""
    if not filename:
        filename = f"{raw_id}.json"
    if not filename.endswith(".json"):
        filename += ".json"

    docs_dir = server._primary_docs_root()
    docs_dir.mkdir(parents=True, exist_ok=True)
    json_path = docs_dir / filename

    if json_path.exists():
        try:
            _display = json_path.relative_to(server._PROJECT_ROOT).as_posix()
        except ValueError:
            _display = json_path.as_posix()
        raise HTTPException(status_code=409, detail=f"File already exists: {_display}")

    try:
        json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        from axiom_graph.docjson import parse as json_doc_scanner  # noqa: PLC0415

        nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(
            json_path, server._PROJECT_ROOT, server._PROJECT_ID, docs_dir=docs_dir
        )
        for node in nodes:
            db.upsert_node(server._db(), node)
        for edge in edges:
            db.upsert_edge(server._db(), edge)
        with db._connect(server._db()) as conn:
            for rec in doc_recs:
                db.upsert_doc(conn, rec)
            for rec in sec_recs:
                db.upsert_doc_section(conn, rec)

        rel = json_path.relative_to(server._PROJECT_ROOT).as_posix()
        try:
            raw_id = json_path.relative_to(docs_dir).as_posix().removesuffix(".json").replace("/", ".")
        except ValueError:
            raw_id = rel.removesuffix(".json").replace("/", ".")
            if raw_id.startswith("docs."):
                raw_id = raw_id[5:]
        doc_id = f"{server._PROJECT_ID}::docs.{raw_id}"
        return {"ok": True, "doc_id": doc_id, "file_path": rel}
    except Exception as exc:
        if json_path.exists():
            json_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@docs_router.post("/api/docs")
def create_doc(body: _DocCreateRequest) -> dict:
    """Create a new DocJSON file in docs/ and index it."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title must not be empty")

    raw_id = body.doc_id.strip() if body.doc_id else ""
    if not raw_id:
        raw_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not raw_id:
        raise HTTPException(status_code=422, detail="Could not derive a doc id")

    primary_root = server._primary_docs_root()
    target_docs_root = primary_root
    subdir_tail = ""
    if body.subdirectory:
        subdir = body.subdirectory.strip("/").strip()
        if subdir:
            subdir_posix = subdir.replace("\\", "/")
            matched = False
            for root_rel in server._docs_root_rels():
                _rr = root_rel.rstrip("/")
                if _rr and (subdir_posix == _rr or subdir_posix.startswith(_rr + "/")):
                    root_path = Path(root_rel)
                    target_docs_root = root_path if root_path.is_absolute() else (server._PROJECT_ROOT / root_path)
                    subdir_tail = subdir_posix[len(_rr) :].lstrip("/")
                    matched = True
                    break
            if not matched:
                subdir_tail = subdir_posix
    docs_dir = target_docs_root if not subdir_tail else (target_docs_root / subdir_tail)
    docs_dir.mkdir(parents=True, exist_ok=True)
    json_path = docs_dir / f"{raw_id}.json"

    if json_path.exists():
        try:
            rel_display = json_path.relative_to(server._PROJECT_ROOT).as_posix()
        except ValueError:
            rel_display = json_path.as_posix()
        raise HTTPException(status_code=409, detail=f"File already exists: {rel_display}")

    data = {
        "title": title,
        "tags": [],
        "sections": [
            {
                "id": "introduction",
                "heading": "Introduction",
                "content": "",
                "level": 2,
                "links": [],
            }
        ],
    }

    try:
        json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        from axiom_graph.docjson import parse as json_doc_scanner  # noqa: PLC0415

        nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(
            json_path, server._PROJECT_ROOT, server._PROJECT_ID, docs_dir=target_docs_root
        )
        for node in nodes:
            db.upsert_node(server._db(), node)
        for edge in edges:
            db.upsert_edge(server._db(), edge)
        with db._connect(server._db()) as conn:
            for rec in doc_recs:
                db.upsert_doc(conn, rec)
            for rec in sec_recs:
                db.upsert_doc_section(conn, rec)

        rel = json_path.relative_to(server._PROJECT_ROOT).as_posix()
        try:
            raw_rel_id = json_path.relative_to(target_docs_root).as_posix().removesuffix(".json").replace("/", ".")
        except ValueError:
            raw_rel_id = rel.removesuffix(".json").replace("/", ".")
            if raw_rel_id.startswith("docs."):
                raw_rel_id = raw_rel_id[5:]
        doc_id = f"{server._PROJECT_ID}::docs.{raw_rel_id}"
        return {"ok": True, "doc_id": doc_id, "file_path": rel}
    except Exception as exc:
        if json_path.exists():
            json_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@docs_router.post("/api/docs/mkdir")
def mkdir_docs(body: _MkdirRequest) -> dict:
    """Create a subdirectory under docs/."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    rel = body.path.strip().strip("/")
    if not rel:
        raise HTTPException(status_code=422, detail="Path must not be empty")

    for segment in rel.split("/"):
        if not re.match(r"^[a-zA-Z0-9_-]+$", segment):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid folder name: {segment!r}. Only letters, digits, dashes and underscores allowed.",
            )

    rel_posix = rel.replace("\\", "/")
    target: Path | None = None
    display_rel: str = ""
    for root_rel in server._docs_root_rels():
        _rr = root_rel.rstrip("/")
        if _rr and (rel_posix == _rr or rel_posix.startswith(_rr + "/")):
            root_path = Path(root_rel)
            root_abs = root_path if root_path.is_absolute() else (server._PROJECT_ROOT / root_path)
            tail = rel_posix[len(_rr) :].lstrip("/")
            target = root_abs / tail if tail else root_abs
            display_rel = rel_posix
            break
    if target is None:
        primary = server._primary_docs_root()
        target = primary / rel_posix
        try:
            display_rel = target.relative_to(server._PROJECT_ROOT).as_posix()
        except ValueError:
            display_rel = target.as_posix()

    if target.exists():
        return {"ok": True, "path": display_rel, "created": False}

    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"ok": True, "path": display_rel, "created": True}


@docs_router.get("/api/docs/subdirs")
def list_doc_subdirs() -> dict:
    """List every subdirectory under any configured docs root."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    roots = server._docs_roots()
    if not roots:
        return {"dirs": ["docs"]}

    dirs: list[str] = []
    seen: set[str] = set()

    def _add(rel_str: str) -> None:
        if rel_str not in seen:
            seen.add(rel_str)
            dirs.append(rel_str)

    for root_abs in roots:
        try:
            root_rel = root_abs.relative_to(server._PROJECT_ROOT).as_posix()
        except ValueError:
            root_rel = root_abs.as_posix()
        _add(root_rel)
        if root_abs.is_dir():
            for p in sorted(root_abs.rglob("*")):
                if p.is_dir():
                    try:
                        rel = p.relative_to(server._PROJECT_ROOT).as_posix()
                    except ValueError:
                        rel = p.as_posix()
                    _add(rel)
    return {"dirs": dirs}


@docs_router.post("/api/docs/{doc_id:path}/move")
def move_doc_endpoint(doc_id: str, body: _DocMoveRequest) -> dict:
    """Move, rename, or both a DocJSON file."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    doc_node = db.get_node(server._db(), doc_id)
    if doc_node is None:
        raise HTTPException(status_code=404, detail=f"Doc not found: {doc_id!r}")

    old_path = server._PROJECT_ROOT / doc_node.location
    if not old_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {doc_node.location}")

    source_docs_root: Path | None = None
    for r in server._docs_roots():
        try:
            old_path.relative_to(r)
            source_docs_root = r
            break
        except ValueError:
            continue
    if source_docs_root is None:
        raise HTTPException(
            status_code=400,
            detail=f"Doc is not under any configured docs root (location={doc_node.location})",
        )

    docs_dir = source_docs_root

    try:
        old_rel = old_path.relative_to(docs_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Doc is not under the matched docs root") from exc

    current_folder = old_rel.parent.as_posix()
    current_stem = old_rel.stem

    dest_docs_root = source_docs_root
    if body.destination is not None:
        dest_raw = body.destination.strip().strip("/")
        dest_posix = dest_raw.replace("\\", "/")
        matched = False
        for root_rel in server._docs_root_rels():
            _rr = root_rel.rstrip("/")
            if _rr and (dest_posix == _rr or dest_posix.startswith(_rr + "/")):
                root_path = Path(root_rel)
                dest_docs_root = root_path if root_path.is_absolute() else (server._PROJECT_ROOT / root_path)
                dest_raw = dest_posix[len(_rr) :].lstrip("/")
                matched = True
                break
        if not matched:
            dest_raw = dest_posix
        new_folder = dest_raw
    else:
        new_folder = current_folder if current_folder != "." else ""

    if body.filename is not None:
        raw_name = body.filename.strip()
        if not raw_name:
            raise HTTPException(status_code=422, detail="Filename must not be empty")
        sanitized = re.sub(r"[^a-z0-9-]", "-", raw_name.lower()).strip("-")
        if not sanitized:
            raise HTTPException(status_code=422, detail=f"Invalid filename: {raw_name!r}")
        new_stem = sanitized
    else:
        new_stem = current_stem

    if new_folder:
        new_json_path = dest_docs_root / new_folder / f"{new_stem}.json"
    else:
        new_json_path = dest_docs_root / f"{new_stem}.json"

    if old_path.resolve() == new_json_path.resolve():
        return {"ok": True, "old_id": doc_id, "new_id": doc_id, "new_path": doc_node.location, "noop": True}

    if new_json_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Target already exists: {new_json_path.relative_to(server._PROJECT_ROOT).as_posix()}",
        )

    new_json_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(old_path), str(new_json_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to move file: {exc}") from exc

    new_rel = new_json_path.relative_to(server._PROJECT_ROOT).as_posix()
    try:
        raw_id = new_json_path.relative_to(dest_docs_root).as_posix().removesuffix(".json").replace("/", ".")
    except ValueError:
        raw_id = new_rel.removesuffix(".json").replace("/", ".")
        if raw_id.startswith("docs."):
            raw_id = raw_id[5:]
    new_doc_id = f"{server._PROJECT_ID}::docs.{raw_id}"

    try:
        db.move_doc(server._db(), doc_id, new_doc_id, new_rel)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DB migration failed: {exc}") from exc

    try:
        from axiom_graph.docjson import parse as json_doc_scanner  # noqa: PLC0415

        nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(
            new_json_path, server._PROJECT_ROOT, server._PROJECT_ID, docs_dir=dest_docs_root
        )
        for node in nodes:
            db.upsert_node(server._db(), node)
        for edge in edges:
            db.upsert_edge(server._db(), edge)
        with db._connect(server._db()) as conn:
            for rec in doc_recs:
                db.upsert_doc(conn, rec)
            for rec in sec_recs:
                db.upsert_doc_section(conn, rec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Re-scan failed: {exc}") from exc

    return {"ok": True, "old_id": doc_id, "new_id": new_doc_id, "new_path": new_rel}


@docs_router.put("/api/docs/{doc_id:path}")
def save_doc(doc_id: str, body: _DocSaveRequest) -> dict:
    """Save edited DocJSON back to disk and re-index."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    doc_node = db.get_node(server._db(), doc_id)
    if doc_node is None:
        raise HTTPException(status_code=404, detail=f"Doc not found: {doc_id!r}")

    json_path = server._PROJECT_ROOT / doc_node.location
    data = body.doc

    for key in ("title", "sections"):
        if key not in data:
            raise HTTPException(status_code=422, detail=f"Missing required key: {key}")

    data.pop("id", None)

    try:
        json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        from axiom_graph.docjson import parse as json_doc_scanner  # noqa: PLC0415
        from axiom_graph.index.builder import _matched_docs_dir as _mdd  # noqa: PLC0415

        matched_root = _mdd(json_path, server._PROJECT_ROOT)
        nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(
            json_path, server._PROJECT_ROOT, server._PROJECT_ID, docs_dir=matched_root
        )
        for node in nodes:
            db.upsert_node(server._db(), node)
        for edge in edges:
            db.upsert_edge(server._db(), edge)
        with db._connect(server._db()) as conn:
            for rec in doc_recs:
                db.upsert_doc(conn, rec)
            for rec in sec_recs:
                db.upsert_doc_section(conn, rec)

        return {"ok": True, "sections_saved": len(sec_recs)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
