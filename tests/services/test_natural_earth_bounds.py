from shapely.geometry import MultiPolygon, box

from datamanager.countries.natural_earth import mainland_bounds, srtm_bbox_from_bounds


def test_nearby_islands_are_kept_and_distant_territories_ignored():
    mainland = box(-5, 42.3, 8.2, 51)
    corsica = box(8.55, 41.37, 9.56, 43)  # near: must be in the SRTM box
    guiana = box(-54, 2, -51, 5.7)  # overseas: must not blow up the box
    bounds = mainland_bounds(MultiPolygon([mainland, corsica, guiana]))
    assert bounds == (-5, 41.37, 9.56, 51)
    assert srtm_bbox_from_bounds(bounds) == [41, 51, -5, 10]


def test_single_polygon_unchanged():
    assert mainland_bounds(box(0, 1, 2, 3)) == (0, 1, 2, 3)
