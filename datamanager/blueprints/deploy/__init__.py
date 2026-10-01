from datamanager.blueprints.deploy.routes import bp
from datamanager.blueprints.nav import NavItem

NAV = NavItem(label="Deploy", endpoint="deploy.index", blueprint="deploy", order=40, position="top")

__all__ = ["bp", "NAV"]
