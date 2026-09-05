"""
watcher.py -- keeps Battle.net games' installed flags current.

Install state comes from the Agent's product.db (see product_db.py). The
Agent rewrites that file (and the rest of its data/ dir) on every
install/uninstall/update, so a filesystem watch on the Agent directory is
enough -- no polling.
"""

# recursive + file events: product.db is a *file* the Agent rewrites in
# place under Agent/ (an uninstall just removes that game's entry from it,
# no directory create/delete at all) -- PluginInstallWatcher's default
# directory-events-only config never sees that, so an uninstall left the
# game stuck 'installed' until the next full "Sync Library" (confirmed live
# 2026-09-04, same class of bug EA's Cleanup.exe hit -- see
# ea_app/watcher.py). recursive covers the per-product Agent.<pid>/
# subdirectories too, not just the top-level product.db. debounce coalesces
# the write burst an install/uninstall produces into one resync.

import json
import logging
import sys

from runners.watcher import PluginInstallWatcher

log = logging.getLogger(__name__)


def _get_wine_prefix():
    try:
        from config import CONFIG_PATH
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        return (cfg.get('launchers', {}).get('battle_net', {}).get('prefix') or '').strip() or None
    except Exception:
        return None


def sync_bnet_install_status():
    """Recompute installed flags for all Battle.net games from product.db."""
    from .battle_net import resync_installed
    if sys.platform == 'win32' or _get_wine_prefix():
        resync_installed()


_watcher = PluginInstallWatcher(
    'battle_net', sync_bnet_install_status,
    recursive=True, watch_files=True, debounce_seconds=1.5,
)


def start_bnet_watcher(watch_path: str):
    _watcher.start(watch_path)


def stop_bnet_watcher():
    _watcher.stop()
