"""
catalog.py -- static Battle.net product catalog.

Blizzard publishes no public/documented owned-library API -- account.py's
games-and-subs/transactions sync uses undocumented endpoints the
account.battle.net website itself calls, reachable only via a signed-in
session, not a developer API key -- and the client caches no owned-library
file locally either. Neither path carries everything PlayDate needs on its
own: games-and-subs returns a title id and a display name but no genres, no
`uid`/`code` for local matching or launching; transactions returns only a
free-text product name, no title id at all; the local product.db/license
scan has no names or metadata whatsoever. So every game PlayDate can name
has to be known ahead of time regardless of which sync path is used. This
table maps the identifiers the plugin needs onto each other:

  code   -- Battle.net "program id" / TACT product id (e.g. "Pro", "WoW",
            "VIPR"). Used as the PlayDate platform_id, and as the argument to
            `Battle.net.exe --exec="launch <code>"`.
  uid    -- lowercase internal name (e.g. "prometheus", "wow", "viper"). This
            is what product.db records as a product_install's uid and what the
            client's per-product catalog fragment is named after, so it's the
            key both local-library passes match on. Also the `--game=<uid>`
            install argument.
  title_id -- numeric title id from account.battle.net/api/games-and-subs
            (the optional signed-in sync). NULL for entries never seen there.

`free` marks the free-to-play titles Battle.net lists for every account
regardless of ownership -- the local sync only imports those when product.db
shows them actually installed.

`lic_ok` (default True) is False for titles whose local license signal can't
be trusted as a purchase: every modern Call of Duty has had free-access
weekends and rides alongside free Warzone, and Blizzard grants those as real
retail-level licenses that look identical to a purchase in the local data
(`features_cached_data_points.licenses` is a bare id list with no
active/trial/owned flag). Those are only imported when actually installed;
the signed-in games-and-subs sync is the authoritative check.

Ported from Playnite's BattleNetLibrary game table, cross-checked against the
189 product fragments the live client caches under
<prefix>/.../Battle.net/Cache/.
"""

import re

