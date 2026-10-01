from pathlib import Path

from datamanager.config import config
from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.services import assets, polygons
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner


def polygons_fingerprint(resolved: dict) -> str:
    """What the polygons depend on: per region its core and overlap countries and the carve of each carved
    country (not addresses, GTFS or SRTM, which the region hash also covers)."""
    import hashlib
    import json

    regions = [
        [r["name"], sorted(c["iso2"] for c in r["core"]), sorted(c["iso2"] for c in r["overlap"]),
         sorted(([c["iso2"], c["carve"]] for c in r["core"] + r["overlap"] if c.get("carve")), key=lambda x: x[0])]
        for r in resolved.get("regions", [])
    ]
    content = {"regions": sorted(regions, key=lambda r: r[0]), "borders": sorted(map(sorted, resolved.get("border_regions", [])))}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


class PolygonsStage(StageRunner):
    """Derives the region core, 100 km overlap, border-pair and carve polygons of a configuration and
    stores them as `.poly` (+ `.geojson`) assets. Needs only the configuration; the OSM extract stage
    consumes the approved result."""

    key = "polygons"
    produces = (
        StageIO("core-polygon"),
        StageIO("overlap-polygon"),
        StageIO("border-polygon"),
        StageIO("carve-polygon"),
    )

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_id is None:
            raise ValidationError("polygons needs a configuration")
        session = SessionLocal()
        run_id = int(context.run_id)
        fingerprint = polygons_fingerprint(context.config_resolved or {})
        context.step_cb("Deriving polygons")
        specs = polygons.compute(session, context.config_id)
        out_dir = assets.asset_dir("polygons", run_id)

        entries, invalid = [], []
        for index, spec in enumerate(specs, 1):
            context.step_cb(f"{spec.name} ({index}/{len(specs)})")
            geometry, facts = polygons.finalize(spec.geometry)
            entry = {"name": spec.name, "kind": spec.kind, "regions": spec.regions, "countries": spec.countries, **facts}
            if facts["valid"]:
                poly, geojson = polygons.write_files(out_dir, spec.name, geometry)
                asset = assets.create(
                    session, run_id, context.config_id, spec.asset_type, spec.name, poly,
                    meta={"geojson": str(geojson.resolve().relative_to(Path(config.DATA_ROOT).resolve())), "countries": spec.countries,
                          "regions": spec.regions, "area_km2": facts["area_km2"], "vertices": facts["vertices"],
                          "fingerprint": fingerprint},
                )
                entry.update(asset_id=asset.id, poly_bytes=asset.size_bytes, sha256=asset.content_hash,
                             preview=polygons.preview(geometry))
            else:
                invalid.append(spec.name)
                entry["message"] = "empty or invalid geometry"
            entries.append(entry)

        report = {
            "summary": {
                "polygons": len(entries),
                "by_kind": {k: sum(1 for e in entries if e["kind"] == k) for k in ("core", "overlap", "border", "carve")},
                "invalid": len(invalid),
                "vertices": sum(e["vertices"] for e in entries),
            },
            "polygons": entries,
        }
        if invalid:
            report["error"] = "Empty or invalid polygons: " + ", ".join(invalid)
        return StageResult(status="failed" if invalid else "success", report=report)
