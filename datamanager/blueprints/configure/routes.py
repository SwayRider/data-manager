import json

import yaml
from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from datamanager.db import SessionLocal
from datamanager.errors import ValidationError
from datamanager.models import ConfigProfile, Country, Region
from datamanager.services import carve as carve_service
from datamanager.services import downloads
from datamanager.services import config_profiles as profiles
from datamanager.services import regions as region_service
from datamanager.services import resolve as resolve_service
from datamanager.services import settings as settings_service
from datamanager import map_styles
from datamanager.stages.download_tiles import TILES_KEY
from datamanager.services import geofabrik
from datamanager.services import map_styles as style_service

bp = Blueprint(
    "configure",
    __name__,
    url_prefix="/configure",
    template_folder="templates",
    static_folder="static",
)

TABS = (("map", "Map"), ("style", "Style"), ("transit", "Transit"), ("resolved", "Resolved"))
TAB_KEYS = {key for key, _ in TABS}
LAST_CONFIG_COOKIE = "last_config"


def _get_config_or_404(config_id: int) -> ConfigProfile:
    profile = profiles.get_profile(SessionLocal(), config_id)
    if profile is None:
        abort(404)
    return profile


def _hx_redirect(url: str) -> Response:
    return Response("", headers={"HX-Redirect": url})