# code, uid, title_id, name, genres, free, lic_ok
_ROWS = [
    ("WoW",  "wow",        5730135,    "World of Warcraft",                              "MMO,RPG",                 False, True),
    ("WoWC", "wow_classic",None,       "World of Warcraft Classic",                      "MMO,RPG",                 False, True),
    ("D3",   "diablo3",    17459,      "Diablo III",                                     "Action,RPG",              False, True),
    ("OSI",  "osi",        5198665,    "Diablo II: Resurrected",                          "Action,RPG",              False, True),
    ("D1",   "diablo_i",   1146246220, "Diablo",                                          "Action,RPG",              False, True),
    ("Fen",  "fenris",     4613486,    "Diablo IV",                                       "Action,RPG",              False, True),
    ("ANBS", "anbs",       1095647827, "Diablo Immortal",                                "Action,RPG",              True,  True),
    ("S2",   "s2",         21298,      "StarCraft II",                                    "Strategy,RTS",            True,  True),
    ("S1",   "s1",         21297,      "StarCraft: Remastered",                          "Strategy,RTS",            False, True),
    ("SC",   "starcraft",  None,       "StarCraft Anthology",                            "Strategy,RTS",            False, True),
    ("W3",   "w3",         22323,      "Warcraft III: Reforged",                         "Strategy,RTS",            False, True),
    ("W1",   "warcraft_i", 1463898673, "Warcraft: Orcs & Humans",                        "Strategy,RTS",            False, True),
    ("W2",   "warcraft_ii",1462911566, "Warcraft II: Battle.net Edition",                "Strategy,RTS",            False, True),
    ("W1R",  "warcraft_i_remastered",  5714258,  "Warcraft: Remastered",                 "Strategy,RTS",            False, True),
    ("W2R",  "warcraft_ii_remastered", 5714514,  "Warcraft II: Remastered",              "Strategy,RTS",            False, True),
    ("GRY",  "gryphon",    4674137,    "Warcraft Rumble",                                 "Strategy,Mobile",         True,  True),
    ("WTCG", "hs_beta",    1465140039, "Hearthstone",                                     "Card Game,Strategy",      True,  True),
    ("Hero", "heroes",     1214607983, "Heroes of the Storm",                             "MOBA,Action",             True,  True),
    ("Pro",  "prometheus", 5272175,    "Overwatch",                                       "Action,FPS,Multiplayer",  True,  True),
    ("VIPR", "viper",      1447645266, "Call of Duty: Black Ops 4",                       "Action,FPS,Multiplayer",  False, False),
    ("ODIN", "odin",       1329875278, "Call of Duty: Modern Warfare",                    "Action,FPS,Multiplayer",  False, False),
    ("LAZR", "lazarus",    1279351378, "Call of Duty: Modern Warfare 2 Campaign Remastered", "Action,FPS",           False, False),
    ("FORE", "fore",       1179603525, "Call of Duty: Vanguard",                          "Action,FPS,Multiplayer",  False, False),
    ("ZEUS", "zeus",       1514493267, "Call of Duty: Black Ops Cold War",                "Action,FPS,Multiplayer",  False, False),
    ("AUKS", "auks",       1096108883, "Call of Duty: Modern Warfare II",                 "Action,FPS,Multiplayer",  False, False),
    ("PNTA", "pinta",      None,       "Call of Duty: Modern Warfare III",                "Action,FPS,Multiplayer",  False, False),
    ("WLBY", "wlby",       1464615513, "Crash Bandicoot 4: It's About Time",             "Platformer",              False, True),
    ("RTRO", "rtro",       1381257807, "Blizzard Arcade Collection",                      "Arcade",                  False, True),
    ("ARIS", "aris",       1095911763, "Doom: The Dark Ages",                             "Action,FPS",              False, True),
    ("SCOR", "scorpio",    1396920146, "Sea of Thieves",                                  "Action,Adventure",        False, True),
    ("ARK",  "arkansas",   4280907,    "The Outer Worlds 2",                             "RPG,Action",              False, True),
    ("LBRA", "libra",      1279414849, "Tony Hawk's Pro Skater 3 + 4",                   "Sports",                  False, True),
    ("AQUA", "aqua",       1095849281, "Avowed",                                          "RPG,Action",              False, True),
]

PUBLISHER = "Blizzard Entertainment"

GAMES = [
    {'code': c, 'uid': u, 'title_id': t, 'name': n, 'genres': g, 'free': f, 'lic_ok': l}
    for (c, u, t, n, g, f, l) in _ROWS
]

_BY_CODE     = {r['code'].lower(): r for r in GAMES}
_BY_UID      = {r['uid'].lower(): r for r in GAMES}
_BY_TITLE_ID = {r['title_id']: r for r in GAMES if r['title_id']}


def by_code(code):
    return _BY_CODE.get((code or '').lower())


def by_uid(uid):
    return _BY_UID.get((uid or '').lower())


def by_title_id(title_id):
    try:
        return _BY_TITLE_ID.get(int(title_id))
    except (TypeError, ValueError):
        return None


def resolve(token):
    """Best-effort lookup by any identifier (code, uid, or numeric title id)."""
    return by_code(token) or by_uid(token) or by_title_id(token)


_TRADEMARK_RE = re.compile(r'[®™©]')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')


def normalize_name(name):
    """Casefold a display name for loose matching -- strips trademark symbols
    and all non-alphanumerics, so 'Overwatch®' and 'OVERWATCH' both
    collapse to 'overwatch'. Used to match account.battle.net's free-text
    productTitle (from /api/transactions) back to a catalog entry, since that
    endpoint carries no titleId/code, only a display name."""
    name = _TRADEMARK_RE.sub('', name or '')
    return _NON_ALNUM_RE.sub('', name.lower())


_BY_NORM_NAME = {normalize_name(r['name']): r for r in GAMES}


def by_name(name):
    return _BY_NORM_NAME.get(normalize_name(name))
