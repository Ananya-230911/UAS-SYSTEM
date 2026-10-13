# Leaflet 1.9.4 (vendored)

Unmodified copy of `dist/leaflet.js`, `dist/leaflet.css` and `dist/images/`
from the npm package `leaflet@1.9.4` (BSD 2-Clause, see LICENSE).

Bundled so the GCS map works without reaching unpkg.com -- a blocked or
flaky CDN left the whole page blank (no map, no buttons working). Map
*tiles* still come from OpenStreetMap and need internet; without it the
map background is grey but every marker, route and panel still works.
