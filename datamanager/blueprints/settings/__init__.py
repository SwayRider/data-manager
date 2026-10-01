from datamanager.blueprints.nav import NavItem
from datamanager.blueprints.settings.routes import bp, tools_warning

NAV = NavItem(label="Settings", endpoint="settings.index", blueprint="settings", order=90, position="bottom",
              badge=tools_warning)

__all__ = ["bp", "NAV"]
