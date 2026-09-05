"""
product_db.py -- reads Battle.net's Agent product database.

The Agent tracks every installed product in a single protobuf file:
    Windows:  %PROGRAMDATA%\\Battle.net\\Agent\\product.db
    Wine:     <prefix>/drive_c/ProgramData/Battle.net/Agent/product.db

No schema ships with the client; the field layout below was recovered by
decoding a real file against the public protobuf wire-format spec (varints,
tags, length-delimited fields) -- the same approach utils.parse_appinfo()
takes for Steam's appinfo.vdf and the Ubisoft plugin's config cache. It
matches the structure the GOG Galaxy community Blizzard plugin documents:

    ProductDb        { repeated ProductInstall product_installs = 1 }
    ProductInstall   { string uid = 1; string product_code = 2;
                       UserSettings settings = 3;
                       CachedProductState cached_product_state = 4 }
    UserSettings     { string install_path = 1 }
    CachedProductState { BaseProductState base_product_state = 1 }
    BaseProductState { bool installed = 1; bool playable = 2 }

Only the handful of fields the plugin needs are pulled out; everything else
is skipped by wire type.
"""

import logging
import os

log = logging.getLogger(__name__)


def _read_varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _iter_fields(buf):
    """Yield (field_number, wire_type, value) for a protobuf message. `value`
    is an int for varint/fixed types and a bytes slice for length-delimited."""
    i, n = 0, len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
            yield fn, wt, v
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            yield fn, wt, buf[i:i + ln]
            i += ln
        elif wt == 1:
            yield fn, wt, int.from_bytes(buf[i:i + 8], 'little')
            i += 8
        elif wt == 5:
            yield fn, wt, int.from_bytes(buf[i:i + 4], 'little')
            i += 4
        else:  # groups (3/4) are obsolete and never appear here
            return


def _first(buf, field_number, wire_type=2):
    for fn, wt, v in _iter_fields(buf):
        if fn == field_number and wt == wire_type:
            return v
    return None


def parse(path):
    """Parse product.db at `path`. Returns a list of dicts:
        {uid, product_code, install_path, installed, playable}
    Empty list on any read/parse failure (missing file, client not signed in
    yet, format drift)."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return []

    out = []
    try:
        for fn, wt, entry in _iter_fields(data):
            if fn != 1 or wt != 2:
                continue
            uid = code = ''
            install_path = ''
            installed = playable = False
            for efn, ewt, ev in _iter_fields(entry):
                if efn == 1 and ewt == 2:
                    uid = ev.decode('utf-8', 'replace')
                elif efn == 2 and ewt == 2:
                    code = ev.decode('utf-8', 'replace')
                elif efn == 3 and ewt == 2:  # UserSettings
                    ip = _first(ev, 1)
                    if ip is not None:
                        install_path = ip.decode('utf-8', 'replace')
                elif efn == 4 and ewt == 2:  # CachedProductState
                    base = _first(ev, 1)
                    if base is not None:
                        for bfn, bwt, bv in _iter_fields(base):
                            if bfn == 1 and bwt == 0:
                                installed = bool(bv)
                            elif bfn == 2 and bwt == 0:
                                playable = bool(bv)
            if uid or code:
                out.append({
                    'uid': uid, 'product_code': code,
                    'install_path': install_path.replace('\\', '/'),
                    'installed': installed, 'playable': playable,
                })
    except (IndexError, ValueError) as e:
        log.warning('Battle.net product.db parse failed: %s', e)
        return []
    return out


def agent_db_path(prefix=None):
    """Absolute path to product.db for the given Wine prefix, or the native
    ProgramData location when prefix is None (Windows)."""
    if prefix:
        return os.path.join(prefix, 'drive_c', 'ProgramData', 'Battle.net',
                            'Agent', 'product.db')
    return os.path.join(os.environ.get('PROGRAMDATA', r'C:\ProgramData'),
                        'Battle.net', 'Agent', 'product.db')
