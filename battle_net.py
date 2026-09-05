"""
battle_net.py -- Battle.net integration for PlayDate.

Blizzard publishes no public/documented owned-library API. The library is
built from local files by default -- product.db for installed games, plus
the account's cached license ids resolved against the client's catalog
fragments for owned-but-not-installed paid games (local_sync.py) -- no login
required. When the optional account.battle.net sign-in (account.py) is
connected, its games-and-subs/transactions data (an undocumented endpoint the
account.battle.net website itself calls, not a developer API) is merged in
on top: it's the authoritative source, so it resolves what the local license
heuristic can't (see catalog.py's `lic_ok`) and supplies real purchase dates.

Launch/install go through the Wine-installed Battle.net launcher.
"""

import logging
import os
import sys
import threading
from datetime import date, datetime, timezone

from config import BASE_DIR, load_config
from database import next_negative_appid, update_game_data

from . import account
from . import catalog
from . import local_sync

log = logging.getLogger(__name__)

VERTICAL_DIR   = os.path.join(BASE_DIR, 'static', 'img', 'library', 'vertical')
HORIZONTAL_DIR = os.path.join(BASE_DIR, 'static', 'img', 'library', 'horizontal')


# ── Launcher config ──────────────────────────────────────────────────────────

def get_prefix():
    """Wine prefix Battle.net is installed in, or '' if not configured.
    Always '' on native Windows (no prefix -- the client is installed directly)."""
    cfg = (load_config() or {}).get('launchers', {}).get('battle_net', {})
    return (cfg.get('prefix') or '').strip()


def is_configured():
    if sys.platform == 'win32':
        from . import product_db
        return os.path.isfile(product_db.agent_db_path(None))
    return bool(get_prefix()) and os.path.isdir(get_prefix())


def _agent_db_path():
    from . import product_db
    return product_db.agent_db_path(None if sys.platform == 'win32' else get_prefix())


# ── Library sync ─────────────────────────────────────────────────────────────

_sync_state = {
    'running': False, 'phase': None, 'done': 0, 'total': 0, 'current_game': '',
    'new_games': 0, 'total_games': 0, 'duplicates_detected': 0, 'error': None,
}
_sync_lock   = threading.Lock()
_sync_cancel = threading.Event()


def get_sync_state():
    with _sync_lock:
        return dict(_sync_state)


def start_library_sync():
    with _sync_lock:
        if _sync_state['running']:
            return {'status': 'already_running'}
        _sync_cancel.clear()
        _sync_state.update({
            'running': True, 'phase': 'scanning', 'done': 0, 'total': 0,
            'current_game': '', 'new_games': 0, 'total_games': 0,
            'duplicates_detected': 0, 'error': None,
        })
    threading.Thread(target=_run_sync, daemon=True).start()
    return {'status': 'started'}


def cancel_library_sync():
    _sync_cancel.set()


def _run_sync():
    try:
        _do_sync()
    except Exception as e:
        log.error('Battle.net library sync thread: %s', e, exc_info=True)
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error', 'error': str(e)})


