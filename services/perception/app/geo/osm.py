"""Mapped features around the point from OpenStreetMap (ODbL).

Buildings, roads, parking, parks, trees and water as people have MAPPED
them -- not detected from the image. Kept separate and labelled so the
dashboard never mixes the two. Overpass API usage policy: light use,
identified User-Agent; results are cached by the caller.
"""
from __future__ import annotations

import math
import os
import threading
import time

import httpx

OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")

_ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
          "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
_PATHS = {"footway", "path", "cycleway", "pedestrian", "steps", "track"}
_GREEN_LANDUSE = {"grass", "forest", "meadow", "farmland", "orchard", "recreation_ground", "village_green", "farmyard"}
_GREEN_LEISURE = {"park", "garden", "pitch", "playground", "golf_course", "nature_reserve"}
_GREEN_NATURAL = {"wood", "scrub", "grassland", "heath", "tree_row", "wetland"}


def _length_m(geom: list[dict]) -> float:
    total = 0.0
    for a, b in zip(geom, geom[1:]):
        dlat = math.radians(b["lat"] - a["lat"])
        dlon = math.radians(b["lon"] - a["lon"]) * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
        total += 6_371_000 * math.hypot(dlat, dlon)
    return total


def summarise(elements: list[dict]) -> dict:
    buildings = parking = trees = green = water = 0
    roads: dict[str, float] = {}
    paths_m = 0.0
    names: list[str] = []
    for e in elements:
        t = e.get("tags", {})
        if "building" in t:
            buildings += 1
        hw = t.get("highway")
        if hw in _ROADS and e.get("geometry"):
            roads[hw] = roads.get(hw, 0.0) + _length_m(e["geometry"])
        elif hw in _PATHS and e.get("geometry"):
            paths_m += _length_m(e["geometry"])
        if t.get("amenity") == "parking":
            parking += 1
        if t.get("natural") == "tree":
            trees += 1
        if t.get("landuse") in _GREEN_LANDUSE or t.get("leisure") in _GREEN_LEISURE or t.get("natural") in _GREEN_NATURAL:
            green += 1
        if t.get("natural") == "water" or "waterway" in t:
            water += 1
        if t.get("name") and (t.get("amenity") or t.get("building") or t.get("leisure") or t.get("tourism") or t.get("office")):
            if t["name"] not in names:
                names.append(t["name"])
    return {
        "source": "OpenStreetMap contributors (ODbL) via the Overpass API - mapped features, NOT detected from the image",
        "buildings": buildings,
        "roads_m": {k: round(v) for k, v in sorted(roads.items(), key=lambda kv: -kv[1])},
        "footpaths_m": round(paths_m),
        "parking_areas": parking,
        "single_trees_mapped": trees,
        "green_areas": green,
        "water_features": water,
        "named_places": names[:8],
    }


def mapped_features(south: float, west: float, north: float, east: float, http: httpx.Client) -> dict:
    b = f"{south},{west},{north},{east}"
    q = (f"[out:json][timeout:25];("
         f'way["building"]({b});relation["building"]({b});way["highway"]({b});'
         f'nwr["amenity"="parking"]({b});node["natural"="tree"]({b});'
         f'way["landuse"]({b});way["leisure"]({b});way["natural"]({b});way["waterway"]({b});'
         f'nwr["amenity"]["name"]({b});'
         f");out geom;")
    resp = http.post(OVERPASS_URL, data={"data": q})
    resp.raise_for_status()
    return summarise(resp.json().get("elements", []))


_nominatim_lock = threading.Lock()
_nominatim_last = 0.0


def place_name(lat: float, lon: float, http: httpx.Client) -> str | None:
    """Nominatim's usage policy: at most one request per second."""
    global _nominatim_last
    with _nominatim_lock:
        wait = 1.1 - (time.monotonic() - _nominatim_last)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last = time.monotonic()
    resp = http.get(f"{NOMINATIM_URL}/reverse", params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 17})
    resp.raise_for_status()
    return resp.json().get("display_name")
