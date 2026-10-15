"""Step-by-step check of real imagery retrieval on THIS computer.

    cd services/perception
    ..\\..\\.venv\\Scripts\\python.exe -m app.geo.diagnose 12.8231 80.0442

Prints each step (catalogue search, opening the image file, reading the
window) with the full error if one fails, so a network/firewall problem
can be told apart from a code problem. Read-only; changes nothing.
"""
from __future__ import annotations

import sys
import time

import httpx

from app.geo import imagery


def main(lat: float, lon: float) -> int:
    import rasterio

    print(f"rasterio {rasterio.__version__}, GDAL {rasterio.__gdal_version__}")
    print(f"certificates for GDAL: {imagery.GDAL_ENV.get('CURL_CA_BUNDLE', 'none (certifi not installed)')}")
    client = httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), headers={"User-Agent": imagery.USER_AGENT}, follow_redirects=True)
    t = time.time()
    try:
        r = client.post(f"{imagery.STAC_URL}/search", json={
            "collections": ["sentinel-2-l2a"], "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "query": {"eo:cloud_cover": {"lt": 40}}, "sortby": [{"field": "properties.datetime", "direction": "desc"}], "limit": 1})
        r.raise_for_status()
        f = r.json()["features"][0]
    except Exception as exc:  # noqa: BLE001 -- a diagnostic prints everything
        print(f"1. Sentinel-2 catalogue search: FAILED ({time.time() - t:.1f} s): {imagery.describe_error(exc)}")
        return 1
    print(f"1. Sentinel-2 catalogue search: OK ({time.time() - t:.1f} s) -> {f['id']}, {f['properties'].get('datetime')}")
    for key in ("visual", "scl", "red", "nir"):
        href = f["assets"][key]["href"]
        t = time.time()
        try:
            data, native, scale = imagery.read_window(href, lat, lon, 150, resampling="nearest")
            print(f"2. {key:6s}: OK ({time.time() - t:.1f} s) {data.shape[2]}x{data.shape[1]} px of {native:g} m  <- {href}")
        except Exception as exc:  # noqa: BLE001
            print(f"2. {key:6s}: FAILED ({time.time() - t:.1f} s): {imagery.describe_error(exc)}\n          file: {href}")
    try:
        img, attempts = imagery.retrieve(lat, lon, 100, client)
        print(f"3. Full retrieval: OK -> {img.provider}, captured {img.capture_date}, {img.gsd_m:.2f} m/px, {img.rgb.shape[1]}x{img.rgb.shape[0]} px")
        if img.extra.get("bands_note"):
            print(f"   note: {img.extra['bands_note']}")
    except imagery.ImageryUnavailable as exc:
        print("3. Full retrieval: FAILED")
        for a in exc.attempts:
            print(f"   {a['source']}: {a['result']}")
        return 1
    return 0


if __name__ == "__main__":
    lat, lon = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) == 3 else (12.8231, 80.0442)
    sys.exit(main(lat, lon))
