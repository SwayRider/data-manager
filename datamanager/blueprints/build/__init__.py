from datamanager.blueprints.build.routes import bp
from datamanager.blueprints.nav import NavItem

NAV = NavItem(label="Build", endpoint="build.index", blueprint="build", order=20, position="top")

__all__ = ["bp", "NAV"]
