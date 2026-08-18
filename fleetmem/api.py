"""HTTP + WebSocket API and the demo UI.

Health is deliberately honest: /healthz reports which backends are REALLY in use, so a
judge (or an operator) can see at a glance whether the vectors came from Bedrock or the
local fallback, and whether the memory layer is Cloud or local.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .db import DB
from .errors import MemoryBackendError
from .sim import Warehouse
from .aws import STORE

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fleetmem.api")

app = FastAPI(title="FleetMem", version="0.1.0")
WEB = Path(__file__).resolve().parent.parent / "web"
warehouse: Warehouse | None = None

# three.js is vendored, not pulled from a CDN: the demo must work offline and must not
# depend on a third party staying up for the whole judging window.
app.mount("/vendor", StaticFiles(directory=str(WEB / "vendor")), name="vendor")


class Lesson(BaseModel):
    robot_id: str = "R1"
    lesson: str
    location: str | None = None


class RaceRequest(BaseModel):
    resource: str = "dock-3"
    robots: list[str] = ["R1", "R2"]


class TaskRequest(BaseModel):
    robot_id: str
    task: str = "deliver pallet"
    candidates: list[str] | None = None


@app.on_event("startup")
def _startup():
    global warehouse
    log.info("config: %s", config.describe())
    DB.apply_schema()
    warehouse = Warehouse()
    warehouse.start()
    log.info("warehouse simulation started")


@app.get("/", response_class=HTMLResponse)
def index():
    """3D warehouse view (default). The 2D canvas remains available at /2d."""
    return (WEB / "index3d.html").read_text()


@app.get("/2d", response_class=HTMLResponse)
def index_2d():
    return (WEB / "index.html").read_text()


def _store_status() -> dict:
    try:
        return STORE.status()
    except Exception as exc:            # never let a probe take down the health endpoint
        return {"enabled": False, "reason": f"probe failed: {type(exc).__name__}"}


@app.get("/healthz")
def healthz():
    """Reports real backend state. Never returns 200 for a broken database."""
    from .embeddings import get_embedder
    from .agent import active_reasoner_name
    try:
        db_health = DB.health()
    except MemoryBackendError as exc:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": str(exc), "config": config.describe()})
    return {
        "ok": True,
        "database": db_health,
        "embeddings_provider": get_embedder().name,
        "reasoning_provider": active_reasoner_name(),
        "artifact_store": _store_status(),
        "config": config.describe(),
    }


@app.get("/api/state")
def state():
    return warehouse.snapshot()


@app.post("/api/race")
def race(req: RaceRequest):
    """Two agents, one dock, genuinely concurrent. The core demo."""
    if len(req.robots) < 2:
        raise HTTPException(400, "need at least two robots to race")
    results = warehouse.race(req.resource, tuple(req.robots[:2]))
    return {"resource": req.resource, "results": results,
            "holder": warehouse.memory.holder_of(req.resource)}


@app.post("/api/task")
def task(req: TaskRequest):
    return warehouse.assign(req.robot_id, req.task, req.candidates)


@app.post("/api/remember")
def remember(item: Lesson):
    row = warehouse.memory.remember(item.robot_id, item.lesson, location=item.location)
    warehouse.emit("memory", f"{item.robot_id} remembered: {item.lesson}", item.robot_id)
    return {"id": str(row["id"]), "created_at": str(row["created_at"])}


@app.get("/api/recall")
def recall(q: str, limit: int = 5):
    hits = warehouse.memory.recall(q, limit=limit)
    return {"query": q, "hits": [
        {"robot_id": h["robot_id"], "lesson": h["lesson"], "location": h["location"],
         "distance": round(float(h["distance"]), 4), "provider": h["provider"]}
        for h in hits]}


@app.get("/api/claims")
def claims():
    return {"claims": [
        {**c, "claimed_at": str(c["claimed_at"])} for c in warehouse.memory.live_claims()]}


@app.get("/api/events")
def events(limit: int = 30):
    rows = warehouse.memory.recent_events(limit)
    return {"events": [{**r, "created_at": str(r["created_at"])} for r in rows]}


@app.post("/api/reset")
def reset():
    warehouse.release_all()
    return {"ok": True}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    try:
        while True:
            await sock.send_json(warehouse.snapshot())
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("websocket closed: %s", exc)