def _do_sync():
    from database import get_db

    if not is_configured():
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error',
                                'error': 'Battle.net is not set up. Open Plugins -> Manage to install it.'})
        return

    # Authoritative from product.db for every game already in the library,
    # not just ones the scan below happens to rediscover -- otherwise a game
    # that drops out of the scan entirely (uninstalled, and not owned via a
    # paid license/connected account) never gets its stale installed=1
    # cleared by a manual sync, only by the filesystem watcher catching the
    # change live (confirmed live 2026-09-04: Hearthstone stayed 'installed'
    # after a real uninstall until this was added).
    resync_installed()

    if sys.platform == 'win32':
        found = _scan_installed_only()
    else:
        found = local_sync.scan(get_prefix())
    log.info('Battle.net sync: %d games from local files', len(found))

    if account.is_connected():
        session = account.get_valid_session()
        if session:
            acct_games = account.scan_owned(session)
            found = _merge_account(found, acct_games)
            log.info('Battle.net sync: %d games after merging account data (+%d from account)',
                     len(found), len(acct_games))
        else:
            log.warning('Battle.net sync: account connected but session build failed -- '
                        'using local scan only')

    db = get_db()
    try:
        existing = {row['platform_id']: dict(row) for row in db.execute(
            "SELECT appid, platform_id, installed, install_path FROM games WHERE platform = 'battle_net'"
        ).fetchall()}
        blacklisted = {
            row[0] for row in db.execute(
                "SELECT platform_id FROM blacklist WHERE platform_id IS NOT NULL"
            ).fetchall()
        }
    finally:
        db.close()

    new_items = [g for g in found
                 if g['code'] not in existing and g['code'] not in blacklisted]
    total_new = len(new_items)

    with _sync_lock:
        _sync_state.update({'phase': 'processing', 'done': 0, 'total': total_new})

    today_ts        = int(datetime.now(timezone.utc).timestamp())
    today           = date.today().isoformat()
    new_games_count = 0

    db = get_db()
    try:
        # refresh installed-state / paths on games we already have, and
        # correct date_added when the account sync found a real purchase
        # date for one (only ever overwritten with a *confirmed* date --
        # never a guess -- so this can only fix a wrong sync-time default,
        # not clobber something meaningful)
        for g in found:
            row = existing.get(g['code'])
            if not row:
                continue
            if g.get('date_added'):
                db.execute(
                    "UPDATE games SET installed = ?, install_path = ?, date_added = ? WHERE appid = ?",
                    (1 if g['installed'] else 0, g['install_path'] or row.get('install_path'),
                     g['date_added'], row['appid']),
                )
            else:
                db.execute(
                    "UPDATE games SET installed = ?, install_path = ? WHERE appid = ?",
                    (1 if g['installed'] else 0, g['install_path'] or row.get('install_path'),
                     row['appid']),
                )
        db.commit()

        for g in new_items:
            if _sync_cancel.is_set():
                break
            with _sync_lock:
                _sync_state['current_game'] = g['name']

            appid = next_negative_appid(db)
            db.execute(
                """INSERT OR IGNORE INTO games
                   (appid, name, platform, platform_id, date_added,
                    completion_status, installed, install_path,
                    art_fetched, meta_fetched, cheevos_fetched,
                    protondb_fetched, hltb_fetched)
                   VALUES (?, ?, 'battle_net', ?, ?, 'Never Played', ?, ?,
                           '0', '0', '0', '0', '0')""",
                (appid, g['name'], g['code'], g.get('date_added', today_ts),
                 1 if g['installed'] else 0, g['install_path']),
            )
            db.commit()

            meta = _build_metadata(g['code'])
            meta['meta_fetched'] = today
            try:
                update_game_data(appid, **meta)
            except Exception as e:
                log.warning('Battle.net metadata update failed for %r: %s', g['name'], e)

            _fetch_art(appid, g['name'])
            log.info('Battle.net sync: added %r as appid %d (%s, %s)',
                     g['name'], appid, g['code'], g['source'])
            new_games_count += 1
            with _sync_lock:
                _sync_state['done'] += 1
    finally:
        db.close()

    if _sync_cancel.is_set():
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'stopped',
                                'new_games': new_games_count, 'total_games': len(found)})
        return

    from database import auto_detect_duplicates
    from plugins import get_platform_priority
    dupes = auto_detect_duplicates(platform_priority=get_platform_priority())

    with _sync_lock:
        _sync_state.update({
            'running': False, 'phase': 'done',
            'new_games': new_games_count, 'total_games': len(found),
            'duplicates_detected': dupes,
        })


