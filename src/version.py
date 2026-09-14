"""Build identity: version and repo URL, shared by the GUI and the CLI updater.

Kept in its own GTK-free module so the `echotray upgrade` CLI can read the
version and repo URL without importing the GTK app (which needs a display and
the GTK bindings).
"""

__version__ = "2.5.2"
REPO_URL = "https://github.com/rebelcommand/echotray"
