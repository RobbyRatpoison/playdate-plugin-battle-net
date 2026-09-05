"""
local_sync.py -- derives the Battle.net library from the client's own local
files, no account API access.

Two independent signals are combined:

  1. product.db (product_db.parse)        -> games actually installed, + path
  2. CachedData.db 'features_cached_data_points' -> the account's license ids,
     cross-referenced against the per-product catalog fragments the client
     caches under <prefix>/.../Battle.net/Cache/ to turn a license id into a
     program id (= catalog `code`).

Signal 2 covers owned-but-not-installed *paid* games. It leans on a cache
Blizzard populates for feature targeting rather than a real library manifest,
so it can be incomplete on a very large account -- the optional signed-in
sync (games-and-subs) is the authoritative fallback. Free-to-play titles have
no license and Battle.net lists them for everyone, so they're only surfaced
here when product.db shows them installed.

The catalog-fragment format was read straight out of the live cache: each is
a JSON blob with `fragment_id`, a `products` list carrying `base.program_id`,
and `program_configuration` / `installs` sections whose rules match on
`license_id` (int or list) and `requires_licenses`.
"""

import glob
import json
import logging
import os
import sqlite3

from . import catalog
from . import product_db

log = logging.getLogger(__name__)


def _cached_data_db(prefix):
    return os.path.join(prefix, 'drive_c', 'users', 'steamuser', 'AppData',
                        'Local', 'Battle.net', 'CachedData.db')


def _cache_dir(prefix):
    return os.path.join(prefix, 'drive_c', 'users', 'steamuser', 'AppData',
                        'Local', 'Battle.net', 'Cache')


def read_owned_license_ids(prefix):
    """License ids for the signed-in account, from CachedData.db. Empty set if
    the file is missing or the key isn't populated yet."""
    db_path = _cached_data_db(prefix)
    if not os.path.isfile(db_path):
        return set()
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT value FROM key_value_store WHERE key = ?",
                ('features_cached_data_points',),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning('Battle.net: CachedData.db read failed: %s', e)
        return set()
    if not row:
        return set()
    try:
        return {int(x) for x in (json.loads(row[0]).get('licenses') or [])}
    except (ValueError, TypeError, json.JSONDecodeError):
        return set()


def _walk_license_ids(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ('license_id', 'requires_licenses'):
                if isinstance(v, int):
                    out.add(v)
                elif isinstance(v, list):
                    out.update(x for x in v if isinstance(x, int))
            else:
                _walk_license_ids(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_license_ids(x, out)


def build_license_index(prefix):
    """Map each program id (catalog `code`) to the set of license ids that
    grant it, by scanning every cached catalog fragment in the prefix."""
    index = {}
    for fp in glob.glob(os.path.join(_cache_dir(prefix), '*', '*', '*')):
        try:
            with open(fp, 'rb') as f:
                head = f.read(1)
                if head != b'{':
                    continue
                f.seek(0)
                frag = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(frag, dict) or 'fragment_id' not in frag:
            continue
        program_id = None
        for p in frag.get('products') or []:
            program_id = (p.get('base') or {}).get('program_id')
            if program_id:
                break
        if not program_id:
            continue
        lic = set()
        _walk_license_ids(frag.get('program_configuration'), lic)
        _walk_license_ids(frag.get('installs'), lic)
        if lic:
            index.setdefault(program_id, set()).update(lic)
    return index


def scan(prefix):
    """Return the local Battle.net library as a list of dicts:
        {code, uid, name, genres, installed, install_path, source}
    `source` is 'installed', 'license', or 'both'. Unknown product codes seen
    in product.db are still returned (name = the raw code) so nothing an
    account owns silently vanishes."""
    if not prefix or not os.path.isdir(prefix):
        return []

    games = {}  # code -> dict

    # ---- signal 1: installed products ---------------------------------------
    for row in product_db.parse(product_db.agent_db_path(prefix)):
        if row['product_code'] in ('agent', 'bna'):
            continue
        game = catalog.by_uid(row['uid']) or catalog.by_code(row['product_code'])
        code = game['code'] if game else (row['product_code'] or row['uid'])
        games[code] = {
            'code': code,
            'uid': game['uid'] if game else row['uid'],
            'name': game['name'] if game else code,
            'genres': game['genres'] if game else '',
            'installed': bool(row['installed'] or row['playable']),
            'install_path': row['install_path'],
            'source': 'installed',
        }

    # ---- signal 2: owned licenses -> paid games ----------------------------
    owned_licenses = read_owned_license_ids(prefix)
    if owned_licenses:
        lic_index = build_license_index(prefix)
        for code, lic_ids in lic_index.items():
            if not (owned_licenses & lic_ids):
                continue
            # A license match is proof of a real entitlement for most titles,
            # so free-to-play ones count here too (e.g. an original-Overwatch
            # purchase that predates free Overwatch 2). The exception is
            # `lic_ok = False` titles -- every modern Call of Duty, where a
            # free-access weekend or free Warzone grants a retail-looking
            # license the local data can't tell from a purchase. Those only
            # come in when product.db shows them installed.
            game = catalog.by_code(code)
            if not game:
                continue
            if code in games:
                games[code]['source'] = 'both'
            elif not game['lic_ok']:
                continue
            else:
                games[code] = {
                    'code': code, 'uid': game['uid'], 'name': game['name'],
                    'genres': game['genres'], 'installed': False,
                    'install_path': '', 'source': 'license',
                }

    return list(games.values())
