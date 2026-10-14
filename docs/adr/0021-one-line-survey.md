# ADR-0021: One-line survey (CBBA Phase F)

Status: accepted (built on request, October 2026)

## Context

Phases A-E work, but only as separate pieces: you clicked drones and
places onto the map by hand, planned, replayed, flew. Searching a city
only moved the map. The perception service (YOLOv8) had its own box and
no link to the flight. The operator asked for one flow:

1. search any place on the map;
2. type one line, e.g. "3 drones survey this area", press Go;
3. the area is covered with survey points automatically;
4. the drones split them with CBBA, and the map shows them talking;
5. they fly (Phase E playback);
6. each point shows what the camera saw, plus a summary at the end.

## Decision

Join the existing parts. Nothing new where something already did the job
(hard rule 3).

| Step | Reused | New |
| --- | --- | --- |
| Read the line | `app/mission/goal_parser.py` (Groq if `GROQ_API_KEY`, else rules; scope refusal). Split into `interpret_goal()` (shared) + `parse_goal()` (unchanged behaviour). A few everyday intent words added: check, monitor, observe, look over/around/at, see what. | -- |
| Where to look | the map area on screen | `app/survey.py::survey_box`: the middle of the view, each side clamped to 100 m - 1.2 km |
| Survey points | `app/planning/coverage.py::split_into_cells` (centres = points S01, S02 …) | 4 points per drone, 6-24 |
| Drones, fence | -- | `build_survey_scenario`: D1…Dn (1-8, default 3), camera, 100% battery, launch spots spread along the area's south edge; fence = area + 60 m |
| Who goes where | CBBA + safety gate (`allocate`, `/missions/plan`) | -- |
| Flight | Phase E `/sim/run` | `Frame.talk`: the (sender, receiver) of every CBBA bid message delivered in that tick |
| Camera | `services/perception` `GET /vehicles/{id}/perception` (webcam with `PERCEPTION_USE_WEBCAM=1`, i.e. `run_all.ps1 -Webcam`; real YOLOv8) | `?image=true` returns the picture with boxes from the same inference; `POST /survey/camera` returns counts + picture, or `unavailable` (see "Camera revision") |
| Page | the panel's plan / sim / instruction code | "★ Quick survey" box, Go, orange "talk" lines, 📷 badges, camera list, summary |

Endpoints (fleet-agent, :8020): `POST /survey/plan` (text + map view →
scenario or a question; nothing planned yet), `POST /survey/camera`.

### The AI's job stays the same

The line is read by the goal parser, whose output schema (`ParsedGoal`)
only has `intent`, `num_vehicles`, `altitude_m`. It never sees or produces
coordinates. Points, launch spots and fence come from the map view by
fixed code; who goes where is CBBA; whether it may fly is the safety gate
(hard rules 1 and 2). Tested: a fake AI returning 4 drones gives exactly
the same points as the rules path.

### Out of scope stays out of scope

Before any AI runs, the line is checked against the goal parser's list
(weapons, attack, strike, bomb, shoot, …) **and** the instruction panel's
list (target, destroy, hit, neutralise, enemy, hostile). "find targets
here" is refused. Camera results are display only: shown to the operator,
never fed back into planning, never acted on (CBBA_PLAN.md §2C, §8).

### Phases D and E on a survey

They work unchanged on the survey mission: "D2 is offline, S3 is high
priority" (S3 = S03), Apply → re-plan → in survey mode the flight re-runs
straight away. "Make this drone fail now" → the others re-plan and finish
its points. Playback slows to at most 2x while any drone is deciding
(status planning/holding) so the talking can be seen.

## Honest limits

- **The camera is this computer's webcam**, not a camera at the place on
  the map (the drones are simulated). The page says which camera took
  each picture.
- **The talking is simulated** (in-process `LossyTransport`, as Phase E).
- Survey points are grid-cell centres -- "look at this cell", not a full
  lawnmower sweep. `lawnmower_path` exists and could be used later.
- The survey area is a square-ish box in the middle of the screen, not a
  drawn polygon. Drawing a fence by hand still works through the normal
  mission setup.
- Camera results are not stored (CBBA_PLAN.md §2C planned stored
  observations; they are only on the page for now).

## Camera revision (same day, on the operator's feedback)

The first version showed **made-up counts** (seeded random numbers) when
the perception service was not running. The operator rejected that, rightly:
a number nobody detected proves nothing. Now:

- No invented result anywhere. Camera not running → the point shows
  "📷 ✕ camera not connected" and the command to start it.
- When a drone reaches a place, the browser playback **pauses**, the
  perception service grabs a webcam frame **at that moment** and runs
  YOLOv8 on it; the page shows that picture with the boxes drawn, the
  counts, then the flight continues.
- `scripts/run_perception.ps1 -Webcam` and `scripts/run_all.ps1 -Webcam`
  start the camera service with the webcam.
- Without `-Webcam` the service still works on its bundled sample photos,
  and every result says "sample photo".

## Test results (sandbox, October 2026)

- `services/fleet-agent`: 556 passed, 1 skipped (CrewAI not installed),
  including `tests/survey/` (26): Delhi, Mumbai, London, Sydney, Nairobi,
  Oslo -- each: gate passes, every point assigned exactly once, every
  point inspected in the flight, drones exchanged bids; drone lost →
  the others finish all points; refusals; camera via perception (mocked,
  GET only) and fallback.
- Sweep (not a unit test): 6 cities × 4 view sizes (100 m … whole city)
  × 1/2/3/5/8 drones = 120 surveys, 0 blocked; 36 of them flown, every
  point inspected once.
- `apps/gcs-web`: 25 Node tests passed.
- Browser (headless Chromium): Delhi view, "3 drones survey this area" →
  12 points, 3 orange talk lines at start, 12/12 checked, summary shown;
  Mumbai: "attack this area" refused; 4 drones; "D2 is offline, S3 is
  high priority" → re-planned without D2, 16/16; D3 failed mid-flight →
  2 planning rounds, 16/16. No page errors.

### After the camera revision

- `services/fleet-agent`: 557 passed, 1 skipped. Camera tests: live
  webcam answer, sample-photo answer, camera down → `unavailable` with
  empty counts (nothing invented).
- `services/perception`: 34 passed, with the real YOLOv8n model (weights
  downloaded) -- including the new `image=true` picture.
- `apps/gcs-web`: 25 passed.
- Browser, real perception service running (sample photos -- this sandbox
  has no webcam): Delhi, 3 drones, 12/12 places photographed; the flight
  clock stayed still while pictures were taken; each point showed the
  real picture with YOLOv8's boxes (e.g. "3 people, 1 bus"). With no
  camera service: 12 × "camera not connected", no numbers.
- **Not tested here: a real webcam** (no camera device in the sandbox).
  The webcam path is the perception service's existing
  `PERCEPTION_USE_WEBCAM=1` code, unit-tested with a fake camera; the
  first real test is on the operator's PC.
