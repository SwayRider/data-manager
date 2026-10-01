from datamanager.blueprints.configure.routes import bp
from datamanager.blueprints.nav import NavItem

NAV = NavItem(label="Configure", endpoint="configure.index", blueprint="configure", order=10)

__all__ = ["bp", "NAV"]
