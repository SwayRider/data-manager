import json

import pytest
from pyproj import Geod
from shapely.geometry import box

from datamanager.config import config
from datamanager.services import overlap

GEOD = Geod(ellps="WGS84")


@pytest.mark.parametrize("lat", [0, 50, 65])
def test_buffer_is_true_distance_in_every_direction(lat):
    lon = 5.0
    point_box = box(lon - 0.0005, lat - 0.0005, lon + 0.0005, lat + 0.0005)
    zone = overlap.buffer_m(point_box, 100_000)
    x0, y0, x1, y1 = zone.bounds
    east = GEOD.inv(lon, lat, x1, lat)[2]
    west = GEOD.inv(lon, lat, x0, lat)[2]
    north = GEOD.inv(lon, lat, lon, y1)[2]
    south = GEOD.inv(lon, lat, lon, y0)[2]
    for distance in (east, west, north, south):
        assert distance == pytest.approx(100_000, rel=0.01)


def test_buffer_grows_the_geometry_and_keeps_it_inside():
    core = box(0, 50, 1, 51)
    zone = overlap.buffer_m(core, 10_000)
    assert zone.contains(core) and zone.area > core.area


def _key(iso, bounds):
    x0, y0, x1, y1 = bounds
    path = config.DATA_ROOT + f"/geo-{iso}.geojson"
    with open(path, "w") as f:
        json.dump({"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}, f)
    return overlap.geometry_key(iso, f"geo-{iso}.geojson")


def test_overlap_threshold_is_100_km_not_64(tmp_data_root):
    # at 50.5°N one degree of longitude is ~71 km: 1.27° ≈ 90 km, 1.55° ≈ 110 km
    core = _key("aa", (0, 50, 1, 51))
    near = _key("bb", (2.27, 50, 3.27, 51))  # ~90 km east of aa: the old 3857 zone (~64 km) missed it
    far = _key("cc", (2.55, 50, 3.55, 51))  # ~110 km east
    assert overlap.overlap_candidates((core,), (core, near, far)) == ("bb",)


def test_bordering_uses_true_10_km_buffers(tmp_data_root):
    a = _key("aa", (0, 50, 1, 51))
    close = _key("bb", (1.12, 50, 2.12, 51))  # ~8 km gap: two 10 km buffers meet
    away = _key("cc", (1.4, 50, 2.4, 51))  # ~28 km gap
    assert overlap.bordering((a,), (close,)) and not overlap.bordering((a,), (away,))
