"""
account.py -- optional signed-in library sync via account.battle.net.

Blizzard's public API has no owned-games endpoint (see local_sync.py's
docstring), so this doesn't use an API key or OAuth client at all -- it
follows Playnite's BattleNetLibrary and the GOG Galaxy community Blizzard
plugin instead: capture the account.battle.net *session cookies* from a real
login, then replay them with plain requests against the same JSON endpoints
the client's own Phoenix UI calls.

    GET  /api/logout                              -- initial popup URL; clears
                                                      any stale session so a
                                                      protected page redirects
                                                      into Blizzard's real login
    GET  /oauth2/authorization/account-settings    -- refreshes the auth cookie
    GET  /api/games-and-subs                       -- {"gameAccounts": [...]}
    GET  /api/                                     -- {"authenticated": bool, ...}
    GET  /api/details                              -- best-effort battletag

The account.battle.net session cookies (BA-*) are HttpOnly, so capturing them
needs main.py's open_auth_popup in its whole-cookie-jar mode (cookie_name='*'
-- document.cookie can't see them, only the native WebKit/WebView2 cookie
store can). See __init__.py's manage_ui() for how the popup is wired up.

This is the authoritative source for what's actually owned -- unlike the
local product.db/CachedData.db scan (local_sync.py), it distinguishes a real
purchase from a free-access license, which the local data structurally can't
(see catalog.py's `lic_ok`). Classic (non-Agent) titles -- Diablo II, Warcraft
III's pre-Reforged classic mode -- aren't covered: they need a different
install/launch mechanism entirely, matching local_sync's existing gap.
"""

import json
import logging

import requests

from config import load_config, _save_config_data

from . import catalog

log = logging.getLogger(__name__)

BASE = 'https://account.battle.net'
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


def get_auth_url():
    return f'{BASE}/api/logout'


def _load():
    return (load_config() or {}).get('battle_net_web') or {}


def is_connected():
    return bool(_load().get('cookies'))


def get_display_name():
    return _load().get('battletag') or None


def clear():
    cfg = load_config() or {}
    cfg.pop('battle_net_web', None)
    _save_config_data(cfg)


def save_cookies(raw_json):
    """raw_json: the JSON-encoded {cookie_name: value} dict captured by the
    popup's whole-jar mode. Returns (True, display_name) or (False, message)."""
    try:
        cookies = json.loads(raw_json)
    except (ValueError, TypeError):
        # Never log raw_json itself -- if this path is ever hit with a real
        # cookie value in transit (vs. the unrelated URL code bug this was
        # written to catch) it would leak a live session token into a log
        # file that gets submitted to Discord for support triage.
        log.warning('Battle.net account: callback payload was not valid JSON (%d chars)',
                    len(raw_json))
        return False, 'Could not read the login session'
    if not isinstance(cookies, dict) or not cookies:
        log.warning('Battle.net account: callback payload had no cookies')
        return False, 'No cookies captured -- try logging in again'
    log.info('Battle.net account: callback received %d cookies: %s',
             len(cookies), sorted(cookies.keys()))

    cfg = load_config() or {}
    cfg['battle_net_web'] = {'cookies': cookies}
    _save_config_data(cfg)

    session = get_valid_session()
    battletag = _fetch_battletag(session) if session else None
    if battletag:
        cfg = load_config() or {}
        cfg.setdefault('battle_net_web', {})['battletag'] = battletag
        _save_config_data(cfg)
    log.info('Battle.net account: connected (%d cookies captured)', len(cookies))
    return True, battletag or 'Battle.net Account'


def get_valid_session():
    """requests.Session replaying the saved cookies, or None if not connected."""
    cookies = _load().get('cookies')
    if not cookies:
        return None
    session = requests.Session()
    session.headers['User-Agent'] = _UA
    for name, value in cookies.items():
        session.cookies.set(name, value, domain='.battle.net')
    try:
        # Refreshes the auth cookie -- both Playnite and the GOG Galaxy
        # community plugin do this before every games-and-subs call.
        session.get(f'{BASE}/oauth2/authorization/account-settings', timeout=15)
    except requests.RequestException as e:
        log.warning('Battle.net account: warm-up request failed: %s', e)
    return session


def check_authenticated(session):
    try:
        resp = session.get(f'{BASE}/api/', timeout=10)
        return bool(resp.json().get('authenticated'))
    except (requests.RequestException, ValueError):
        return False


def _fetch_battletag(session):
    try:
        resp = session.get(f'{BASE}/api/details', timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ('battleTag', 'battletag', 'battleTagName'):
        if data.get(key):
            return data[key]
    return None


def fetch_owned_title_ids(session):
    """Numeric titleIds the account owns. Empty set on any failure (expired
    session, network error, response shape drift) -- callers must treat that
    as 'nothing to add', never as 'account owns nothing'."""
    try:
        resp = session.get(f'{BASE}/api/games-and-subs', timeout=20)
        if resp.status_code != 200:
            log.warning('Battle.net account: games-and-subs HTTP %d', resp.status_code)
            return set()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning('Battle.net account: games-and-subs failed: %s', e)
        return set()
    return {acc['titleId'] for acc in (data.get('gameAccounts') or [])
            if isinstance(acc.get('titleId'), int)}


def fetch_purchase_dates(session):
    """{catalog code: unix timestamp} for every real money purchase on the
    account, from account.battle.net/api/transactions. Only actual
    transactions appear here -- free-access grants and F2P titles have no
    entry (confirmed live: a Call of Duty free-access weekend and Warzone
    show up as an owned license/game account but never as a transaction),
    which is itself a stronger ownership signal than the local license scan
    can produce (see catalog.py's `lic_ok`). The endpoint carries no
    titleId/code, only a free-text productTitle, matched via
    catalog.by_name(). Empty dict on any failure -- callers must treat that
    as 'no dates known', never as 'nothing was purchased'."""
    try:
        resp = session.get(f'{BASE}/api/transactions', timeout=20)
        if resp.status_code != 200:
            log.warning('Battle.net account: transactions HTTP %d', resp.status_code)
            return {}
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning('Battle.net account: transactions failed: %s', e)
        return {}

    out = {}
    for p in data.get('purchases') or []:
        title  = (p.get('productTitle') or '').strip()
        date_ms = p.get('date')
        if not title or not isinstance(date_ms, int):
            continue
        game = catalog.by_name(title)
        if not game:
            continue
        ts = date_ms // 1000
        # Earliest purchase wins if a title appears more than once (e.g. a
        # base game and a later re-purchase/gift under the same display name).
        if game['code'] not in out or ts < out[game['code']]:
            out[game['code']] = ts
    return out


def scan_owned(session):
    """Library entries in the same shape as local_sync.scan()'s, for every
    title id games-and-subs returns that this plugin's catalog recognizes.
    Entries with a real purchase date (fetch_purchase_dates) carry
    'date_added'; battle_net.py uses that to both set it on a new row and
    correct it on one already synced with a guessed date."""
    purchase_dates = fetch_purchase_dates(session)
    games = []
    for tid in fetch_owned_title_ids(session):
        game = catalog.by_title_id(tid)
        if not game:
            log.info('Battle.net account: unrecognized title id %s -- add it to catalog.py', tid)
            continue
        entry = {
            'code': game['code'], 'uid': game['uid'], 'name': game['name'],
            'genres': game['genres'], 'installed': False, 'install_path': '',
            'source': 'account',
        }
        ts = purchase_dates.get(game['code'])
        if ts:
            entry['date_added'] = ts
        games.append(entry)
    return games
