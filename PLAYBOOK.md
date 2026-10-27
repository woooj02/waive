# Playbook: running a demo on this kit

Strategy (ideas, planning, judging, video, submission) lives in the workspace's `hack-*` skills. This file covers
only what is specific to the kit.

## Day one

- [ ] `./new.sh <event> <project> <port>`, then `./deploy.sh` the empty app once, so hosting is proven early.
- [ ] Put the sponsor tools in `app/tools.py` (`@tool(...)`), one function each, and the prompt in `SYSTEM` in
      `app/agent.py`.
- [ ] Write the scripted demo prompts into `EXAMPLES` in `web/index.html`, so they're one click away.

## While building

- Deploy early and often (`./deploy.sh`; tests run first). "Works on my laptop" isn't a demo.
- The tool-call trace under each answer is the demo: name tools and arguments so the trace reads well on screen.
- No `ANTHROPIC_API_KEY` means the offline mock engine. Use it for tests, never present it as live.

## Demo insurance

- [ ] Rehearse with `REPLAY=record`, then present with `REPLAY=play`: the scripted prompts answer instantly even if
      the wifi or the API is down. Say so if asked; it replays a real earlier run.
- [ ] Set `CLAUDE_EFFORT=low` if answers feel slow on stage, and rehearse at that setting.
- [ ] Record a backup screen capture of the full demo. Keep a phone hotspot ready.

## Hosting

- `./deploy.sh` gives `https://NAME.91.99.162.43.sslip.io`. Set `DOMAIN` in `deploy.env` for a real subdomain.
- `./deploy.sh --env` copies `.env` to the box; `./deploy.sh logs` follows the log; `./deploy.sh down` takes it
  offline once the event's keep-alive date has passed.
- Some networks block sslip.io (the home network redirects it). Check the URL from a phone on mobile data.
