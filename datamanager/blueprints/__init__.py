from flask import Flask, request

from datamanager.blueprints import build, configure, countries, deploy, health, main, repo, settings
from datamanager.blueprints.nav import NavItem

# Add a new section: create its subpackage (exporting `bp` and optionally `NAV`) and list it here.
SECTIONS = (configure, build, repo, deploy, settings)
BLUEPRINTS = (main, health, countries, *SECTIONS)  # countries: no nav entry, serves the map


def register_blueprints(app: Flask) -> None:
    for module in BLUEPRINTS:
        app.register_blueprint(module.bp)

    nav_items: list[NavItem] = sorted(
        (m.NAV for m in SECTIONS if hasattr(m, "NAV")), key=lambda item: item.order
    )

    @app.context_processor
    def inject_nav():
        return {
            "nav_top": [i for i in nav_items if i.position == "top"],
            "nav_bottom": [i for i in nav_items if i.position == "bottom"],
            "active_section": request.blueprint,
            "nav_warnings": {i.endpoint: i.badge() for i in nav_items if i.badge is not None},
        }
