"""Retrieve a real image of a lat/lon from openly licensed sources.

Why these sources (checked October 2026, see docs/adr/0022):

1. OpenAerialMap (https://openaerialmap.org) -- aerial and drone imagery,
   often 0.02-0.5 m per pixel, every image licensed CC-BY 4.0 (free to
   display AND to analyse). Coverage is patchy: only where someone has
   uploaded imagery. Tried first because it is the only free, openly
   licensed source sharp enough for vehicles.
2. Copernicus Sentinel-2 L2A (ESA), found via Element 84's free Earth
   Search STAC API, read from the public AWS "sentinel-cogs" bucket. Covers
   the whole world every ~5 days with a real capture date and cloud
   information, under the Copernicus free, full and open data policy. But
   one pixel is 10 m x 10 m: fields, water, vegetation and large built-up
   areas are visible; individual vehicles and people are not.

Not used, on purpose: Esri World Imagery, Google and Mapbox satellite --
their terms do not allow running our own AI on the imagery (Esri: derived
data "strictly for non-commercial use within ArcGIS"). Nothing here ever
substitutes another picture: if no source covers the point, it raises
ImageryUnavailable and the caller shows that error.

Only a small window around the point is read (HTTP range requests on
Cloud-Optimized GeoTIFFs), never a whole scene.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import httpx
import numpy as np

OAM_URL = os.environ.get("OAM_API_URL", "https://api.openaerialmap.org")
STAC_URL = os.environ.get("STAC_API_URL", "https://earth-search.aws.element84.com/v1")
USER_AGENT = "UAS-SYSTEM student project (simulation; geographic inspection)"

M_PER_DEG_LAT = 111_320.0
OAM_MAX_GSD_M = 1.0  # sharper than this or not worth preferring over Sentinel-2
MAX_PIXELS = 1024  # longest side of the analysed image
# ESA Scene Classification (SCL) classes that mean "can't see the ground".
SCL_CLOUD = {3, 8, 9, 10}

def _ca_bundle() -> dict:
    """GDAL's own HTTP client can't always find the system certificates
    (common on Windows: "SSL certificate problem"); give it certifi's."""
    try:
        import certifi

        return {"CURL_CA_BUNDLE": certifi.where()}
    except ImportError:
        return {}


GDAL_ENV = {
    **_ca_bundle(),
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.TIF,.TIFF",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_USERAGENT": USER_AGENT,
}


class ImageryUnavailable(Exception):
    """No source has a usable image of this point. `attempts` says what
    each source answered."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class GeoImage:
    rgb: np.ndarray  # H x W x 3 uint8, north up: exactly the pixels that get analysed
    bounds: tuple[float, float, float, float]  # south, west, north, east (WGS84) of the image
    gsd_m: float  # ground size of one pixel of `rgb`, metres
    provider: str
    title: str
    capture_date: str | None  # ISO date of acquisition, if the source gives one
    license: str
    attribution: str
    source_url: str  # the file the pixels were read from
    scene_id: str | None = None
    cloud_pct: float | None = None  # share of the window hidden by cloud/shadow (Sentinel-2 SCL)
    extra: dict = field(default_factory=dict)  # provider-specific arrays (e.g. scl, red, nir)


def box_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a square of +/- radius_m around the point."""
    dlat = radius_m / M_PER_DEG_LAT
    dlon = radius_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def _ground_pixel_m(ds, lat: float) -> float:
    """Ground size of one native pixel, metres."""
    px = abs(ds.transform.a)
    if ds.crs.is_geographic:
        return px * M_PER_DEG_LAT * math.cos(math.radians(lat))
    if ds.crs.to_epsg() == 3857:  # Web Mercator metres shrink by cos(lat) on the ground
        return px * math.cos(math.radians(lat))
    return px


