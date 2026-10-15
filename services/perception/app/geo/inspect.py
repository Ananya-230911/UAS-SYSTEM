"""One geographic inspection: lat/lon -> real image -> analysis -> report.

The report keeps three things apart, as the dashboard must:
  - the inspection point (what was asked for),
  - the retrieved image (source, capture date, resolution, licence, bounds),
  - what was analysed (the exact pixels given to YOLO, by SHA-256, and
    what each method found or why it was not run).
"""
from __future__ import annotations

import base64
import hashlib
import threading
from collections import OrderedDict

import httpx

from app import detection
from app.geo import analysis, osm
from app.geo.imagery import USER_AGENT, ImageryUnavailable, retrieve

_cache: OrderedDict = OrderedDict()
_CACHE_SIZE = 64
_cache_lock = threading.Lock()


def _jpeg_b64(bgr) -> str:
    import cv2

    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""


def _raw_image(bgr) -> tuple[str, str]:
    """The analysed pixels for the map overlay: lossless PNG when small (a
    30 x 30 Sentinel-2 window must not be smeared by JPEG), JPEG when large."""
    import cv2

    if max(bgr.shape[:2]) <= 256:
        ok, buf = cv2.imencode(".png", bgr)
        return (base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""), "image/png"
    return _jpeg_b64(bgr), "image/jpeg"


def inspect_location(lat: float, lon: float, radius_m: float = 100.0, http: httpx.Client | None = None,
                     inference_lock: threading.Lock | None = None) -> dict:
    """Raises ImageryUnavailable when no source has an image of the point."""
    key = (round(lat, 5), round(lon, 5), round(radius_m))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    client = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        img, attempts = retrieve(lat, lon, radius_m, client)

        # The exact pixels analysed. YOLO gets the same array in BGR order.
        digest = hashlib.sha256(img.rgb.tobytes()).hexdigest()
        detections: list[dict] = []
        ran: list[str] = []
        if img.gsd_m <= analysis.OBB_MAX_GSD_M:
            use_coco = bool(img.gsd_m <= analysis.COCO_MAX_GSD_M)
            bgr = img.rgb[:, :, ::-1].copy()
            lock = inference_lock or threading.Lock()
            with lock:
                detections = detection.detect_aerial(bgr, use_coco=use_coco)
            ran = ["YOLOv8n-OBB (aerial, DOTA classes)"] + (["YOLOv8n (COCO)"] if use_coco else [])

        if "scl" in img.extra:
            landcover = analysis.landcover_sentinel2(img.extra["scl"], img.extra["red"], img.extra["nir"])
        else:
            landcover = analysis.landcover_rgb(img.rgb)

        south, west, north, east = img.bounds
        try:
            mapped = osm.mapped_features(south, west, north, east, client)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            mapped = {"error": f"OpenStreetMap (Overpass) did not answer: {type(exc).__name__}: {exc}"}
        try:
            name = osm.place_name(lat, lon, client)
        except (httpx.HTTPError, ValueError, KeyError):
            name = None
    finally:
        if http is None:
            client.close()

    counts: dict[str, int] = {}
    for d in detections:
        counts[d["class_name"]] = counts.get(d["class_name"], 0) + 1
    label = f"Retrieved imagery: {img.provider.split(',')[0]} {img.capture_date or ''} - not live footage"
    report = {
        "point": {"lat": lat, "lon": lon, "name": name, "radius_m": radius_m},
        "imagery": {
            "kind": "retrieved geographic imagery (not live drone footage)",
            "provider": img.provider,
            "title": img.title,
            "scene_id": img.scene_id,
            "capture_date": img.capture_date,
            "gsd_m": round(img.gsd_m, 3),
            "cloud_pct": img.cloud_pct,
            "license": img.license,
            "attribution": img.attribution,
            "source_url": img.source_url,
            "bounds": {"south": south, "west": west, "north": north, "east": east},
            "width": int(img.rgb.shape[1]),
            "height": int(img.rgb.shape[0]),
        },
        "analysed_image_sha256": digest,
        "models_run": ran,
        "detections": detections,
        "counts": counts,
        "landcover": landcover,
        "osm": mapped,
        "limitations": analysis.limitations(img.gsd_m, img.capture_date, img.provider.split(",")[0], ran),
        "attempts": attempts,
        "image_jpeg_b64": _jpeg_b64(analysis.annotate(img.rgb, detections, img.gsd_m, label)),
    }
    report["raw_image_b64"], report["raw_image_mime"] = _raw_image(img.rgb[:, :, ::-1])
    with _cache_lock:
        _cache[key] = report
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return report


__all__ = ["ImageryUnavailable", "inspect_location"]
