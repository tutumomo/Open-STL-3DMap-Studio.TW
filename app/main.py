"""FastAPI server for the 3D map builder.

The server is deliberately stateless apart from its caches: the browser owns the
project JSON and posts it with every request. That makes the whole thing
restart-safe and keeps the UI the single source of truth.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Project
from .folder_dialog import pick_folder
from .ifc_import import LoadedDesign, load_design
from .paths import (
    APP_NAME,
    data_dir,
    export_dir,
    open_in_explorer,
    resource_dir,
    set_export_dir,
)
from .pipeline import area_stats, build_model, export_model, load_area, preview_geojson
from .sources import geocode

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("3dmaps")

WEB_DIR = resource_dir() / "web"
DATA = data_dir()
UPLOAD_DIR = DATA / "uploads"
PROJECT_DIR = DATA / "projects"

for d in (UPLOAD_DIR, PROJECT_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME)

_design_cache: dict[str, LoadedDesign] = {}
# Where the last build wrote to; the download endpoint only serves from there.
_last_output_dir: Path | None = None


def _safe_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "kaart")).strip("-").lower()
    return slug[:60] or "kaart"


class ProjectRequest(BaseModel):
    project: dict[str, Any]
    refresh: bool = False
    # "Opslaan als": open a folder dialog first and export there.
    choose_dir: bool = False


class ExportDirRequest(BaseModel):
    path: str


def _load_project(payload: dict[str, Any]) -> Project:
    try:
        project = Project.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Ongeldig project: {exc}") from exc
    project.id = _safe_id(project.id or project.name)
    return project


def _design_for(project: Project) -> LoadedDesign | None:
    if not project.design.filename or not project.design.enabled:
        return None
    path = UPLOAD_DIR / project.id / Path(project.design.filename).name
    if not path.exists():
        return None

    key = f"{path}|{path.stat().st_mtime_ns}"
    if key not in _design_cache:
        _design_cache.clear()
        _design_cache[key] = load_design(path)
    return _design_cache[key]


# --------------------------------------------------------------------------- #
# api
# --------------------------------------------------------------------------- #
@app.get("/api/ping")
def api_ping() -> dict:
    """Marker so a second launch can recognise an already-running instance."""
    from .appinfo import INSTANCE_MARKER
    return {"app": INSTANCE_MARKER}


@app.get("/api/appinfo")
def api_appinfo() -> dict:
    from . import appinfo
    import platform

    version = appinfo.APP_VERSION
    os_label = f"{platform.system()} {platform.release()}"
    body = (
        "\n\n---\n"
        f"App: {appinfo.APP_NAME} {version}\n"
        f"OS: {os_label}\n"
    )
    from urllib.parse import urlencode
    feedback_url = appinfo.ISSUES_NEW_URL + "?" + urlencode({
        "title": "Feedback: ",
        "body": "Beschrijf je feedback of probleem hier." + body,
    })
    return {
        "name": appinfo.APP_NAME,
        "version": version,
        "repo_url": appinfo.REPO_URL,
        "releases_url": appinfo.RELEASES_URL,
        "feedback_url": feedback_url,
    }


@app.get("/api/update-check")
def api_update_check() -> dict:
    from .updater import check_for_update
    return check_for_update()


@app.get("/api/search")
def api_search(q: str) -> dict:
    if len(q.strip()) < 2:
        return {"results": []}
    try:
        return {"results": geocode(q.strip())}
    except Exception as exc:  # noqa: BLE001 - surface the reason in the UI
        raise HTTPException(502, f"Zoeken mislukt: {exc}") from exc


@app.post("/api/area")
def api_area(req: ProjectRequest) -> dict:
    project = _load_project(req.project)
    try:
        area = load_area(project, refresh=req.refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("area fetch failed")
        raise HTTPException(502, f"Ophalen van kaartdata mislukt: {exc}") from exc

    return {
        "stats": area_stats(area, project),
        "geojson": preview_geojson(area, project),
    }


@app.post("/api/build")
def api_build(req: ProjectRequest) -> dict:
    global _last_output_dir

    project = _load_project(req.project)

    target_root = export_dir()
    if req.choose_dir:
        chosen = pick_folder(str(target_root), f"{APP_NAME} - opslaan als")
        if not chosen:
            raise HTTPException(409, "Geannuleerd.")
        target_root = Path(chosen)
        set_export_dir(target_root)

    try:
        area = load_area(project)
        design = _design_for(project)
        detailed = None
        if project.settings.detailed_buildings and project.settings.include_buildings:
            from .buildings3d import fetch_buildings_detailed
            log.info("detail mode on: fetching LoD2.2 (this is slow)")
            detailed = fetch_buildings_detailed(area.bbox_rd)
        model = build_model(area, project, design, detailed=detailed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("build failed")
        raise HTTPException(500, f"Genereren mislukt: {exc}") from exc

    if not model.bodies:
        raise HTTPException(400, "Er bleef geen geometrie over. Staat alles uitgevinkt?")

    out_dir = target_root / project.id
    try:
        files = export_model(model, out_dir, project.id,
                             bed_size_mm=project.settings.bed_size_mm)
    except OSError as exc:
        raise HTTPException(
            400, f"Kon niet schrijven naar {out_dir}: {exc}. Kies een andere map."
        ) from exc

    _last_output_dir = out_dir
    return {
        "files": files,
        "bands": model.bands,
        "stats": model.stats,
        "project_id": project.id,
        "output_dir": str(out_dir),
    }


@app.get("/api/export-dir")
def api_export_dir_get() -> dict:
    return {"path": str(export_dir())}


@app.post("/api/export-dir/pick")
def api_export_dir_pick() -> dict:
    chosen = pick_folder(str(export_dir()), f"{APP_NAME} - kies exportmap")
    if not chosen:
        return {"path": str(export_dir()), "changed": False}
    set_export_dir(Path(chosen))
    return {"path": chosen, "changed": True}


@app.post("/api/export-dir")
def api_export_dir_set(req: ExportDirRequest) -> dict:
    path = Path(req.path).expanduser()
    set_export_dir(path)
    return {"path": str(path)}


@app.post("/api/reveal")
def api_reveal() -> dict:
    target = _last_output_dir if _last_output_dir and _last_output_dir.is_dir() else export_dir()
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": open_in_explorer(target), "path": str(target)}


@app.post("/api/design/upload")
async def api_design_upload(
    project_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    pid = _safe_id(project_id)
    target_dir = UPLOAD_DIR / pid
    target_dir.mkdir(parents=True, exist_ok=True)

    name = Path(file.filename or "ontwerp.ifc").name
    target = target_dir / name
    with target.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    try:
        design = load_design(target)
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Kon bestand niet inlezen: {exc}") from exc

    _design_cache.clear()
    return {"filename": name, **design.info()}


@app.delete("/api/design")
def api_design_delete(project_id: str, filename: str) -> dict:
    path = UPLOAD_DIR / _safe_id(project_id) / Path(filename).name
    path.unlink(missing_ok=True)
    _design_cache.clear()
    return {"ok": True}


@app.get("/api/download/{project_id}/{filename}")
def api_download(project_id: str, filename: str) -> FileResponse:
    if _last_output_dir is None:
        raise HTTPException(404, "Nog niets gegenereerd")
    directory = _last_output_dir.resolve()
    path = (directory / Path(filename).name).resolve()
    if not path.is_file() or directory not in path.parents:
        raise HTTPException(404, "Bestand niet gevonden")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.get("/api/projects")
def api_projects() -> dict:
    items = []
    for path in sorted(PROJECT_DIR.glob("*.json")):
        items.append({"id": path.stem, "modified": path.stat().st_mtime})
    return {"projects": items}


@app.post("/api/projects/save")
def api_project_save(req: ProjectRequest) -> dict:
    import json

    project = _load_project(req.project)
    path = PROJECT_DIR / f"{project.id}.json"
    path.write_text(json.dumps(project.to_dict(), indent=2), encoding="utf-8")
    return {"ok": True, "id": project.id}


@app.get("/api/projects/{project_id}")
def api_project_load(project_id: str) -> dict:
    import json

    path = PROJECT_DIR / f"{_safe_id(project_id)}.json"
    if not path.exists():
        raise HTTPException(404, "Project niet gevonden")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# static
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
