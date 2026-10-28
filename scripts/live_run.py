"""One policy, live, start to finish: for the demo video and for anyone who wants to see real inference.

    LLM_NO_CACHE=1 ./venv/bin/python scripts/live_run.py childrens-colorado

Bypasses the response cache, so every call goes to Nebius Token Factory now: Nemotron 3 Ultra writes the policy as
logic, Nemotron 3 Super checks each clause's meaning and writes its own model, then Z3 audits it. Each call is
printed as it finishes (model, seconds, tokens, cost). Costs a few cents per policy.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.auditing import replay  # noqa: E402
from app.formalize import formalize_once  # noqa: E402
from app.law import audit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class LiveMeter(llm.Meter):
    def add(self, call):
        super().add(call)
        tag = "cached" if call.cached_result else f"{call.seconds:5.1f}s"
        print(f"  {call.label:24} {call.step:38} {tag:>7}  {call.tokens_in:>6} in / {call.tokens_out:>5} out  "
              f"${call.cost_usd:.4f}", flush=True)


def main() -> None:
    pid = sys.argv[1] if len(sys.argv) > 1 else "childrens-colorado"
    text = (ROOT / "data" / "corpus" / f"{pid}.txt").read_text()
    print(f"Policy: {pid} ({len(text):,} characters) · cache {'OFF' if os.environ.get('LLM_NO_CACHE') == '1' else 'on'}"
          f" · Nebius Token Factory\n")
    meter, t0 = LiveMeter(), time.monotonic()
    print("1. Nemotron 3 Ultra writes the policy as logic; Super checks every clause's meaning")
    a, rep = formalize_once(text, "ultra", meter)
    print(f"   kept {len(a.requires)} requirements and {len(a.rules)} rules; dropped {len(rep['dropped'])}\n")
    print("2. Nemotron 3 Super writes its own model, independently")
    b, _ = formalize_once(text, "super", meter)
    print(f"   kept {len(b.requires)} requirements and {len(b.rules)} rules\n")
    print("3. Z3 checks the encoded rules against Colorado law and 501(r)")
    z0 = time.monotonic()
    findings = audit(a)
    for f in findings:
        mark = {"proved": "PROVED", "counterexample": "COUNTEREXAMPLE"}.get(f.status, f.status.upper())
        same = ""
        if f.status == "counterexample":
            same = "  [Super's model: same case fails]" if replay(f.as_dict(), b) else "  [Super's model: disputed]"
        print(f"   {mark:15} {f.title}{same}")
        if f.status == "counterexample":
            print(f"                   {f.summary}")
    print(f"\nZ3: {time.monotonic() - z0:.2f}s · models: ${meter.cost_usd:.4f} · total {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
