# services/perception

Perception & AI Decision service — simulated camera/LiDAR sensor input
(or a real local webcam, see below), real YOLOv8n object detection, and
a retrieval + guardrails safety recommendation. See
`docs/adr/0015-perception-and-decision.md` for the full design and,
importantly, exactly what's real vs. simulated here.

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

- `GET /geo/inspect?lat=&lon=&radius_m=100` — a REAL image of that
  location from an openly licensed source (OpenAerialMap aerial imagery
  where it exists, else Copernicus Sentinel-2 at 10 m), analysed: the
  aerial YOLOv8n-OBB model where the resolution allows, land cover, and
  OpenStreetMap features (labelled as map data). Records provider,
  capture date, resolution, licence, attribution and the SHA-256 of the
  exact pixels analysed. 404 (with what each source said) if no image —
  nothing is substituted. Retrieved imagery, never presented as live
  drone footage. See `docs/adr/0022-geographic-imagery.md`. Optional
  overrides: `OAM_API_URL`, `STAC_API_URL`, `OVERPASS_URL`,
  `NOMINATIM_URL`. Needs internet access to those services.

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

### Use a real webcam instead of sample images

Off by default — set `PERCEPTION_USE_WEBCAM=1` before starting the
service to capture genuinely live frames from the local machine's
webcam (device 0) instead of cycling through `sample_frames/`:
```powershell
$env:PERCEPTION_USE_WEBCAM = "1"
powershell -ExecutionPolicy Bypass -File scripts\run_perception.ps1
```
Requires a camera attached to the machine running this service and
`opencv-python` installed (already in `requirements.txt`). Each
**Detect objects**/**Get recommendation** call in the UI captures a
fresh frame from the camera at that moment and runs real YOLOv8
detection on it.

## Tests

```bash
PYTHONPATH=. pytest tests/
```

`tests/test_detection.py` runs real inference (not mocked) against the
bundled sample frames — the test that actually proves detection.py does
real object detection.
