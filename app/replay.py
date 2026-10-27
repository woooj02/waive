"""Demo insurance. Record good runs, and replay them if the network dies on stage.

REPLAY=record  saves every successful run under data/replays/
REPLAY=play    serves a saved run when the exact prompt was recorded, and
               falls through to a live run otherwise
(unset)        always runs live

Rehearse the demo with REPLAY=record. On stage, run with REPLAY=play and the
scripted prompts come back instantly even on venue wifi.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DIR = Path(os.environ.get("REPLAY_DIR", Path(__file__).resolve().parent.parent / "data" / "replays"))


def mode() -> str:
    return os.environ.get("REPLAY", "").lower()


def _path(prompt: str) -> Path:
    key = hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:16]
    return DIR / f"{key}.json"


def get(prompt: str) -> dict | None:
    if mode() != "play":
        return None
    p = _path(prompt)
    if not p.exists():
        return None
    result = json.loads(p.read_text())
    result["replayed"] = True
    return result


def save(prompt: str, result: dict) -> None:
    if mode() != "record" or result.get("engine") != "claude":
        return
    DIR.mkdir(parents=True, exist_ok=True)
    _path(prompt).write_text(json.dumps({"prompt": prompt, **result}, indent=2, default=str))
