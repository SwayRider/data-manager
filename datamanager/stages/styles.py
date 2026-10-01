import hashlib
import json

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.services import assets, map_styles
from datamanager.services import settings as settings_service
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner

MODES = ("light", "dark")


def build_styles(session, config_id: int) -> dict[str, dict]:
    """mode -> the style of the configuration with the Public URLs of the settings applied."""
    urls = settings_service.public_urls(session)
    return {mode: map_styles.config_style(session, config_id, mode, **urls) for mode in MODES}


def styles_fingerprint(session, config_id: int) -> str:
    """What the style files are made of: the chosen styles, label zooms and public URLs. Hashing the built
    styles also notices a regenerated vendored style."""
    built = build_styles(session, config_id)
    return hashlib.sha256(json.dumps(built, sort_keys=True).encode()).hexdigest()[:16]


def validate(style: dict) -> list[str]:
    """Problems that would make MapLibre reject or empty the style."""
    problems = []
    sources = style.get("sources", {})
    if map_styles.SOURCE not in sources:
        problems.append(f"source '{map_styles.SOURCE}' is missing")
    elif "__TILES__" in str(sources[map_styles.SOURCE].get("url", "")):
        problems.append("the tiles URL is still the placeholder (Settings → Public URLs)")
    ids = [layer.get("id") for layer in style.get("layers", [])]
    if len(ids) != len(set(ids)):
        problems.append("duplicate layer ids")
    for layer in style.get("layers", []):
        source = layer.get("source")
        if source is not None and source not in sources:
            problems.append(f"layer {layer.get('id')} uses unknown source '{source}'")
    return problems


class StylesStage(StageRunner):
    """Writes `style-light.json` and `style-dark.json` of a configuration (chosen base styles and label zooms of
    the Style tab, URLs from Settings → Public URLs) as assets, so a release can ship them next to the tiles.
    Needs only the configuration."""

    key = "styles"
    produces = (StageIO("style"),)

    def run(self, context: StageRunContext) -> StageResult:
        if context.config_id is None:
            raise ValidationError("styles needs a configuration")
        session = SessionLocal()
        run_id = int(context.run_id)
        chosen = map_styles.get_settings(session, context.config_id)
        context.step_cb("Building the styles")
        built = build_styles(session, context.config_id)
        fingerprint = styles_fingerprint(session, context.config_id)
        out_dir = assets.asset_dir("styles", run_id)

        entries, warnings = [], []
        for mode, style in built.items():
            context.step_cb(f"Writing style-{mode}.json")
            problems = validate(style)
            path = out_dir / f"style-{mode}.json"
            path.write_text(json.dumps(style, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            asset = assets.create(
                session, run_id, context.config_id, "style", f"style-{mode}", path,
                meta={"mode": mode, "base_style": chosen[f"{mode}_style"], "labels": chosen["labels"], "fingerprint": fingerprint},
            )
            entries.append({
                "name": f"style-{mode}", "mode": mode, "base_style": chosen[f"{mode}_style"], "asset_id": asset.id,
                "bytes": asset.size_bytes, "sha256": asset.content_hash, "layers": len(style["layers"]),
                "tiles_url": style["sources"][map_styles.SOURCE].get("url"), "problems": problems,
            })
            warnings.extend(f"style-{mode}: {p}" for p in problems)

        placeholder = [e["name"] for e in entries if e["tiles_url"] == map_styles.PUBLIC_TILES_URL]
        if placeholder:
            warnings.append("The tiles URL is still the example placeholder (Settings → Public URLs): " + ", ".join(placeholder) + ".")
        report = {
            "summary": {"styles": len(entries), "labels": chosen["labels"], "fingerprint": fingerprint},
            "styles": entries,
            "warnings": warnings,
        }
        failed = [e["name"] for e in entries if e["problems"]]
        if failed:
            report["error"] = "Invalid styles: " + ", ".join(failed)
        return StageResult(status="failed" if failed else "success", report=report)