def read_window(href: str, lat: float, lon: float, radius_m: float, bands=None, out_size: tuple[int, int] | None = None,
                resampling: str = "bilinear"):
    """Read the +/- radius_m window around (lat, lon) from a georeferenced
    raster (local path or https URL of a COG). Returns (masked array
    [bands, H, W], native ground pixel metres, scale factor applied).

    The point must lie on valid data, otherwise ImageryUnavailable-like
    ValueError -- an image that doesn't contain the point is never used."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform, transform_bounds
    from rasterio.windows import from_bounds

    with rasterio.Env(**GDAL_ENV), rasterio.open(href) as ds:
        west, south, east, north = box_around(lat, lon, radius_m)
        left, bottom, right, top = transform_bounds("EPSG:4326", ds.crs, west, south, east, north)
        # The point itself, in the raster's pixel grid.
        (px,), (py,) = transform("EPSG:4326", ds.crs, [lon], [lat])
        row, col = ds.index(px, py)
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            raise ValueError("the point is outside this image")
        window = from_bounds(left, bottom, right, top, ds.transform)
        native = _ground_pixel_m(ds, lat)
        if out_size is None:
            scale = min(1.0, MAX_PIXELS / max(window.width, window.height))
            out_size = (max(1, round(window.height * scale)), max(1, round(window.width * scale)))
        scale = out_size[1] / window.width
        indexes = bands or list(range(1, min(ds.count, 3) + 1))
        data = ds.read(indexes, window=window, out_shape=(len(indexes), *out_size), boundless=True, masked=True,
                       resampling=getattr(Resampling, resampling))
        h, w = out_size
        if np.ma.getmaskarray(data)[:, h // 2, w // 2].all():
            raise ValueError("the image has no data at the point")
        return data, native, scale


def _to_uint8(data: np.ma.MaskedArray) -> np.ndarray:
    """[bands, H, W] -> H x W x 3 uint8 (8-bit kept as is; 16-bit stretched
    2-98 percentile). Missing data -> black."""
    arr = np.ma.filled(data, 0)
    if arr.shape[0] == 1:
        arr = np.repeat(arr, 3, axis=0)
    if arr.dtype != np.uint8:
        valid = np.ma.compressed(data)
        lo, hi = (np.percentile(valid, [2, 98]) if valid.size else (0, 1))
        arr = np.clip((arr.astype(np.float32) - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(np.transpose(arr[:3], (1, 2, 0)))


# --- OpenAerialMap ---------------------------------------------------------------

def openaerialmap(lat: float, lon: float, radius_m: float, http: httpx.Client) -> GeoImage:
    """Sharpest-then-newest OAM image whose footprint contains the point."""
    d = 0.0002
    resp = http.get(f"{OAM_URL}/meta", params={"bbox": f"{lon - d},{lat - d},{lon + d},{lat + d}",
                                               "gsd_to": OAM_MAX_GSD_M, "limit": 20})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    tried = []
    for rec in results:
        href = rec.get("uuid", "")
        bbox = rec.get("bbox") or []
        if not href.lower().endswith((".tif", ".tiff")) or len(bbox) != 4:
            continue
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        try:
            data, native, scale = read_window(href, lat, lon, radius_m)
        except (ValueError, OSError) as exc:  # rasterio errors are OSError subclasses
            tried.append(f"{rec.get('title') or href}: {exc}")
            continue
        west, south, east, north = box_around(lat, lon, radius_m)
        date = (rec.get("acquisition_start") or rec.get("acquisition_end") or "")[:10] or None
        provider = rec.get("provider") or "unknown contributor"
        return GeoImage(
            rgb=_to_uint8(data), bounds=(south, west, north, east), gsd_m=float(native / scale),
            provider="OpenAerialMap", title=rec.get("title") or "OpenAerialMap image",
            capture_date=date, license=rec.get("license") or "CC-BY 4.0",
            attribution=f"© {provider}, OpenAerialMap / Open Imagery Network (CC-BY 4.0)",
            source_url=href, scene_id=rec.get("_id"),
        )
    if tried:
        raise ValueError("OpenAerialMap images cover the area but could not be read here: " + "; ".join(tried[:3]))
    raise ValueError("no OpenAerialMap imagery covers this point")


# --- Sentinel-2 -----------------------------------------------------------------------

def _reflectance(data: np.ma.MaskedArray, asset: dict) -> np.ma.MaskedArray:
    rb = (asset.get("raster:bands") or [{}])[0]
    return data.astype(np.float32) * float(rb.get("scale", 0.0001)) + float(rb.get("offset", 0.0))


def sentinel2(lat: float, lon: float, radius_m: float, http: httpx.Client, max_scenes: int = 4) -> GeoImage:
    """Newest Sentinel-2 L2A scene over the point whose window is mostly
    cloud-free (checked with ESA's own Scene Classification band)."""
    radius_m = max(radius_m, 150.0)  # at least 30 x 30 pixels of 10 m
    body = {
        "collections": ["sentinel-2-l2a"],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "query": {"eo:cloud_cover": {"lt": 40}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "limit": 10,
    }
    resp = http.post(f"{STAC_URL}/search", json=body)
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        raise ValueError("no Sentinel-2 scene with under 40% cloud covers this point")
    best = None
    for f in features[:max_scenes]:
        a = f.get("assets", {})
        # s3:// links are "requester pays" (need AWS credentials): skip those scenes.
        if not all(k in a and a[k].get("href") and not str(a[k]["href"]).startswith("s3://") for k in ("visual", "scl", "red", "nir")):
            continue
        try:
            visual, native, scale = read_window(a["visual"]["href"], lat, lon, radius_m, resampling="nearest")
            size = visual.shape[1:]
            scl = np.ma.filled(read_window(a["scl"]["href"], lat, lon, radius_m, out_size=size, resampling="nearest")[0][0], 0)
        except (ValueError, OSError):
            continue
        cloud = float(np.isin(scl, list(SCL_CLOUD)).mean() * 100)
        cand = (cloud, f, a, visual, native, scale, scl)
        if best is None or cloud < best[0]:
            best = cand
        if cloud <= 20:
            break
    if best is None:
        raise ValueError("Sentinel-2 scenes found, but none had readable public image files")
    cloud, f, a, visual, native, scale, scl = best
    size = visual.shape[1:]
    red = _reflectance(read_window(a["red"]["href"], lat, lon, radius_m, out_size=size, resampling="nearest")[0][0], a["red"])
    nir = _reflectance(read_window(a["nir"]["href"], lat, lon, radius_m, out_size=size, resampling="nearest")[0][0], a["nir"])
    west, south, east, north = box_around(lat, lon, radius_m)
    when = str(f.get("properties", {}).get("datetime", ""))[:10] or None
    return GeoImage(
        rgb=_to_uint8(visual), bounds=(south, west, north, east), gsd_m=float(native / scale),
        provider="Copernicus Sentinel-2 L2A (ESA), via Element 84 Earth Search",
        title=f.get("id", "Sentinel-2 scene"), capture_date=when,
        license="Copernicus Sentinel data: free, full and open (Copernicus data policy)",
        attribution=f"Contains modified Copernicus Sentinel data {when[:4] if when else ''}".strip(),
        source_url=a["visual"]["href"], scene_id=f.get("id"), cloud_pct=round(cloud, 1),
        extra={"scl": scl, "red": red, "nir": nir},
    )


def retrieve(lat: float, lon: float, radius_m: float, http: httpx.Client | None = None) -> tuple[GeoImage, list[dict]]:
    """Best available real image of the point + what each source said."""
    client = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    attempts: list[dict] = []
    try:
        for name, fn in (("OpenAerialMap", openaerialmap), ("Sentinel-2", sentinel2)):
            try:
                img = fn(lat, lon, radius_m, client)
                attempts.append({"source": name, "result": "used"})
                return img, attempts
            except (ValueError, OSError, httpx.HTTPError, KeyError, TypeError) as exc:
                attempts.append({"source": name, "result": f"{type(exc).__name__}: {exc}"})
    finally:
        if http is None:
            client.close()
    raise ImageryUnavailable("No openly licensed imagery could be retrieved for this point.", attempts)
