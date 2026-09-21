# services/perception

Perception & AI Decision service — simulated camera/LiDAR sensor input,
real YOLOv8n object detection, and a retrieval + guardrails safety
recommendation. See `docs/adr/0015-perception-and-decision.md` for the
full design and, importantly, exactly what's real vs. simulated here.

**Separate from `services/api` on purpose**: its `ultralytics`
dependency (which pulls in PyTorch) is large and shouldn't be forced
onto anyone just running the core GCS stack. Entirely optional — every
other phase of this project works fully without this service running.

## Endpoints
- `GET /health`
- `GET /vehicles/{id}/perception` — runs real object detection on the
  vehicle's current simulated camera frame, plus a simulated LiDAR scan
  reduced to obstacle clusters. Returns `{"frame":, "detections": [...],
  "lidar_obstacles": [...]}`.
- `GET /vehicles/{id}/decision?armed=&battery_pct=&gps_fix_type=` —
  perception plus an advisory recommendation (`CONTINUE`/`HOLD`/
  `DIVERT`/`RTL`) from the RAG+guardrails decision layer, with its
  reasoning, the matched safety guideline, and every guardrail check
  that ran. **Never issues a command** — it's advisory only, the same
  principle `services/api/app/risk_monitor.py` already established.

## Run locally

One-time setup — installs into the same shared venv every other
service in this project already uses:
```bash
pip install -r requirements.txt
```

Then:
```bash
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8010
```

First run needs network access once to download the ~6MB YOLOv8n
model weights (cached afterward in `.models/`, gitignored — a build
artifact, not source).

On Windows, `scripts/run_perception.ps1` does the above in one command
(after the one-time `pip install`), in its own window, matching the
pattern `scripts/run_local.ps1` already uses for the core stack.

## Tests

```bash
PYTHONPATH=. pytest tests/
```

`tests/test_detection.py` runs real inference (not mocked) against the
bundled sample frames — the test that actually proves detection.py does
real object detection.
