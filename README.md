# Hackathon starter kit

A Claude agent with tools, a clean single-page UI showing the tool-call trace,
offline tests, demo replay, and one-command deploy to the Hetzner box over
HTTPS. The point is to spend the hackathon on the idea, not on setup.

## Start a project

```bash
./new.sh paypal-2026 spendguard 8101   # → ../active/paypal-2026, own venv + git repo
cd ../active/paypal-2026
cp .env.example .env                   # add ANTHROPIC_API_KEY (no key = mock engine)
./run.sh                               # http://127.0.0.1:8101
```

## What to change per hackathon

| File | What it is |
|------|------------|
| `app/tools.py` | **The tools.** Sponsor APIs, data and actions. Each is a function with `@tool(...)`. |
| `app/agent.py` | The `SYSTEM` prompt. The loop itself rarely needs changing. |
| `web/index.html` | Name, tagline and `EXAMPLES` (the scripted demo prompts) |
| `PLAYBOOK.md` | Kit-specific demo ops: deploy early, scripted prompts, replay, hosting |

Adding a tool:

```python
@tool(ticker={"type": "string", "description": "e.g. AAPL"})
def quote(ticker: str) -> dict:
    """Latest price for a US stock. Use for any question about current prices."""
    return {"ticker": ticker, "price": ...}
```

## Commands

```bash
./run.sh            # dev server with auto-reload
./run.sh test       # offline tests (mock engine), also run by deploy.sh
./deploy.sh         # tests → rsync → venv on the box → systemd + nginx + TLS
./deploy.sh --env   # same, and copy .env (API key) to the box
./deploy.sh logs    # follow the server log
./deploy.sh down    # take it offline
```

The deploy goes to `https://NAME.91.99.162.43.sslip.io` by default, with no DNS
setup. Set `DOMAIN` in `deploy.env` to use a real subdomain. Each app runs as
`hack-NAME.service`, capped at 512 MB RAM and 1 CPU, because the box is shared.

## Settings (`.env`)

| Var | Default | |
|-----|---------|-|
| `ANTHROPIC_API_KEY` | — | Without it, the app runs on the offline mock engine |
| `CLAUDE_MODEL` | `claude-opus-5-5` | |
| `CLAUDE_EFFORT` | `medium` | `low` is faster on stage, `high` for harder reasoning |
| `AGENT_MAX_STEPS` | `8` | Maximum number of tool-call rounds per request |
| `REPLAY` | off | `record` while rehearsing, `play` on stage (see `app/replay.py`) |

## Design notes

- **Server-side refusal fallback is on.** Calls go through the beta endpoint
  with `fallbacks: "default"`, so if a safety classifier declines a benign
  request, the API retries it on another model within the same call. Remove
  `betas`/`fallbacks` in `agent.py` to turn it off.
- **Tools are strict.** Arguments are guaranteed to match the schema, and a
  failing tool returns an error the model can recover from instead of
  crashing the run.
- **The trace is the demo.** Every tool call, with its input and output, is
  shown under the answer.
