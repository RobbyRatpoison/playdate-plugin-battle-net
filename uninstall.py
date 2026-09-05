"""
uninstall.py -- runs Blizzard's own per-game uninstaller inside the Wine prefix.

Battle.net has no uninstall protocol URL or CLI verb of its own. Each installed
game registers a standard Windows uninstall entry, e.g.

  [HKLM\\...\\Uninstall\\Overwatch]
  Publisher      = "Blizzard Entertainment"
  InstallLocation= "C:\\Program Files (x86)\\Overwatch"
  UninstallString= "\"C:\\ProgramData\\Battle.net\\Agent\\Blizzard Uninstaller.exe\"
                    --lang=enUS --uid=prometheus --displayname=\"Overwatch\""

so uninstall = find that entry (matched to the game by install path, then
display name) and run its UninstallString through Wine. The Blizzard
Uninstaller shows a small confirmation dialog; there's no silent switch that
is reliable across titles, so this is a user-completes-the-window action, the
same shape as the launcher install.

Windows uses the same registry entry directly.
"""

import logging
import os
import re

log = logging.getLogger(__name__)

_HEADER_RE = re.compile(r'^\[(.+?)\]')
_VALUE_RE  = re.compile(r'^"(.*?)"\s*=\s*(.*)$')


def _unescape(s):
    return s.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')


def _read_reg(path, sections):
    if not os.path.isfile(path):
        return
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            cur = None
            for line in f:
                line = line.strip()
                if not line or line[0] in ';#':
                    continue
                m = _HEADER_RE.match(line)
                if m:
                    key = _unescape(m.group(1))
                    cur = sections.setdefault(key, {}) if 'Uninstall\\' in key else None
                    continue
                if cur is None:
                    continue
                m = _VALUE_RE.match(line)
                if m:
                    val = m.group(2).strip()
                    if val.startswith('"') and val.endswith('"'):
                        val = _unescape(val[1:-1])
                    cur[_unescape(m.group(1))] = val
    except OSError as e:
        log.warning('Battle.net uninstall: reg read failed for %s: %s', path, e)


def _blizzard_uninstall_entries(prefix):
    sections = {}
    _read_reg(os.path.join(prefix, 'system.reg'), sections)
    _read_reg(os.path.join(prefix, 'user.reg'), sections)
    out = []
    for key, vals in sections.items():
        us = vals.get('UninstallString', '')
        if not us:
            continue
        if (vals.get('Publisher') == 'Blizzard Entertainment'
                or 'Blizzard Uninstaller' in us or 'Battle.net.exe' in us):
            out.append(vals)
    return out


def _pick_entry(entries, install_path='', display_name=''):
    want_loc  = (install_path or '').replace('\\', '/').rstrip('/').lower()
    want_name = (display_name or '').strip().lower()
    for e in entries:
        loc = (e.get('InstallLocation', '')).replace('\\', '/').rstrip('/').lower()
        if want_loc and loc and (loc == want_loc or loc.endswith(want_loc) or want_loc.endswith(loc)):
            return e
    if want_name:
        for e in entries:
            if e.get('DisplayName', '').strip().lower() == want_name:
                return e
    return None


def _split_uninstall_string(us):
    qm = re.match(r'^\s*"([^"]+)"\s*(.*)$', us)
    if qm:
        exe_win, rest = qm.group(1), qm.group(2)
    else:
        parts = us.split(None, 1)
        exe_win, rest = parts[0], (parts[1] if len(parts) > 1 else '')
    args = []
    for m in re.finditer(r'--[\w-]+(?:=(?:"[^"]*"|[^\s]+))?', rest):
        tok = m.group(0)
        if '=' in tok:
            k, v = tok.split('=', 1)
            args.append(f'{k}={v.strip(chr(34))}')
        else:
            args.append(tok)
    return exe_win, args


def find_command_native(install_path='', display_name=''):
    """Windows: return (exe_path, args) for the game's uninstaller from the
    real registry, or (None, None)."""
    try:
        import winreg
    except ImportError:
        return None, None
    entries = []
    roots = [(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
             (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
             (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')]
    for hive, sub in roots:
        try:
            k = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                name = winreg.EnumKey(k, i)
                sk = winreg.OpenKey(k, name)
                vals = {}
                for j in range(winreg.QueryInfoKey(sk)[1]):
                    vn, vv, _ = winreg.EnumValue(sk, j)
                    vals[vn] = vv
            except OSError:
                continue
            us = vals.get('UninstallString', '')
            if us and (vals.get('Publisher') == 'Blizzard Entertainment'
                       or 'Blizzard Uninstaller' in us or 'Battle.net.exe' in us):
                entries.append(vals)
    e = _pick_entry(entries, install_path, display_name)
    if not e:
        return None, None
    return _split_uninstall_string(e['UninstallString'])


def _win_to_prefix_path(win_path, prefix):
    p = win_path.strip().strip('"')
    m = re.match(r'^([A-Za-z]):[\\/](.*)$', p)
    if not m:
        return p
    rel = m.group(2).replace('\\', '/')
    return os.path.join(prefix, 'drive_c', rel)


def find_command(prefix, install_path='', display_name=''):
    """Linux: return (exe_abs_path_in_prefix, args_list) for the game's
    uninstaller, or (None, None) if no matching registry entry is found."""
    entry = _pick_entry(_blizzard_uninstall_entries(prefix), install_path, display_name)
    if not entry:
        return None, None
    exe_win, args = _split_uninstall_string(entry['UninstallString'])
    return _win_to_prefix_path(exe_win, prefix), args
