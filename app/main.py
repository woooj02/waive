"""HTTP layer: one page, one agent endpoint, one health check."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, replay, tools

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="hackathon-app")
app.mount("/static", StaticFiles(directory=WEB), name="static")


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "engine": agent.engine(),
        "model": agent.MODEL,
        "replay": replay.mode() or "off",
        "tools": sorted(tools.TOOLS),
    }


@app.post("/api/run")
def run(req: RunRequest):
    cached = replay.get(req.prompt)
    if cached:
        return cached
    try:
        result = agent.run(req.prompt)
    except Exception as exc:
        # Say what broke. A blank 500 during judging is the worst outcome.
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    replay.save(req.prompt, result)
    return result
