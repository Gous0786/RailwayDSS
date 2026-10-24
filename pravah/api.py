"""
PRAVAH service: corridor data, a shared simulation clock, and a live feed.

Run:
    uvicorn pravah.api:app --port 8000
    # then open http://localhost:8000/

Endpoints
    GET  /api/health          service and data status
    GET  /api/corridor        stations, polyline and chainage mapping
    GET  /api/state?t=        train states at one instant (polling fallback)
    WS   /ws/live             pushed state, plus clock control

The clock lives on the server, not in the browser, so every connected client
sees the same instant. Control messages are broadcast to all of them:

    {"cmd": "play"} | {"cmd": "pause"}
    {"cmd": "seek",  "t": 21600}
    {"cmd": "speed", "speed": 300}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .corridor import Corridor
from .replay import ReplaySource, SimClock

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

TICK_HZ = 4.0            # pushes per second; the clock interpolates between them

app = FastAPI(title="PRAVAH", version="0.1.0")


class Hub:
    """Shared clock, data source and connected clients."""

    def __init__(self) -> None:
        self.corridor: Corridor | None = None
        self.source: ReplaySource | None = None
        self.clock: SimClock | None = None
        self.clients: set[WebSocket] = set()
        self.error: str | None = None
        self._task: asyncio.Task | None = None

    def load(self) -> None:
        try:
            self.corridor = Corridor.load()
            self.source = ReplaySource.load()
            start = max(self.source.span[0], 6 * 3600)
            self.clock = SimClock(start, self.source.span)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def frame(self) -> dict:
        assert self.clock and self.source
        return {"type": "state", **self.clock.snapshot(),
                "trains": self.source.state_at(self.clock.t)}

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        payload = json.dumps(msg, separators=(",", ":"))
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def pump(self) -> None:
        """Push state while anyone is watching."""
        interval = 1.0 / TICK_HZ
        while True:
            try:
                if self.clients and self.clock:
                    await self.broadcast(self.frame())
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.pump())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


hub = Hub()

_SECTION = {}


def _graph():
    """Section graph, built once. Loading it costs a second or so."""
    if "g" not in _SECTION:
        try:
            from .section import SectionGraph
            _SECTION["g"] = SectionGraph.load()
        except Exception:  # noqa: BLE001
            _SECTION["g"] = None
    return _SECTION["g"]


@app.on_event("startup")
async def _startup() -> None:
    hub.load()
    hub.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await hub.stop()


@app.get("/api/health")
async def health() -> JSONResponse:
    ok = hub.error is None and hub.source is not None
    return JSONResponse({
        "status": "ok" if ok else "error",
        "error": hub.error,
        "corridor": hub.corridor.name if hub.corridor else None,
        "stations": len(hub.corridor.stations) if hub.corridor else 0,
        "services": len(hub.source.paths) if hub.source else 0,
        "day": hub.source.day if hub.source else None,
        "feed": hub.source.kind if hub.source else None,
        "clients": len(hub.clients),
    }, status_code=200 if ok else 503)


@app.get("/api/corridor")
async def corridor() -> JSONResponse:
    if not hub.corridor:
        return JSONResponse({"error": hub.error or "not loaded"}, status_code=503)
    c = hub.corridor
    return JSONResponse({
        "corridor": c.name, "official_km": c.official_km,
        "attribution": c.attribution,
        "stations": [{"code": s.code, "name": s.name, "cum_km": s.cum_km,
                      "lat": s.lat, "lon": s.lon} for s in c.stations],
        "polyline": [[round(lon, 6), round(lat, 6)] for lat, lon in c._pts],
        "cum_osm_km": c._cum,
        "control_points": [list(p) for p in c._ctrl],
    })


@app.get("/api/paths")
async def paths() -> JSONResponse:
    """Whole-day train paths as chainage over time.

    The live feed carries only the current instant. A train graph needs the
    complete path of every service, so it is served once and then kept in
    step by the clock on the socket.
    """
    if not hub.source:
        return JSONResponse({"error": hub.error or "not loaded"}, status_code=503)
    s = hub.source
    return JSONResponse({
        "corridor": s.corridor, "day": s.day, "kind": s.kind,
        "time_span": s.span,
        "trains": [{"id": p.id, "type": p.type, "dir": p.dir, "pts": p.pts}
                   for p in s.paths],
    })


@app.post("/api/optimise")
def optimise(body: dict | None = None) -> JSONResponse:
    """Plan freight through a window and report what it costs.

    Deliberately a sync def: CP-SAT blocks for tens of seconds, so FastAPI
    runs this in a worker thread rather than stalling the event loop and the
    live feed along with it.

    Body (all optional):
        day        "2025-08-01", else the busiest
        from_min   window start, default 360
        to_min     window end, default 720
        limit_s    seconds per lexicographic stage, default 20
        tolerance  minutes a scheduled service may move, default 0
    """
    body = body or {}
    try:
        from .demand import DemandBook
        from .explain import explain
        from .optimise import Optimiser, OptimiserConfig
        from .plan import Plan as PlanT
        from .plan import plan_to_paths
        from .timetable import scheduled_plan
        from .verify import Verifier, VerifierConfig
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"optimiser unavailable: {e}"}, status_code=503)

    if not hub.corridor:
        return JSONResponse({"error": hub.error or "not loaded"}, status_code=503)

    graph = _graph()
    if graph is None:
        return JSONResponse({"error": "section graph unavailable"}, status_code=503)

    a = float(body.get("from_min", 360.0))
    b = float(body.get("to_min", 720.0))
    limit = float(body.get("limit_s", 20.0))
    tolerance = float(body.get("tolerance", 0.0))

    book = DemandBook.load()
    day = book.day(body["day"]) if body.get("day") else book.busiest()
    window = day.between(a, b)
    fixed = scheduled_plan(graph)

    cfg = OptimiserConfig(horizon_start_min=max(0.0, a - 30.0),
                          horizon_end_min=b + 180.0,
                          time_limit_s=limit,
                          service_tolerance_min=tolerance)
    plan, report = Optimiser(graph, cfg).solve(window, fixed)

    vcfg = VerifierConfig.load()
    vcfg.assume_unknown_line_fits = True
    vcfg.service_tolerance_min = tolerance
    baseline = {str(v) for v in Verifier(graph, vcfg).verify(fixed).errors}
    combined = PlanT(trains=plan.trains + fixed.trains, horizon=plan.horizon)
    checked = Verifier(graph, vcfg).verify(combined)
    introduced = [v for v in checked.errors if str(v) not in baseline]

    reasons = explain(graph, plan, fixed, vcfg, demand=window)

    return JSONResponse({
        "day": day.day,
        "window": [a, b],
        "offered": report.offered,
        "dispatched": report.dispatched,
        "seconds": round(report.seconds, 1),
        "stages": [{"name": s.name, "status": s.status,
                    "objective": s.objective, "gap_pct": s.gap_pct,
                    "seconds": round(s.seconds, 1)} for s in report.stages],
        "paths": plan_to_paths(graph, plan),
        "violations": [{"kind": v.kind.value, "severity": v.severity.value,
                        "detail": v.detail, "trains": list(v.trains),
                        "resource": v.resource, "at": v.at}
                       for v in introduced],
        "kpis": checked.kpis.to_dict(),
        "explain": {k: v.to_dict() for k, v in reasons.items()},
    })


@app.get("/api/state")
async def state(t: float | None = None) -> JSONResponse:
    """Polling fallback for clients that cannot hold a socket open."""
    if not hub.source or not hub.clock:
        return JSONResponse({"error": hub.error or "not loaded"}, status_code=503)
    at = hub.clock.t if t is None else float(t)
    return JSONResponse({"t": round(at, 1), "trains": hub.source.state_at(at)})


@app.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    if not hub.source or not hub.clock:
        await ws.send_text(json.dumps({"type": "error",
                                       "message": hub.error or "not loaded"}))
        await ws.close()
        return

    hub.clients.add(ws)
    try:
        await ws.send_text(json.dumps({
            "type": "hello", "corridor": hub.source.corridor,
            "day": hub.source.day, "feed": hub.source.kind,
            "services": len(hub.source.paths), **hub.clock.snapshot()},
            separators=(",", ":")))
        await ws.send_text(json.dumps(hub.frame(), separators=(",", ":")))

        while True:
            msg = json.loads(await ws.receive_text())
            cmd = msg.get("cmd")
            if cmd == "play":
                hub.clock.play()
            elif cmd == "pause":
                hub.clock.pause()
            elif cmd == "seek":
                hub.clock.seek(msg.get("t", hub.clock.t))
            elif cmd == "speed":
                hub.clock.set_speed(msg.get("speed", 300))
            else:
                continue
            # control is shared: tell everyone, not just the sender
            await hub.broadcast(hub.frame())
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    finally:
        hub.clients.discard(ws)


# Static viewer and data, mounted last so /api and /ws win.
app.mount("/data", StaticFiles(directory=ROOT / "data"), name="data")
app.mount("/web", StaticFiles(directory=WEB, html=True), name="web")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
