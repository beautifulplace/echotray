"""Build identity: version and repo URL, shared by the GUI and the CLI updater.

Kept in its own GTK-free module so the `echotray upgrade` CLI can read the
version and repo URL without importing the GTK app (which needs a display and
the GTK bindings). The Gitea build points at Gitea (5.x line), the GitHub build
at GitHub (2.x line) — each build checks its own version line.
"""

__version__ = "2.4.0"
REPO_URL = "https://github.com/beautifulplace/echotray"
