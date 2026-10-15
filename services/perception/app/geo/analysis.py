"""What can be read from a retrieved geographic image, and what can't.

- Sentinel-2 (10 m): land cover from ESA's own Scene Classification band
  (SCL) plus NDVI from the real red and near-infrared bands. No object
  detection: at 10 m per pixel a car is smaller than one pixel.
- Sharper aerial imagery: object detection (app/detection.py:
  detect_aerial) and a colour-based vegetation estimate (RGB only).

Every number here is computed from the retrieved pixels; nothing is
filled in.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

# ESA Sentinel-2 L2A Scene Classification classes.
_SCL_GROUP = {
    4: "vegetation",
    5: "not vegetated (built-up or bare soil)",
    6: "water",
    11: "snow or ice",
    3: "cloud or cloud shadow", 8: "cloud or cloud shadow", 9: "cloud or cloud shadow", 10: "cloud or cloud shadow",
}

OBB_MAX_GSD_M = 1.0  # beyond this, aerial object detection is pointless
COCO_MAX_GSD_M = 0.15  # drone-level sharpness: also try the ground-photo model


def landcover_sentinel2(scl: np.ndarray, red, nir) -> dict:
    total = scl.size
    shares: dict[str, float] = {}
    for cls, name in _SCL_GROUP.items():
        shares[name] = shares.get(name, 0.0) + float((scl == cls).sum())
    other = total - sum(shares.values())
    out = {name: round(v / total * 100, 1) for name, v in shares.items() if v}
    if other:
        out["other / unclassified"] = round(other / total * 100, 1)
    res = {"method": "ESA Sentinel-2 Scene Classification (SCL) + NDVI from the red and near-infrared bands",
           "percent": dict(sorted(out.items(), key=lambda kv: -kv[1]))}
    if red is None or nir is None:
        res["method"] = "ESA Sentinel-2 Scene Classification (SCL); NDVI not available"
        return res
    clear = ~np.isin(scl, [0, 1, 3, 8, 9, 10])
    ndvi = (np.ma.filled(nir, np.nan) - np.ma.filled(red, np.nan)) / (np.ma.filled(nir, np.nan) + np.ma.filled(red, np.nan) + 1e-6)
    vals = ndvi[clear & np.isfinite(ndvi)]
    if vals.size:
        res["ndvi_mean"] = round(float(vals.mean()), 2)
        res["vegetation_density_percent"] = {
            "dense (NDVI > 0.6)": round(float((vals > 0.6).mean() * 100), 1),
            "moderate (0.3-0.6)": round(float(((vals > 0.3) & (vals <= 0.6)).mean() * 100), 1),
            "little or none (< 0.3)": round(float((vals <= 0.3).mean() * 100), 1),
        }
    return res


def landcover_rgb(rgb: np.ndarray) -> dict:
    """Excess-green index on an RGB image: a rough vegetation share. Only an
    estimate -- no infrared band, so shadows and green roofs can fool it."""
    f = rgb.astype(np.float32)
    s = f.sum(axis=2) + 1e-6
    r, g, b = f[..., 0] / s, f[..., 1] / s, f[..., 2] / s
    valid = rgb.max(axis=2) > 0
    veg = ((2 * g - r - b) > 0.05) & (f[..., 1] > 40) & valid
    n = max(int(valid.sum()), 1)
    return {"method": "colour-based estimate (excess-green index on RGB); no infrared band available",
            "percent": {"vegetation (estimate)": round(float(veg.sum()) / n * 100, 1),
                        "everything else": round(100 - float(veg.sum()) / n * 100, 1)}}


def limitations(gsd_m: float, capture_date: str | None, provider: str, ran: list[str]) -> list[str]:
    out = [f"This is retrieved geographic imagery from {provider}"
           + (f", captured {capture_date}" if capture_date else " (capture date not given by the source)")
           + ". It is not live footage from the simulated drone."]
    if gsd_m >= 5:
        out.append(f"One pixel is about {gsd_m:.0f} m x {gsd_m:.0f} m: a car is smaller than one pixel and people are invisible, "
                   "so no object detection was run. What this image can show: vegetation, water, open ground and larger built-up areas.")
    elif gsd_m > OBB_MAX_GSD_M:
        out.append(f"At {gsd_m:.1f} m per pixel only large structures are visible; object detection would not be reliable, so it was not run.")
    else:
        out.append(f"Resolution {gsd_m:.2f} m per pixel. Ran: {', '.join(ran)}. The aerial model only knows its 15 DOTA classes "
                   "(vehicles, planes, ships, storage tanks, pools, sports courts, bridges, harbours, roundabouts, helicopters); "
                   "buildings, roads and trees are not among them - see the OpenStreetMap section for those.")
        if gsd_m > 0.3:
            out.append("Small vehicles are only a few pixels wide at this resolution; some will be missed.")
        if gsd_m > COCO_MAX_GSD_M:
            out.append("People are not meaningfully visible at this resolution and are not looked for.")
        else:
            out.append("At this sharpness people can be visible, but YOLO models are not trained to recognise people seen from "
                       "straight above; person detections here are unreliable.")
    if capture_date:
        try:
            age = (dt.date.today() - dt.date.fromisoformat(capture_date)).days / 365.25
            if age >= 2:
                out.append(f"The image is about {age:.0f} years old; things on the ground may have changed.")
        except ValueError:
            pass
    return out


def annotate(rgb: np.ndarray, detections: list[dict], gsd_m: float, label: str):
    """Display copy: enlarged if tiny (nearest-neighbour, so every real
    pixel stays a visible square - no invented detail), detection boxes,
    a crosshair on the requested point and a scale bar. Returns BGR."""
    import cv2

    h, w = rgb.shape[:2]
    k = max(1, math.ceil(400 / max(h, w)))
    img = cv2.resize(rgb[:, :, ::-1], (w * k, h * k), interpolation=cv2.INTER_NEAREST) if k > 1 else rgb[:, :, ::-1].copy()
    for d in detections:
        pts = (np.array(d["polygon"], dtype=np.float32) * k).astype(np.int32)
        color = (0, 200, 255) if d["model"].startswith("YOLOv8n-OBB") else (255, 120, 0)
        cv2.polylines(img, [pts], True, color, 2)
        x, y = pts[:, 0].min(), max(pts[:, 1].min() - 4, 10)
        cv2.putText(img, f"{d['class_name']} {d['confidence']:.2f}", (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    H, W = img.shape[:2]
    cx, cy = W // 2, H // 2
    for c, t in (((0, 0, 0), 3), ((255, 255, 255), 1)):
        cv2.line(img, (cx - 14, cy), (cx + 14, cy), c, t)
        cv2.line(img, (cx, cy - 14), (cx, cy + 14), c, t)
    # scale bar: a round length near a quarter of the width
    metres_per_px = gsd_m / k
    target = W / 4 * metres_per_px
    nice = 10 ** math.floor(math.log10(max(target, 1e-6)))
    nice *= 5 if target / nice >= 5 else 2 if target / nice >= 2 else 1
    bar = int(round(nice / metres_per_px))
    y0 = H - 28
    cv2.rectangle(img, (8, y0 - 4), (8 + bar, y0 + 4), (255, 255, 255), -1)
    cv2.rectangle(img, (8, y0 - 4), (8 + bar, y0 + 4), (0, 0, 0), 1)
    cv2.putText(img, f"{nice:g} m", (12 + bar, y0 + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.rectangle(img, (0, H - 16), (W, H), (0, 0, 0), -1)
    cv2.putText(img, label[: max(10, W // 7)], (4, H - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA)
    return img
