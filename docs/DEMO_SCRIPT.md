# UAS-SYSTEM — Live Demo Script

A rehearsed walkthrough for presenting UAS-SYSTEM in person. Follow the
steps in order; each has exactly what to click and a one-line talking
point. Total time: ~10-12 minutes.

**Practice this once, start to finish, before the real demo.** Muscle
memory matters more than memorizing lines.

---

## Before your sir arrives (setup, not part of the demo itself)

1. Open a terminal at the project root and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\stop_local.ps1
   powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
   ```
   Wait for `Open: http://localhost:8080/?api=http://localhost:8000`.
2. Open that URL in a **fresh** browser tab (not a cached old one).
3. Arrange your windows so the browser is full-screen/large and the
   terminal windows are minimized (you won't need them again unless
   something goes wrong).
4. Confirm the Fleet list shows `sim-1`. If it says "Waiting for
   vehicles…", give it 5 seconds, then refresh once.

---

## 1. Introduce it (30 sec)

**Say:** "This is UAS-SYSTEM — ground control software for drones. It
lets an operator see live telemetry, send commands, and fly missions,
for one drone or a whole fleet, with safety features like geofencing."

Point at the architecture, if asked — it's in the README:
```
simulation/sitl  --MAVLink/UDP-->  telemetry-gateway  --HTTP-->  api  <--WS/REST-->  gcs-web
```
**Say:** "The drone itself is simulated for this demo — same real
MAVLink protocol real autopilots speak — but the system was also
validated once against real PX4 flight-stack firmware, not just this
simulator."

## 2. Basic control (1.5 min)

1. Point at the map — `sim-1`'s marker, live telemetry panel on the right.
2. Click **Arm**. **Say:** "That's a real command going through the API
   to the vehicle, and back — watch the Armed field flip."
3. Click **Takeoff (20m)**. Watch Altitude climb in the telemetry panel.
4. Click **RTL**. **Say:** "Return-to-launch — it'll fly home and
   disarm automatically."

## 3. Plan and fly a mission (2 min)

1. Under **Mission**, click 2-3 points on the map near the drone.
2. Click **Upload**, then **Start**.
3. Point at the **Waypoint** field advancing and the drone's marker
   moving along the dashed path.
4. **Say:** "This uses the real MAVLink mission protocol — the same
   handshake a real autopilot expects — not a custom shortcut."

## 4. Search anywhere + add a second vehicle (2 min) — the newest feature

1. In the map search box, type your city or college name, press Enter.
   **Say:** "You're not locked into one location — you can look
   anywhere."
2. Under **Add a Vehicle**, click **Pick location**, then click the map
   where you searched.
3. **Say:** "This fills in a suggested vehicle ID and ports, and gives
   me the exact commands to start a new simulated drone right there."
   (You don't have to actually run them live unless you have 2 spare
   terminals ready and rehearsed — showing the generated commands
   proves the feature; running them is a bonus if you're confident.)

## 5. Geofencing + automatic failsafe (2.5 min) — the strongest "wow" moment

1. Select `sim-1` again (or whichever vehicle is near you).
2. Under **Geofence**, click **Draw**, click 3-4 points to outline a
   small boundary **around** the drone's current position, click
   **Draw** again to stop, then **Save**.
3. Under **Mission**, click a point **outside** that boundary, click
   **Upload**. **Say:** "Watch — it gets rejected before it's even sent
   to the drone." Point at the red `Upload FAILED` message.
4. Now click **Delete** under Geofence, then **Draw** a **tiny**
   boundary that does **not** include the drone's current position,
   **Save**.
5. Click **Arm**. **Say:** "Now watch the Geofence row." Within a
   second it should flip to `BREACHED — auto-RTL issued` in red, and
   the drone starts flying home **without anyone clicking RTL**.
   **Say:** "That's a safety failsafe reacting on its own."

## 6. Risk monitoring + Remote ID (1.5 min)

1. Point at the **Risk** row in telemetry. **Say:** "This watches for
   unusual patterns — battery draining too fast, weak GPS, sudden
   altitude loss — and flags them for the operator. It never takes
   action itself, unlike the geofence failsafe — it's advisory."
2. Under **Remote ID**, click **Fetch broadcast**. **Say:** "Real
   drones are legally required to broadcast their identity and
   location — this simulates that broadcast's content."

## 6b. Optional: AI perception & decision (2 min) — only if the perception service is running

**Only do this section if you started `scripts/run_perception.ps1`
beforehand and confirmed it's up** — it's a separate, optional service
(heavier dependency, kept out of the core startup on purpose). Skip
this section entirely rather than risk it during the demo if you
didn't rehearse it.

1. Under **Perception & AI Decision**, click **Detect objects**.
   **Say:** "This runs a real YOLOv8 object-detection model — not a
   mock — on a simulated camera frame." Point at the detected objects
   and confidence percentages.
2. Click **Get recommendation**. **Say:** "This adds a retrieval-based
   decision layer — it matches the situation against a small set of
   safety guidelines, then runs it through independent guardrail rules
   before recommending an action. It's advisory only, same as the risk
   monitor — it never commands the vehicle itself."
3. Point at the recommended action badge and the reasoning text.

## 7. Wrap-up (1 min)

**Say:** "Everything here is backed by automated tests — over 100 of
them — plus an end-to-end test that actually flies a simulated mission
start to finish. Every major design decision is documented as an
Architecture Decision Record, including the honest limitations of each
piece." *(Open `docs/adr/` briefly if they want to see one.)*

---

## If something goes wrong live

- **A panel looks stale/broken:** hard refresh the browser
  (Ctrl+Shift+R).
- **A vehicle disappeared / "Waiting for vehicles":** check the 4
  terminal windows are still open and didn't crash; worst case, run
  `stop_local.ps1` then `run_local.ps1` again — it takes under a
  minute.
- **You don't remember a step:** it's fine to say "let me show you the
  code for that instead" and open the relevant file — you understand
  this system well enough to talk through the code if the live demo
  hiccups.