def _installed_by_code():
    """{code: (installed_bool, install_path)} from product.db."""
    from . import product_db
    out = {}
    for row in product_db.parse(_agent_db_path()):
        if row['product_code'] in ('agent', 'bna'):
            continue
        game = catalog.by_uid(row['uid']) or catalog.by_code(row['product_code'])
        code = game['code'] if game else (row['product_code'] or row['uid'])
        out[code] = (bool(row['installed'] or row['playable']), row['install_path'])
    return out


def _scan_installed_only():
    """Library entries for installed games only (Windows / no-prefix path)."""
    found = []
    for code, (is_inst, path) in _installed_by_code().items():
        game = catalog.by_code(code)
        found.append({
            'code': code, 'uid': game['uid'] if game else code,
            'name': game['name'] if game else code,
            'genres': game['genres'] if game else '',
            'installed': is_inst, 'install_path': path, 'source': 'installed',
        })
    return found


def _merge_account(local_games, account_games):
    """Union by code. Local data (install state/path) always wins for a game
    both sides found; an account-only game is added with installed=False.
    date_added only ever comes from the account side (local_sync has no
    concept of it), so it's copied across whenever the account found one."""
    by_code = {g['code']: dict(g) for g in local_games}
    for g in account_games:
        if g['code'] in by_code:
            if by_code[g['code']]['source'] != 'installed':
                by_code[g['code']]['source'] = 'account'
            if g.get('date_added'):
                by_code[g['code']]['date_added'] = g['date_added']
        else:
            by_code[g['code']] = g
    return list(by_code.values())


def resync_installed():
    """Refresh installed flags / paths for every Battle.net game from
    product.db. Called by bulk rescrape, restore, and the install watcher."""
    from database import get_db

    if not is_configured():
        return
    installed = _installed_by_code()

    db = get_db()
    try:
        rows = db.execute(
            "SELECT appid, platform_id FROM games WHERE platform = 'battle_net'"
        ).fetchall()
        for r in rows:
            is_inst, path = installed.get(r['platform_id'], (False, ''))
            db.execute(
                "UPDATE games SET installed = ?, install_path = COALESCE(NULLIF(?, ''), install_path) WHERE appid = ?",
                (1 if is_inst else 0, path, r['appid']),
            )
        db.commit()
    finally:
        db.close()


# ── Metadata / art ───────────────────────────────────────────────────────────

def _build_metadata(code):
    game = catalog.by_code(code)
    meta = {'publishers': catalog.PUBLISHER, 'developers': catalog.PUBLISHER}
    if game and game['genres']:
        meta['genres'] = game['genres']
    return meta


def _fetch_art(appid, name):
    try:
        from images import _sgdb_search_game_id, download_vertical, download_horizontal
        sgdb_id = _sgdb_search_game_id(name)
        vert    = download_vertical(appid, sgdb_id=sgdb_id, game_name=name)
        horiz   = download_horizontal(appid, sgdb_id=sgdb_id, game_name=name)
        if vert != 'missing' or horiz != 'missing':
            update_game_data(appid, art_fetched=date.today().isoformat())
            log.info('Battle.net: art fetched for %r', name)
    except Exception as e:
        log.warning('Battle.net: art fetch failed for %r: %s', name, e)


def scrape_single(appid):
    """Rebuild metadata + re-fetch art for one Battle.net game."""
    from database import get_db
    db  = get_db()
    row = db.execute(
        "SELECT platform_id, name FROM games WHERE appid = ? AND platform = 'battle_net'",
        (appid,),
    ).fetchone()
    db.close()
    if not row or not row['platform_id']:
        return None

    meta = _build_metadata(row['platform_id'])
    meta['meta_fetched'] = date.today().isoformat()
    game = catalog.by_code(row['platform_id'])
    _fetch_art(appid, row['name'] or (game['name'] if game else row['platform_id']))
    return meta


def fetch_description(appid, platform_id):
    """No public Battle.net description API -- fall through to Steam."""
    return None
