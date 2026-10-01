from datamanager.blueprints.repo.routes import bp
from datamanager.blueprints.nav import NavItem

NAV = NavItem(label="Repo", endpoint="repo.index", blueprint="repo", order=30, position="top")

__all__ = ["bp", "NAV"]
