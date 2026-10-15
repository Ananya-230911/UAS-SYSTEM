"""Geographic imagery (app/geo). No network: the 'remote' files are real
GeoTIFFs written here with a marker pixel at the requested point, and the
catalogue APIs are mocked. That checks the parts that must be right -- the
image is OF the point, YOLO gets exactly those pixels, nothing is
substituted when a source has nothing."""
import hashlib
import math

import httpx
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from rasterio.warp import transform as warp

from app import detection
from app.geo import analysis, imagery, osm
from app.geo.imagery import ImageryUnavailable, read_window, retrieve

SRM = (12.8231, 80.0442)  # SRM Institute of Science and Technology, Kattankulathur
MARK = (255, 0, 0)


def _geotiff(path, crs, pixel, lat, lon, bands=3, dtype="uint8", fill=60, mark=MARK, size=600, mark_px=1, value=None):
    """A size x size raster centred on (lat, lon) with `mark` at the pixel(s) containing the point."""
    (x,), (y,) = warp("EPSG:4326", crs, [lon], [lat])
    tr = from_origin(x - size / 2 * pixel, y + size / 2 * pixel, pixel, pixel)
    data = np.full((bands, size, size), fill if value is None else value, dtype=dtype)
    row, col = rasterio.transform.rowcol(tr, x, y)
    r = mark_px // 2
    if mark is not None:
        for b in range(bands):
            data[b, row - r:row + r + 1, col - r:col + r + 1] = mark[b] if bands > 1 else mark[0]
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=bands, dtype=dtype, crs=crs, transform=tr) as ds:
        ds.write(data)
    return str(path)


