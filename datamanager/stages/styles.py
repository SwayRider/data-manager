import hashlib
import json

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import Asset
from datamanager.services import assets, map_styles
from datamanager.services import settings as settings_service
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner

MODES = ("light", "dark")


def build_styles(session, config_id: int) -> dict[str, dict]:
    """mode -> the release style (a template for tilesservice) of the configuration."""
    return {mode: map_styles.release_style(session, config_id, mode) for mode in MODES}


def identity(session) -> dict:
    """Id and name the style has in the tiles release (Settings → Map style in the tiles release)."""
    return {"id": settings_service.get(session, "styles.id"), "label": settings_service.get(session, "styles.label")}


def styles_fingerprint(session, config_id: int) -> str:
    """What the style files are made of: the chosen styles, label zooms, id and name. Hashing the built styles
    also notices a regenerated vendored style."""
    built = build_styles(session, config_id)
    return hashlib.sha256(json.dumps([built, identity(session)], sort_keys=True).encode()).hexdigest()[:16]


def next_version(session, config_id: int, style_id: str, fingerprint: str) -> int:
    """The release version of the style: unchanged content keeps its version, changed content gets the next one
    (versions of a style id are immutable once packaged, apps may still use them)."""
    rows = (
        session.query(Asset)
        .filter(Asset.config_profile_id == config_id, Asset.asset_type == "style", Asset.status != "rejected")
        .order_by(Asset.id.desc()).all()
    )
    same_id = [a for a in rows if a.meta_json.get("style_id") == style_id and a.meta_json.get("version")]
    for asset in same_id:
        if asset.meta_json.get("fingerprint") == fingerprint:
            return int(asset.meta_json["version"])
    return max((int(a.meta_json["version"]) for a in same_id), default=0) + 1


def validate(style: dict) -> list[str]:
    """Problems that would make MapLibre reject or empty the style, or tilesservice unable to serve it."""
    problems = []
    sources = style.get("sources", {})
    source = sources.get(map_styles.SOURCE)
    if source is None:
        problems.append(f"source '{map_styles.SOURCE}' is missing")
    elif source.get("tiles") != [map_styles.RELEASE_TILES]:
        problems.append("the tiles source is not the tilesservice template")
    if style.get("glyphs") != map_styles.RELEASE_GLYPHS:
        problems.append("glyphs are not the tilesservice template")
    flavor = str(style.get("sprite", "")).removeprefix(map_styles.TEMPLATE_BASE + "/sprites/")
    if not str(style.get("sprite", "")).startswith(map_styles.TEMPLATE_BASE + "/sprites/") or flavor not in map_styles.vendored_sprites():
        problems.append(f"sprite sheet '{flavor}' is not in the vendored assets")
    missing = sorted(map_styles.font_stacks(style) - map_styles.vendored_fonts())
    if missing:
        problems.append("font stack(s) not in the vendored glyphs: " + ", ".join(missing))
    ids = [layer.get("id") for layer in style.get("layers", [])]
    if len(ids) != len(set(ids)):
        problems.append("duplicate layer ids")
    for layer in style.get("layers", []):
        layer_source = layer.get("source")
        if layer_source is not None and layer_source not in sources:
            problems.append(f"layer {layer.get('id')} uses unknown source '{layer_source}'")
    return problems


class StylesStage(StageRunner):
    """Writes `style-light.json` and `style-dark.json` of a configuration (chosen base styles and label zooms of
    the Style tab) as assets, so a release can ship them next to the tiles. They are templates tilesservice fills
    in (tiles, glyphs and sprite URLs); the style has an id and name (Settings) and an integer version that
    goes up when the content changes. Needs only the configuration."""

    key = "styles"
    produces = (StageIO("style"),)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_id is None:
            raise ValidationError("styles needs a configuration")
        session = SessionLocal()
        run_id = int(context.run_id)
        chosen = map_styles.get_settings(session, context.config_id)
        who = identity(session)
        context.step_cb("Building the styles")
        built = build_styles(session, context.config_id)
        fingerprint = styles_fingerprint(session, context.config_id)
        version = next_version(session, context.config_id, who["id"], fingerprint)
        out_dir = assets.asset_dir("styles", run_id)

        entries, warnings = [], []
        for mode, style in built.items():
            context.step_cb(f"Writing style-{mode}.json")
            problems = validate(style)
            path = out_dir / f"style-{mode}.json"
            path.write_text(json.dumps(style, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            asset = assets.create(
                session, run_id, context.config_id, "style", f"style-{mode}", path,
                meta={"mode": mode, "base_style": chosen[f"{mode}_style"], "labels": chosen["labels"], "fingerprint": fingerprint,
                      "style_id": who["id"], "style_label": who["label"], "version": version,
                      "sprite": map_styles.sprite_flavor(chosen[f"{mode}_style"])},
            )
            entries.append({
                "name": f"style-{mode}", "mode": mode, "base_style": chosen[f"{mode}_style"], "asset_id": asset.id,
                "bytes": asset.size_bytes, "sha256": asset.content_hash, "layers": len(style["layers"]),
                "fonts": sorted(map_styles.font_stacks(style)), "sprite": map_styles.sprite_flavor(chosen[f"{mode}_style"]),
                "problems": problems,
            })
            warnings.extend(f"style-{mode}: {p}" for p in problems)

        report = {
            "summary": {"styles": len(entries), "labels": chosen["labels"], "fingerprint": fingerprint,
                        "style_id": who["id"], "style_label": who["label"], "version": version},
            "styles": entries,
            "warnings": warnings,
        }
        failed = [e["name"] for e in entries if e["problems"]]
        if failed:
            report["error"] = "Invalid styles: " + ", ".join(failed)
        return StageResult(status="failed" if failed else "success", report=report)
