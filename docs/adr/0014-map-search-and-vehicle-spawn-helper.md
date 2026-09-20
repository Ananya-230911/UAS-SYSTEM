# ADR-0014: Map Location Search + Vehicle Spawn Helper

## Status
Accepted

## Context
The original 6-phase roadmap (docs/PHASE_1_PLAN.md, extended through
Phase 6b's ADR-0013) is complete — there is no Phase 7 on the roadmap.
This ADR documents a UX/tooling addition made after that roadmap
finished, not a new numbered phase: the map always opened centered on a
single hardcoded location (Stanford, CA), and every simulated vehicle
always started from one of two hardcoded home coordinates baked into
`scripts/run_local.ps1`/`run_local.sh` and the README's manual
instructions. Two real usability gaps, addressed together because both
are "where in the world" concerns:

1. No way to look at anywhere else on the map without manually panning.
2. No way to start a new simulated vehicle anywhere other than the
   fixed coordinates already written into the docs/scripts.

## Decision

**Map search is a pure viewport change, nothing more.** A search box
(`apps/gcs-web`, geocoding via OpenStreetMap's free Nominatim API — no
API key, consistent with this page already using OSM's free tile
server) recenters/zooms the map to wherever you search. It does not
move, create, or affect any vehicle — a vehicle's real position always
comes from its own telemetry, wherever that vehicle's simulator was
actually started. This is deliberately kept a dumb, one-way lookup
(place name → `map.setView()`), not tied into vehicle discovery at all.

**The vehicle spawn helper hands back commands, it doesn't launch
anything.** `apps/gcs-web` is a static file served to the browser (see
ADR-0005) — it has no ability to start OS processes, and giving a
browser page (even a locally-served one) the ability to spawn arbitrary
local processes via an API call would be a real security/architecture
change this project's scale doesn't call for, not a convenience. So
"Add a Vehicle" works the way every other multi-vehicle setup in this
project already does (see the README's "Want a second vehicle"
section, and `scripts/run_local.sh`'s `FLEET_SIZE`): a human runs the
gateway and simulator as their own processes. What this adds is
removing the tedium of *computing* the right values by hand — click a
spot on the map, and the exact `VEHICLE_ID`/port numbers/
`--home-lat`/`--home-lon` values are filled into ready-to-copy commands,
using the same port-numbering scheme (8001/14550, 8002/14551, ...) the
README's manual walkthrough already established.

`scripts/run_local.ps1` also gained optional `-HomeLat`/`-HomeLon`
parameters (both required together, or neither — never silently
defaulting one), so the single vehicle it starts can begin anywhere
without hand-editing the script, using exactly the same
`sim.py --home-lat/--home-lon` flags that already existed.

## Consequences
- Zero backend/API changes — this is entirely `apps/gcs-web` (plus the
  two new optional script parameters). No new endpoints, no new tests
  needed in `services/api`.
- **Known limitation — Nominatim has no SLA for this project.** It's a
  free, best-effort public geocoding service with a fair-use rate
  limit; fine for a human occasionally typing a search, not something
  to call in a tight loop or depend on for anything automated.
- **Known limitation — the spawn helper's port suggestions are
  best-effort.** They're computed from how many vehicles this browser
  tab currently knows about, not by checking what's actually free on
  the machine — the same manual-coordination expectation the README's
  existing multi-vehicle instructions already carry. The user can edit
  any of the four fields before copying.
- **Follow-up, not built now:** if this project ever wants the API
  itself to launch vehicle processes (making "Add a Vehicle" fully
  one-click), that is a real architecture change — process lifecycle
  management, port allocation, and the security implications of a
  web-triggered local process launch — and deserves its own ADR and
  explicit sign-off when it's actually needed, not something to fold
  into a UI convenience feature.