def _center(rgb):
    h, w = rgb.shape[:2]
    return tuple(int(v) for v in rgb[h // 2, w // 2])


# --- the image is of the requested point -----------------------------------------

def test_window_is_centred_on_the_point_utm(tmp_path):
    """Sentinel-2 style: UTM zone 44N, 10 m pixels."""
    f = _geotiff(tmp_path / "s2.tif", "EPSG:32644", 10.0, *SRM)
    data, native, scale = read_window(f, *SRM, radius_m=150, resampling="nearest")
    rgb = imagery._to_uint8(data)
    assert native == 10.0 and rgb.shape[:2] == (30, 30)
    assert _center(rgb) == MARK                      # the marked point is the image centre
    assert _center(imagery._to_uint8(read_window(f, SRM[0] + 0.0009, SRM[1], radius_m=150, resampling="nearest")[0])) != MARK


def test_window_web_mercator_gsd_is_ground_metres(tmp_path):
    """OpenAerialMap images are often EPSG:3857, whose metres are stretched by 1/cos(lat)."""
    ground = 0.1
    f = _geotiff(tmp_path / "oam.tif", "EPSG:3857", ground / math.cos(math.radians(SRM[0])), *SRM, size=2400, mark_px=21)
    data, native, scale = read_window(f, *SRM, radius_m=100)
    assert native == pytest.approx(ground, rel=1e-6)
    assert max(data.shape[1:]) == imagery.MAX_PIXELS          # 2000 px window shrunk to 1024
    assert native / scale == pytest.approx(200 / 1024, rel=0.02)
    assert _center(imagery._to_uint8(data))[0] > 200


def test_point_outside_the_image_is_refused(tmp_path):
    f = _geotiff(tmp_path / "s2.tif", "EPSG:32644", 10.0, *SRM, size=100)
    with pytest.raises(ValueError, match="outside"):
        read_window(f, 13.0827, 80.2707, radius_m=150)    # Chennai Central, ~30 km away


# --- sources ------------------------------------------------------------------------

def _client(routes):
    def handler(request):
        for (method, part), fn in routes.items():
            if request.method == method and part in str(request.url):
                return fn(request)
        return httpx.Response(404, json={"error": "no route"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _s2_files(tmp_path, scl_value=4):
    visual = _geotiff(tmp_path / "visual.tif", "EPSG:32644", 10.0, *SRM)
    scl = _geotiff(tmp_path / "scl.tif", "EPSG:32644", 20.0, *SRM, bands=1, mark=None, value=scl_value, size=300)
    red = _geotiff(tmp_path / "red.tif", "EPSG:32644", 10.0, *SRM, bands=1, dtype="uint16", mark=None, value=1500)
    nir = _geotiff(tmp_path / "nir.tif", "EPSG:32644", 10.0, *SRM, bands=1, dtype="uint16", mark=None, value=5000)
    rb = [{"scale": 0.0001, "offset": -0.1}]
    return {
        "type": "Feature", "id": "S2B_44PLV_20260930_0_L2A",
        "properties": {"datetime": "2026-09-30T05:08:11Z", "eo:cloud_cover": 3.2},
        "assets": {"visual": {"href": visual}, "scl": {"href": scl},
                   "red": {"href": red, "raster:bands": rb}, "nir": {"href": nir, "raster:bands": rb}},
    }


def test_sentinel2_scene_with_date_cloud_and_bands(tmp_path):
    feat = _s2_files(tmp_path)
    seen = []

    def search(req):
        seen.append(req.read())
        return httpx.Response(200, json={"features": [feat]})

    img = imagery.sentinel2(*SRM, 100, _client({("POST", "/search"): search}))
    assert b'"coordinates":[80.0442,12.8231]' in seen[0].replace(b" ", b"")   # searched at the exact point
    assert img.capture_date == "2026-09-30" and img.cloud_pct == 0.0 and img.gsd_m == pytest.approx(10.0, rel=0.01)
    assert img.attribution == "Contains modified Copernicus Sentinel data 2026"
    assert _center(img.rgb) == MARK and img.rgb.shape[:2] == (30, 30)          # radius raised to 150 m minimum
    assert float(img.extra["red"].mean()) == pytest.approx(0.05) and float(img.extra["nir"].mean()) == pytest.approx(0.4)


def test_cloudy_window_tries_the_next_scene(tmp_path):
    cloudy = _s2_files(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, scl_value=9)
    cloudy["id"] = "cloudy"
    (tmp_path / "b").mkdir()
    clear = _s2_files(tmp_path / "b")
    clear["id"] = "clear"
    clear["properties"]["datetime"] = "2026-09-20T05:08:11Z"
    img = imagery.sentinel2(*SRM, 100, _client({("POST", "/search"): lambda r: httpx.Response(200, json={"features": [cloudy, clear]})}))
    assert img.scene_id == "clear" and img.cloud_pct == 0.0


def test_openaerialmap_preferred_when_it_covers_the_point(tmp_path):
    f = _geotiff(tmp_path / "oam.tif", "EPSG:3857", 0.3 / math.cos(math.radians(SRM[0])), *SRM, size=800, mark_px=9)
    rec = {"uuid": f, "bbox": [80.03, 12.81, 80.06, 12.83], "gsd": 0.3, "title": "SRM campus drone survey",
           "acquisition_start": "2025-02-11T00:00:00.000Z", "provider": "Example Drone Club", "license": "CC-BY 4.0", "_id": "abc"}
    calls = []

    def meta(req):
        calls.append(dict(req.url.params))
        return httpx.Response(200, json={"results": [rec]})

    img, attempts = retrieve(*SRM, 100, _client({("GET", "/meta"): meta}))
    assert img.provider == "OpenAerialMap" and img.capture_date == "2025-02-11" and img.gsd_m == pytest.approx(0.3, rel=0.01)
    assert "Example Drone Club" in img.attribution and img.license == "CC-BY 4.0"
    assert calls[0]["gsd_to"] == "1.0" and attempts == [{"source": "OpenAerialMap", "result": "used"}]
    assert _center(img.rgb)[0] > 200


def test_nothing_available_is_an_error_not_a_substitute():
    client = _client({("GET", "/meta"): lambda r: httpx.Response(200, json={"results": []}),
                      ("POST", "/search"): lambda r: httpx.Response(200, json={"features": []})})
    with pytest.raises(ImageryUnavailable) as e:
        retrieve(*SRM, 100, client)
    assert [a["source"] for a in e.value.attempts] == ["OpenAerialMap", "Sentinel-2"]
    assert "no OpenAerialMap imagery" in e.value.attempts[0]["result"] and "no Sentinel-2 scene" in e.value.attempts[1]["result"]


# --- analysis + full inspection ------------------------------------------------------

OVERPASS = {"elements": [
    {"type": "way", "tags": {"building": "university", "name": "Tech Park"}, "geometry": []},
    {"type": "way", "tags": {"building": "yes"}, "geometry": []},
    {"type": "way", "tags": {"highway": "residential"}, "geometry": [{"lat": 12.8231, "lon": 80.0442}, {"lat": 12.8240, "lon": 80.0442}]},
    {"type": "way", "tags": {"amenity": "parking"}, "geometry": []},
    {"type": "node", "tags": {"natural": "tree"}},
    {"type": "way", "tags": {"leisure": "park"}, "geometry": []},
]}


def _routes(extra):
    return {("POST", "interpreter"): lambda r: httpx.Response(200, json=OVERPASS),
            ("GET", "/reverse"): lambda r: httpx.Response(200, json={"display_name": "SRM Institute of Science and Technology, Kattankulathur"}),
            **extra}


def test_inspect_sentinel2_no_object_detection_landcover_and_osm(tmp_path, monkeypatch):
    from app.geo import inspect as gi

    gi._cache.clear()
    feat = _s2_files(tmp_path)
    monkeypatch.setattr(detection, "detect_aerial", lambda *a, **k: pytest.fail("YOLO must not run on 10 m imagery"))
    rep = gi.inspect_location(*SRM, 100, http=_client(_routes({
        ("GET", "/meta"): lambda r: httpx.Response(200, json={"results": []}),
        ("POST", "/search"): lambda r: httpx.Response(200, json={"features": [feat]})})))
    assert rep["imagery"]["kind"].startswith("retrieved geographic imagery")
    assert rep["imagery"]["capture_date"] == "2026-09-30" and rep["imagery"]["gsd_m"] == pytest.approx(10.0, rel=0.01)
    assert rep["models_run"] == [] and rep["detections"] == []
    assert rep["landcover"]["percent"] == {"vegetation": 100.0} and rep["landcover"]["ndvi_mean"] == pytest.approx(0.78, abs=0.01)
    assert rep["osm"]["buildings"] == 2 and rep["osm"]["roads_m"] == {"residential": 100} and rep["osm"]["parking_areas"] == 1
    assert "NOT detected from the image" in rep["osm"]["source"] and "Tech Park" in rep["osm"]["named_places"]
    assert rep["point"]["name"].startswith("SRM Institute")
    assert any("no object detection was run" in x for x in rep["limitations"])
    assert any("not live footage" in x for x in rep["limitations"])
    import base64, cv2
    raw = cv2.imdecode(np.frombuffer(base64.b64decode(rep["raw_image_b64"]), np.uint8), cv2.IMREAD_COLOR)
    assert rep["raw_image_mime"] == "image/png" and tuple(int(v) for v in raw[15, 15][::-1]) == MARK   # lossless, marker at centre


def test_inspect_high_res_yolo_gets_exactly_the_retrieved_pixels(tmp_path, monkeypatch):
    from app.geo import inspect as gi

    gi._cache.clear()
    f = _geotiff(tmp_path / "oam.tif", "EPSG:3857", 0.1 / math.cos(math.radians(SRM[0])), *SRM, size=1200, mark_px=15)
    rec = {"uuid": f, "bbox": [80.03, 12.81, 80.06, 12.83], "title": "t", "acquisition_start": "2024-01-05", "provider": "p"}
    got = {}

    def fake_yolo(bgr, use_coco=False, **k):
        got["bgr"], got["coco"] = bgr.copy(), use_coco
        return [{"model": "YOLOv8n-OBB (DOTA aerial)", "class_name": "small vehicle", "confidence": 0.81,
                 "polygon": [[10, 10], [30, 10], [30, 20], [10, 20]]}]

    monkeypatch.setattr(detection, "detect_aerial", fake_yolo)
    rep = gi.inspect_location(*SRM, 50, http=_client(_routes({("GET", "/meta"): lambda r: httpx.Response(200, json={"results": [rec]})})))
    rgb = got["bgr"][:, :, ::-1]
    assert hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest() == rep["analysed_image_sha256"]
    assert rep["imagery"]["width"] == rgb.shape[1] and rep["imagery"]["provider"] == "OpenAerialMap"
    assert got["coco"] is True                                  # 0.1 m: drone-level, COCO also tried
    assert rep["counts"] == {"small vehicle": 1} and rep["models_run"][0].startswith("YOLOv8n-OBB")
    assert any("years old" in x for x in rep["limitations"])
    assert rep["image_jpeg_b64"] and rep["raw_image_b64"] and rep["raw_image_mime"] == "image/jpeg"


def test_endpoint_404_lists_what_each_source_said(monkeypatch):
    from app import main
    from app.geo import inspect as gi

    def none(*a, **k):
        raise ImageryUnavailable("No openly licensed imagery could be retrieved for this point.",
                                 [{"source": "OpenAerialMap", "result": "ValueError: no OpenAerialMap imagery covers this point"},
                                  {"source": "Sentinel-2", "result": "ConnectError: refused"}])

    monkeypatch.setattr(gi, "inspect_location", none)
    resp = TestClient(main.app).get("/geo/inspect", params={"lat": SRM[0], "lon": SRM[1]})
    assert resp.status_code == 404 and len(resp.json()["detail"]["attempts"]) == 2
    assert TestClient(main.app).get("/geo/inspect", params={"lat": 95, "lon": 0}).status_code == 422


def test_limitations_by_resolution():
    assert any("smaller than one pixel" in x for x in analysis.limitations(10.0, "2026-09-30", "Sentinel-2", []))
    mid = analysis.limitations(0.5, None, "OpenAerialMap", ["YOLOv8n-OBB"])
    assert any("capture date not given" in x for x in mid) and any("People are not meaningfully visible" in x for x in mid)


def test_annotate_keeps_real_pixels_as_squares():
    rgb = np.zeros((30, 30, 3), np.uint8)
    rgb[0, 0] = (0, 255, 0)
    out = analysis.annotate(rgb, [], 10.0, "x")
    assert out.shape[:2] == (420, 420) and tuple(out[5, 5]) == (0, 255, 0)   # BGR of the green pixel, enlarged 14x


def test_real_obb_model_runs_on_an_image():
    """The actual YOLOv8n-OBB weights (downloaded once) load and return the
    documented structure. The sample frame is a ground photo, so finding
    nothing is fine; this checks the model, not the scene."""
    from app.camera import available_frames
    import cv2

    dets = detection.detect_aerial(cv2.imread(str(available_frames()[0])))
    assert isinstance(dets, list)
    assert all(d["model"].startswith("YOLOv8n-OBB") and len(d["polygon"]) == 4 for d in dets)
    assert detection._get_obb_model().names[10] == "small vehicle"


def test_point_near_the_scene_edge_is_padded_not_shifted(tmp_path):
    """Window half outside the raster: the outside part is 'no data', the
    point stays the centre pixel (nothing is shifted to fill the gap)."""
    lat, lon = SRM
    f = _geotiff(tmp_path / "edge.tif", "EPSG:32644", 10.0, lat, lon, size=600)
    # A point 2.9 km east of SRM: 10 px from the raster's east edge.
    lon2 = lon + 2900 / (111_320 * math.cos(math.radians(lat)))
    data, native, scale = read_window(f, lat, lon2, radius_m=150, resampling="nearest")
    h, w = data.shape[1:]
    mask = np.ma.getmaskarray(data)[0]
    assert not mask[h // 2, w // 2] and mask[:, -1].all() and not mask[:, 0].any()


def test_describe_error_keeps_the_hidden_cause():
    try:
        try:
            raise OSError("HTTP response code: 0 - Operation timed out after 30000 milliseconds")
        except OSError as inner:
            raise imagery.ImageryUnavailable("Read failed. See previous exception for details.", []) from inner
    except Exception as exc:
        text = imagery.describe_error(exc)
    assert "Read failed" in text and "Operation timed out after 30000 milliseconds" in text


def test_ndvi_bands_failing_keeps_the_image(tmp_path):
    feat = _s2_files(tmp_path)
    feat["assets"]["red"]["href"] = str(tmp_path / "missing_red.tif")
    img = imagery.sentinel2(*SRM, 100, _client({("POST", "/search"): lambda r: httpx.Response(200, json={"features": [feat]})}))
    assert _center(img.rgb) == MARK and img.extra["red"] is None and "NDVI not computed" in img.extra["bands_note"]
    lc = analysis.landcover_sentinel2(img.extra["scl"], None, None)
    assert lc["percent"] == {"vegetation": 100.0} and "NDVI not available" in lc["method"]
