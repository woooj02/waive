"""HTTP layer: the page, the streaming check, the Atlas, samples and the packet."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import atlas, finder, llm, pipeline
from .schema import Household

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SAMPLES = ROOT / "samples"
MAX_UPLOAD = 8_000_000
LIVE_PER_IP_PER_DAY = 12

app = FastAPI(title="Waive")
app.mount("/static", StaticFiles(directory=WEB), name="static")
app.mount("/samples", StaticFiles(directory=SAMPLES), name="samples")

_hits: dict[tuple[str, str], int] = defaultdict(int)


def _allow(request: Request) -> bool:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    key = (ip, time.strftime("%Y-%m-%d"))
    _hits[key] += 1
    return _hits[key] <= LIVE_PER_IP_PER_DAY


@app.middleware("http")
async def no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "nebius": llm.configured(), "tavily": finder.tavily_configured(),
            "atlas_policies": len(atlas.load_all()), "spent_today_usd": llm.spent_today(),
            "daily_budget_usd": llm.budget(), "models": {k: m.id for k, m in llm.MODELS.items()}}


@app.get("/api/samples")
def samples():
    return json.loads((SAMPLES / "index.json").read_text())


@app.get("/api/atlas")
def atlas_list():
    out = []
    for e in atlas.load_all():
        p = e["program"]
        tiers = [t for t in p.get("tiers", []) if t.get("verified")]
        free = max((t["max_fpl"] for t in tiers if t["discount_pct"] >= 100), default=None)
        disc = max((t["max_fpl"] for t in tiers), default=None)
        out.append({"id": e["id"], "hospital": e["hospital"], "system": e.get("system", ""),
                    "hospitals": e.get("hospitals", []), "city": e.get("city", ""), "nonprofit": e.get("nonprofit"),
                    "free_to_fpl": free, "discount_to_fpl": disc,
                    "asset_test": p["asset_test"]["applies"] if p["asset_test"].get("verified") else None,
                    "residency": p["residency"]["kind"] if p["residency"].get("verified") else "unknown",
                    "insured_ok": p["insured"]["eligible"] if p["insured"].get("verified") else None,
                    "providers": p["providers"]["scope"] if p["providers"].get("verified") else "unknown",
                    "dropped": len(p.get("dropped", [])),
                    "disagreements": len(e.get("report", {}).get("disagreements", {})),
                    "source_url": e["source_url"], "compiled": e.get("compiled", "")})
    return out


@app.get("/api/atlas/{policy_id}")
def atlas_get(policy_id: str):
    e = atlas.get(policy_id)
    if not e:
        raise HTTPException(404, "No such policy in the Atlas")
    return e


AUDIT = ROOT / "audit"


def _audit_doc(policy_id: str) -> dict:
    p = AUDIT / f"{policy_id}.json"
    if not p.resolve().is_relative_to(AUDIT.resolve()) or not p.exists():
        raise HTTPException(404, "No audit for this policy")
    return json.loads(p.read_text())


@app.get("/api/audit")
def audit_index():
    p = AUDIT / "index.json"
    return json.loads(p.read_text()) if p.exists() else {"policies": []}


@app.get("/api/audit/{policy_id}")
def audit_get(policy_id: str):
    return _audit_doc(policy_id)


@app.post("/api/audit/{policy_id}/prove")
def audit_prove(policy_id: str):
    """Re-run every check with Z3 right now (local, free, milliseconds) on the stored formal model."""
    from .auditing import reverify
    from .formal import PolicyModel
    doc = _audit_doc(policy_id)
    summary = None
    sp = ROOT / "formal" / f"{policy_id}-summary.json"
    if doc.get("summary_source") and sp.exists():
        summary = PolicyModel(**json.loads(sp.read_text())["model"])
    t0 = time.monotonic()
    # Re-verify the published findings (proofs re-run, each counterexample's exact case re-checked); never swap
    # a reviewed finding for a different but equally valid one.
    findings = reverify(policy_id, doc, summary)
    return {"id": policy_id, "seconds": round(time.monotonic() - t0, 4), "z3": __import__("z3").get_version_string(),
            "findings": findings}


@app.get("/api/formal-eval")
def formal_eval():
    p, runs = ROOT / "formal-eval.json", ROOT / "stability-eval.json"
    out = json.loads(p.read_text()) if p.exists() else {"status": "not run yet"}
    if runs.exists():  # repeated runs: stability, and how often a confirmed finding matches our reference
        out["stability"] = json.loads(runs.read_text())["summary"]
    return out


@app.get("/api/eval")
def eval_results():
    p = ROOT / "atlas-eval.json"
    return json.loads(p.read_text()) if p.exists() else {"status": "not run yet"}


@app.post("/api/check")
async def check(request: Request, household: str = Form(...), sample: str = Form(""), typed: str = Form(""),
                file: UploadFile | None = File(None)):
    try:
        hh = Household.model_validate_json(household)
        typed_fields = json.loads(typed) if typed else None
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, f"Check the household details: {exc}") from exc

    upload = None
    if sample:
        meta = next((s for s in json.loads((SAMPLES / "index.json").read_text()) if s["id"] == sample), None)
        if not meta:
            raise HTTPException(404, "Unknown sample")
        name = meta["files"][0]
        upload = ((SAMPLES / name).read_bytes(), name)
    elif file is not None:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "That file is over 8 MB. Try a photo or a smaller PDF.")
        if not data:
            raise HTTPException(422, "The file is empty.")
        upload = (data, file.filename or "bill")
    elif not typed_fields:
        raise HTTPException(422, "Add a bill photo, pick a sample, or type the bill details.")

    live_ok = _allow(request) if not sample else True

    def stream():
        try:
            for event in pipeline.run(hh, upload=upload, typed=typed_fields, allow_live_compile=live_ok,
                                      sample=bool(sample)):
                yield json.dumps(event, default=str) + "\n"
        except Exception as exc:  # last resort: tell the page what broke instead of hanging
            yield json.dumps({"type": "fatal", "detail": f"{type(exc).__name__}: {exc}"[:300]}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/packet")
async def packet(request: Request):
    from . import packet as packetmod

    body = await request.json()
    try:
        pdf = packetmod.build(body)
    except (KeyError, ValueError, ValidationError) as exc:
        raise HTTPException(422, f"Can't build the packet: {exc}") from exc
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="waive-packet.pdf"'})