def _render_tab(config: ConfigProfile, tab: str):
    """Full page for direct navigation, bare partial for htmx tab switches."""
    if tab not in TAB_KEYS:
        abort(404)
    template = f"configure/tabs/{tab}.html"
    extra = {}
    if tab == "map":
        extra = {"regions": region_service.list_regions(SessionLocal(), config.id)}
    elif tab == "style":
        extra = _style_context(config)
    elif tab == "transit":
        extra = {"resolved": resolve_service.resolve_config(SessionLocal(), config.id), "gtfs_error": None,
                 "gtfs_error_region": None, "gtfs_values": {}}
    elif tab == "resolved":
        extra = {"resolved": resolve_service.resolve_config(SessionLocal(), config.id)}
    if request.headers.get("HX-Request"):
        html = render_template(template, tabs=TABS, active_tab=tab, current=config, **extra)
    else:
        html = render_template(
            "configure/index.html",
            tabs=TABS,
            active_tab=tab,
            tab_template=template,
            current=config,
            profiles=profiles.list_profiles(SessionLocal()),
            **extra,
        )
    response = make_response(html)
    response.set_cookie(LAST_CONFIG_COOKIE, str(config.id), max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


def _render_form(profile: ConfigProfile | None, values: dict, error: str | None = None):
    action = url_for("configure.create") if profile is None else url_for("configure.edit", config_id=profile.id)
    html = render_template(
        "configure/_config_form.html",
        action=action,
        title="New Configuration" if profile is None else "Edit Configuration",
        values=values,
        error=error,
    )
    return html, (422 if error else 200)


@bp.get("/")
def index():
    session = SessionLocal()
    last = request.cookies.get(LAST_CONFIG_COOKIE, type=int)
    target = profiles.get_profile(session, last) if last else None
    if target is None:
        available = profiles.list_profiles(session)
        if not available:
            return render_template("configure/empty.html")
        target = available[0]
    return redirect(url_for("configure.map_tab", config_id=target.id))


@bp.get("/<int:config_id>/")
def map_tab(config_id: int):
    return _render_tab(_get_config_or_404(config_id), "map")


@bp.get("/<int:config_id>/tabs/<tab>")
def tab(config_id: int, tab: str):
    return _render_tab(_get_config_or_404(config_id), tab)


@bp.get("/new")
def new():
    return _render_form(None, {"name": "", "description": ""})[0]


@bp.post("/new")
def create():
    values = {"name": request.form.get("name", ""), "description": request.form.get("description", "")}
    try:
        profile = profiles.create_profile(SessionLocal(), **values)
    except ValidationError as exc:
        return _render_form(None, values, exc.message)
    return _hx_redirect(url_for("configure.map_tab", config_id=profile.id))


@bp.route("/<int:config_id>/edit", methods=["GET", "POST"])
def edit(config_id: int):
    profile = _get_config_or_404(config_id)
    if request.method == "GET":
        return _render_form(profile, {"name": profile.name, "description": profile.description})[0]
    values = {"name": request.form.get("name", ""), "description": request.form.get("description", "")}
    try:
        profiles.update_profile(SessionLocal(), profile, **values)
    except ValidationError as exc:
        return _render_form(profile, values, exc.message)
    return _hx_redirect(url_for("configure.map_tab", config_id=profile.id))


@bp.post("/<int:config_id>/delete")
def delete(config_id: int):
    profiles.delete_profile(SessionLocal(), _get_config_or_404(config_id))
    return _hx_redirect(url_for("configure.index"))


@bp.post("/<int:config_id>/duplicate")
def duplicate(config_id: int):
    try:
        copy = profiles.duplicate_profile(SessionLocal(), _get_config_or_404(config_id))
    except ValidationError as exc:
        return exc.message, 422
    return _hx_redirect(url_for("configure.map_tab", config_id=copy.id))


# --- Regions (shown on the map tab) -----------------------------------------


def _get_region_or_404(config_id: int, region_id: int) -> Region:
    _get_config_or_404(config_id)
    region = region_service.get_region(SessionLocal(), config_id, region_id)
    if region is None:
        abort(404)
    return region


def _region_json(region: Region | None) -> dict | None:
    return None if region is None else {"id": region.id, "name": region.name, "color": region.color}


def _render_region_form(config_id: int, region: Region | None, values: dict, error=None, field=None):
    action = (
        url_for("configure.region_create", config_id=config_id)
        if region is None
        else url_for("configure.region_edit", config_id=config_id, region_id=region.id)
    )
    html = render_template(
        "configure/_region_form.html",
        action=action,
        region=region,
        config_id=config_id,
        title="Add Region" if region is None else "Edit Region",
        values=values,
        error=error,
        error_field=field,
    )
    return html, (422 if error else 200)


@bp.get("/<int:config_id>/regions/assignments")
def region_assignments(config_id: int):
    _get_config_or_404(config_id)
    return jsonify(region_service.country_assignments(SessionLocal(), config_id))


@bp.get("/<int:config_id>/regions/new")
def region_new(config_id: int):
    _get_config_or_404(config_id)
    color = region_service.next_color(SessionLocal(), config_id)
    return _render_region_form(config_id, None, {"name": "", "color": color})[0]


@bp.post("/<int:config_id>/regions/new")
def region_create(config_id: int):
    _get_config_or_404(config_id)
    values = {"name": request.form.get("name", ""), "color": request.form.get("color", "")}
    try:
        region_service.create_region(SessionLocal(), config_id, values["name"], values["color"])
    except ValidationError as exc:
        return _render_region_form(config_id, None, values, exc.message, exc.details.get("field"))
    return _hx_redirect(url_for("configure.map_tab", config_id=config_id))


@bp.route("/<int:config_id>/regions/<int:region_id>/edit", methods=["GET", "POST"])
def region_edit(config_id: int, region_id: int):
    region = _get_region_or_404(config_id, region_id)
    if request.method == "GET":
        return _render_region_form(config_id, region, {"name": region.name, "color": region.color})[0]
    values = {"name": request.form.get("name", ""), "color": request.form.get("color", "")}
    try:
        region_service.update_region(SessionLocal(), region, values["name"], values["color"])
    except ValidationError as exc:
        return _render_region_form(config_id, region, values, exc.message, exc.details.get("field"))
    return _hx_redirect(url_for("configure.map_tab", config_id=config_id))


@bp.post("/<int:config_id>/regions/<int:region_id>/delete")
def region_delete(config_id: int, region_id: int):
    region_service.delete_region(SessionLocal(), _get_region_or_404(config_id, region_id))
    return _hx_redirect(url_for("configure.map_tab", config_id=config_id))


@bp.post("/<int:config_id>/regions/<int:region_id>/countries/<iso2>")
def region_add_country(config_id: int, region_id: int, iso2: str):
    region = _get_region_or_404(config_id, region_id)
    try:
        region_service.assign_country(SessionLocal(), region, iso2)
    except ValidationError as exc:
        return jsonify(error=exc.message), 422
    return jsonify(iso2=iso2.lower(), region=_region_json(region))


@bp.delete("/<int:config_id>/regions/<int:region_id>/countries/<iso2>")
def region_remove_country(config_id: int, region_id: int, iso2: str):
    region_service.remove_country(SessionLocal(), _get_region_or_404(config_id, region_id), iso2)
    return jsonify(iso2=iso2.lower(), region=None)


@bp.get("/<int:config_id>/regions/overlap")
def region_overlap(config_id: int):
    _get_config_or_404(config_id)
    return jsonify(region_service.overlap_summary(SessionLocal(), config_id))


@bp.get("/<int:config_id>/regions/<int:region_id>/buffer")
def region_buffer(config_id: int, region_id: int):
    region = _get_region_or_404(config_id, region_id)
    return jsonify(region_service.region_buffer(SessionLocal(), region))


@bp.post("/<int:config_id>/regions/<int:region_id>/overlap/<iso2>")
def region_set_overlap(config_id: int, region_id: int, iso2: str):
    region = _get_region_or_404(config_id, region_id)
    try:
        region_service.set_overlap_override(SessionLocal(), region, iso2, request.form.get("mode", ""))
    except ValidationError as exc:
        return jsonify(error=exc.message), 422
    return jsonify(ok=True)


@bp.delete("/<int:config_id>/regions/<int:region_id>/overlap/<iso2>")
def region_reset_overlap(config_id: int, region_id: int, iso2: str):
    region_service.clear_overlap_override(SessionLocal(), _get_region_or_404(config_id, region_id), iso2)
    return jsonify(ok=True)


# --- Carving (keep only part of a country) -----------------------------------------


@bp.get("/<int:config_id>/carves")
def carves(config_id: int):
    _get_config_or_404(config_id)
    return jsonify(carve_service.kept_geojson(SessionLocal(), config_id))


@bp.post("/<int:config_id>/carves/<iso2>")
def carve_set(config_id: int, iso2: str):
    _get_config_or_404(config_id)
    try:
        carve_service.set_carve(SessionLocal(), config_id, iso2, (request.get_json(silent=True) or {}).get("geometry"))
    except ValidationError as exc:
        return jsonify(error=exc.message), 422
    return jsonify(kept=carve_service.kept_geojson(SessionLocal(), config_id).get(iso2.lower()))


@bp.delete("/<int:config_id>/carves/<iso2>")
def carve_clear(config_id: int, iso2: str):
    _get_config_or_404(config_id)
    carve_service.clear_carve(SessionLocal(), config_id, iso2)
    return jsonify(ok=True)


# --- Map style --------------------------------------------------------------------


def _style_context(config: ConfigProfile, values: dict | None = None, error: str | None = None) -> dict:
    settings = style_service.get_settings(SessionLocal(), config.id)
    if values:  # re-render of a rejected form keeps what was typed
        settings = {**settings, **values}
    return {
        "settings": settings, "error": error,
        "label_keys": style_service.LABEL_KEYS,
        "light_styles": map_styles.for_mode("light"), "dark_styles": map_styles.for_mode("dark"),
    }


def _read_labels() -> dict:
    return {k: request.form.get(f"label_{k}", "") for k in style_service.LABEL_KEYS}


@bp.post("/<int:config_id>/style/settings")
def style_settings(config_id: int):
    config = _get_config_or_404(config_id)
    form = {
        "light_style": request.form.get("light_style", ""), "dark_style": request.form.get("dark_style", ""),
        "labels": _read_labels(),
    }
    try:
        style_service.save_settings(SessionLocal(), config_id, form["light_style"], form["dark_style"], form["labels"])
    except ValidationError as exc:
        html = render_template("configure/tabs/style.html", tabs=TABS, active_tab="style", current=config,
                               **_style_context(config, {**form, "labels": {k: v for k, v in form["labels"].items() if v != ""}}, exc.message))
        return html, 422
    return render_template("configure/tabs/style.html", tabs=TABS, active_tab="style", current=config,
                           **_style_context(config))


@bp.get("/<int:config_id>/style/preview/<key>")
def style_preview(config_id: int, key: str):
    _get_config_or_404(config_id)
    if key not in map_styles.STYLES:
        abort(404)
    tiles = downloads.resolve_version(SessionLocal(), TILES_KEY)
    tiles_url = "pmtiles://" + url_for("configure.style_tiles", _external=True) if tiles else None
    assets_url = url_for("configure.static", filename="map-assets", _external=True)  # vendored glyphs and sprites
    return jsonify(style_service.preview_info(key, tiles_url, assets_url))


@bp.get("/style/tiles.pmtiles")
def style_tiles():
    """The approved Protomaps planet build for the Style preview (Range requests, read in place by the pmtiles protocol)."""
    record = downloads.resolve_version(SessionLocal(), TILES_KEY)
    path = downloads.abs_path(record) if record else None
    if path is None or not path.exists():
        abort(404)
    return send_file(path, mimetype="application/octet-stream", conditional=True, max_age=0)


def _style_download(config_id: int, mode: str):
    config = _get_config_or_404(config_id)
    session = SessionLocal()
    urls = settings_service.public_urls(session)  # global setting (default: OpenFreeMap), query args win
    urls.update({k: request.args[k] for k in ("tiles_url", "glyphs", "sprite") if request.args.get(k)})
    style = style_service.config_style(session, config_id, mode, **urls)
    return _download(json.dumps(style, indent=2), f"{config.name}-{mode}.json", "application/json")


@bp.get("/<int:config_id>/style-light.json")
def style_light(config_id: int):
    return _style_download(config_id, "light")


@bp.get("/<int:config_id>/style-dark.json")
def style_dark(config_id: int):
    return _style_download(config_id, "dark")


# --- Resolved view ----------------------------------------------------------------


@bp.post("/<int:config_id>/regions/<int:region_id>/openaddresses/<iso2>")
def region_openaddresses(config_id: int, region_id: int, iso2: str):
    region = _get_region_or_404(config_id, region_id)
    session = SessionLocal()
    try:
        region_service.set_openaddresses_file(
            session, region, iso2, request.form.get("file", ""), request.form.get("included") == "1"
        )
    except ValidationError as exc:
        return exc.message, 422
    countries = {c.iso2: c for c in session.query(Country)}
    return render_template(
        "configure/_resolved_region.html",
        region=resolve_service.resolve_region(session, region, countries),
        config_id=config_id,
    )


# --- GTFS feeds (Transit tab) ---------------------------------------------------------


def _transit_card(config_id: int, region: Region, error: str | None = None, values: dict | None = None):
    session = SessionLocal()
    countries = {c.iso2: c for c in session.query(Country)}
    html = render_template(
        "configure/_transit_region.html",
        region=resolve_service.resolve_region(session, region, countries),
        config_id=config_id,
        gtfs_error=error,
        gtfs_values=values or {},
    )
    return html, (422 if error else 200)


@bp.post("/<int:config_id>/regions/<int:region_id>/gtfs")
def region_gtfs_add(config_id: int, region_id: int):
    region = _get_region_or_404(config_id, region_id)
    values = {"url": request.form.get("url", ""), "label": request.form.get("label", "")}
    try:
        region_service.add_gtfs_feed(SessionLocal(), region, values["url"], values["label"])
    except ValidationError as exc:
        return _transit_card(config_id, region, exc.message, values)
    return _transit_card(config_id, region)


@bp.post("/<int:config_id>/regions/<int:region_id>/gtfs/<int:feed_id>/delete")
def region_gtfs_delete(config_id: int, region_id: int, feed_id: int):
    region = _get_region_or_404(config_id, region_id)
    try:
        region_service.remove_gtfs_feed(SessionLocal(), region, feed_id)
    except ValidationError:
        abort(404)
    return _transit_card(config_id, region)


def _download(body: str, filename: str, mimetype: str) -> Response:
    return Response(
        body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@bp.get("/<int:config_id>/resolved.yml")
def resolved_yaml(config_id: int):
    config = _get_config_or_404(config_id)
    data = resolve_service.to_legacy_dict(resolve_service.resolve_config(SessionLocal(), config_id))
    return _download(yaml.safe_dump(data, sort_keys=False), f"{config.name}.yml", "text/yaml")


@bp.get("/<int:config_id>/resolved.json")
def resolved_json(config_id: int):
    config = _get_config_or_404(config_id)
    data = resolve_service.to_dict(resolve_service.resolve_config(SessionLocal(), config_id))
    return _download(json.dumps(data, indent=2), f"{config.name}.json", "application/json")
