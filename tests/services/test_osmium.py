import shutil

import pytest

from datamanager.services import osmium

pytestmark = pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium not installed")


def test_failure_raises_and_leaves_no_output(tmp_path):
    out = tmp_path / "out.osm.pbf"
    with pytest.raises(osmium.OsmiumError, match="failed"):
        osmium.merge_latest("osmium", [tmp_path / "missing1.pbf", tmp_path / "missing2.pbf"], out)
    assert list(tmp_path.iterdir()) == []


def test_merge_of_nothing_is_an_error(tmp_path):
    with pytest.raises(osmium.OsmiumError, match="nothing"):
        osmium.merge_latest("osmium", [], tmp_path / "x.osm.pbf")


def test_poly_bbox(tmp_path):
    poly = tmp_path / "a.poly"
    poly.write_text("a\n1\n   0.000000   50.000000\n   2.500000   51.500000\n   1.000000   49.000000\nEND\nEND\n")
    assert osmium.poly_bbox(poly) == [0.0, 49.0, 2.5, 51.5]


def _tiny_pbf(tmp_path):
    import subprocess
    xml = tmp_path / "t.osm"
    xml.write_text("<?xml version='1.0'?><osm version='0.6'><node id='1' version='1' timestamp='2024-01-01T00:00:00Z' lat='50.5' lon='0.5'/></osm>")
    pbf = tmp_path / "t.osm.pbf"
    subprocess.run(["osmium", "cat", str(xml), "-o", str(pbf)], check=True)
    poly = tmp_path / "a.poly"
    poly.write_text("a\n1\n   0.0   50.0\n   1.0   50.0\n   1.0   51.0\n   0.0   51.0\n   0.0   50.0\nEND\nEND\n")
    return pbf, poly


def test_extract_many_reports_peak_memory_and_moves_outputs(tmp_path):
    pbf, poly = _tiny_pbf(tmp_path)
    out = tmp_path / "out" / "a.osm.pbf"
    peak = osmium.extract_many("osmium", pbf, [(poly, out)], tmp_path / "work")
    assert out.exists() and peak >= 0


def test_extract_many_is_stopped_when_memory_is_low(tmp_path, monkeypatch):
    pbf, poly = _tiny_pbf(tmp_path)
    monkeypatch.setattr(osmium, "available_gb", lambda: 0.5)
    with pytest.raises(osmium.OsmiumMemoryError, match="only 0.5 GB"):
        osmium.extract_many("osmium", pbf, [(poly, tmp_path / "out.osm.pbf")], tmp_path / "work", min_free_gb=4)
    assert not (tmp_path / "out.osm.pbf").exists()


def test_bbox_coverage():
    poly = [5.7, 49.4, 6.5, 50.2]
    assert osmium.bbox_coverage([4.8, 48.7, 6.6, 50.7], poly) == 1.0  # data pokes out on every side: fine
    assert osmium.bbox_coverage([5.7, 49.4, 6.1, 50.2], poly) == pytest.approx(0.5)
    assert osmium.bbox_coverage([7, 51, 8, 52], poly) == 0.0  # data elsewhere entirely
    assert osmium.bbox_coverage([6, 50, 6, 50.1], poly) is None  # degenerate data bbox is not comparable
    assert osmium.bbox_coverage(None, poly) is None and osmium.bbox_coverage(poly, [1, 1, 1, 2]) is None
