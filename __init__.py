import logging
import os
import sys

log = logging.getLogger(__name__)


def _find_native_launcher():
    """Path to Battle.net.exe on Windows, or None."""
    if sys.platform != 'win32':
        return None
    from runners.windows import find_installed_exe
    exe = find_installed_exe('Battle.net', 'Battle.net.exe')
    if exe:
        return exe
    # Fallback: standard install-path guesses, in case the registry entry
    # is missing or named differently.
    for env in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'):
        base = os.environ.get(env, '')
        if base:
            for p in (os.path.join(base, 'Battle.net', 'Battle.net.exe'),
                      os.path.join(base, 'Blizzard Entertainment', 'Battle.net', 'Battle.net.exe')):
                if os.path.isfile(p):
                    return p
    return None


def _find_launcher_exe(prefix):
    """Absolute path to Battle.net.exe inside a Wine prefix, or None."""
    if not prefix or not os.path.isdir(prefix):
        return None
    direct = os.path.join(prefix, 'drive_c', 'Program Files (x86)',
                          'Battle.net', 'Battle.net.exe')
    if os.path.isfile(direct):
        return direct
    for dirpath, _dirs, files in os.walk(prefix):
        if 'Battle.net.exe' in files:
            return os.path.join(dirpath, 'Battle.net.exe')
    return None


def _launcher_config():
    import json
    from config import CONFIG_PATH
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    lc = cfg.get('launchers', {}).get('battle_net', {})
    prefix   = (lc.get('prefix') or '').strip()
    wine_bin = (lc.get('wine_bin') or '').strip() or None
    return prefix, wine_bin


def _launch_via_client(prefix, wine_bin, code):
    """Start an installed game through the Wine-installed Battle.net client.

    `battlenet://<code>` only *selects* a game in the client -- it never starts
    a play session -- so an installed game is launched with
    `Battle.net.exe --exec="launch <code>"` instead. That command needs the
    client already running and loaded, so a cold start warms it up on a
    background thread first, then fires the launch (Playnite does the same).
    """
    import threading
    from runners.wine import run_in_prefix, _prefix_has_running_process

    exe = _find_launcher_exe(prefix)
    if not exe:
        raise RuntimeError('Battle.net.exe not found in the prefix -- reinstall the launcher.')

    launch_arg = f'--exec=launch {code}'

    if _prefix_has_running_process(prefix):
        run_in_prefix(prefix, exe, args=[launch_arg], wine_bin=wine_bin,
                      env_extra={'WINEDEBUG': '-all'})
        return

    def _cold_then_launch():
        import time
        try:
            run_in_prefix(prefix, exe, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'})
            for _ in range(30):
                time.sleep(1)
                if _prefix_has_running_process(prefix):
                    break
            time.sleep(8)  # let the client UI finish loading before it'll take --exec
            run_in_prefix(prefix, exe, args=[launch_arg], wine_bin=wine_bin,
                          env_extra={'WINEDEBUG': '-all'})
        except Exception as e:
            log.error('Battle.net cold launch failed: %s', e, exc_info=True)

    threading.Thread(target=_cold_then_launch, daemon=True).start()


class BattleNetPlugin:
    id       = 'battle_net'
    name     = 'Battle.net'
    platform = 'battle_net'
    label    = 'Battle.net'

    def register(self, app):
        from .routes import bp
        app.register_blueprint(bp)
        log.info('Battle.net plugin registered')

    def on_startup(self):
        from .watcher import sync_bnet_install_status, start_bnet_watcher
        try:
            sync_bnet_install_status()
        except Exception as e:
            log.warning('Startup Battle.net install sync failed: %s', e)
        prefix, _ = _launcher_config()
        if prefix:
            watch = os.path.join(prefix, 'drive_c', 'ProgramData', 'Battle.net', 'Agent')
            try:
                start_bnet_watcher(watch)
            except Exception as e:
                log.warning('Battle.net watcher start failed: %s', e)

    def resync_installed(self):
        from .battle_net import resync_installed
        resync_installed()

    def on_shutdown(self):
        from .watcher import stop_bnet_watcher
        stop_bnet_watcher()

    def on_uninstall(self):
        from . import account
        account.clear()

    def launch_game(self, appid):
        import time
        from database import get_db, ts_to_date, update_game_data
        from . import catalog

        db  = get_db()
        row = db.execute(
            "SELECT platform_id, installed FROM games WHERE appid = ?", (appid,)
        ).fetchone()
        db.close()
        if not row:
            return {'status': 'error', 'message': 'Battle.net game not found'}

        code = (row['platform_id'] or '').strip()
        if not code:
            return {'status': 'error', 'message': 'Game has no product code -- try re-syncing'}
        game = catalog.by_code(code)
        code = game['code'] if game else code

        if sys.platform == 'win32':
            try:
                if row['installed']:
                    native = _find_native_launcher()
                    if native:
                        import subprocess
                        subprocess.Popen([native, f'--exec=launch {code}'])
                    else:
                        os.startfile(f'battlenet://{code}')
                else:
                    os.startfile(f'battlenet://{code}')
            except Exception as e:
                return {'status': 'error', 'message': f'Launch failed: {e}'}
        else:
            prefix, wine_bin = _launcher_config()
            if not prefix:
                return {'status': 'error',
                        'message': 'Battle.net not configured. Open Plugins -> Manage to set up the launcher.'}
            try:
                if row['installed']:
                    _launch_via_client(prefix, wine_bin, code)
                else:
                    # Not installed -- open the client to the game's page so the
                    # user can install it. battlenet://<code> only *selects* the
                    # game, it doesn't start a download or a play session.
                    from runners.wine import launch_protocol_url
                    launch_protocol_url(prefix, f'battlenet://{code}', wine_bin=wine_bin,
                                        env_extra={'WINEDEBUG': '-all'})
            except RuntimeError as e:
                return {'status': 'error', 'message': str(e)}
            except Exception as e:
                return {'status': 'error', 'message': f'Launch failed: {e}'}

        if row['installed']:
            now = int(time.time())
            update_game_data(appid, last_played=now)
            return {'status': 'success', 'last_played': ts_to_date(now)}
        return {'status': 'success'}

    def uninstall_game(self, appid):
        from database import get_db
        from . import uninstall as _uninstall

        db  = get_db()
        row = db.execute(
            "SELECT platform_id, name, install_path, installed FROM games WHERE appid = ?",
            (appid,),
        ).fetchone()
        db.close()
        if not row:
            return {'status': 'error', 'message': 'Battle.net game not found'}
        if not row['installed']:
            return {'status': 'error', 'message': 'That game is not installed.'}

        if sys.platform == 'win32':
            import subprocess
            exe, args = _uninstall.find_command_native(row['install_path'], row['name'])
            if not exe:
                return {'status': 'error',
                        'message': 'Could not find the uninstaller -- remove it from the Battle.net client instead.'}
            try:
                subprocess.Popen([exe] + args)
            except Exception as e:
                return {'status': 'error', 'message': f'Uninstall failed: {e}'}
            return {'status': 'success'}

        prefix, wine_bin = _launcher_config()
        if not prefix:
            return {'status': 'error', 'message': 'Battle.net launcher is not configured.'}
        exe, args = _uninstall.find_command(prefix, row['install_path'], row['name'])
        if not exe:
            return {'status': 'error',
                    'message': 'Could not find the uninstaller -- remove it from the Battle.net client instead.'}
        try:
            from runners.wine import run_in_prefix
            run_in_prefix(prefix, exe, args=args, wine_bin=wine_bin,
                          env_extra={'WINEDEBUG': '-all'})
        except RuntimeError as e:
            return {'status': 'error', 'message': str(e)}
        except Exception as e:
            return {'status': 'error', 'message': f'Uninstall failed: {e}'}
        return {'status': 'success'}

    def start_launcher(self):
        """Open Battle.net with no specific game."""
        if sys.platform == 'win32':
            native = _find_native_launcher()
            if not native:
                return {'status': 'error', 'message': 'Battle.net is not installed'}
            try:
                os.startfile(native)
            except Exception as e:
                return {'status': 'error', 'message': f'Launch failed: {e}'}
            return {'status': 'success'}

        prefix, wine_bin = _launcher_config()
        if not prefix:
            return {'status': 'error',
                    'message': 'Battle.net not configured. Open Plugins -> Manage to set up the launcher.'}
        exe = _find_launcher_exe(prefix)
        if not exe:
            return {'status': 'error', 'message': 'Battle.net.exe not found in the prefix -- reinstall the launcher.'}
        try:
            from runners.wine import run_in_prefix
            run_in_prefix(prefix, exe, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'})
        except RuntimeError as e:
            return {'status': 'error', 'message': str(e)}
        except Exception as e:
            return {'status': 'error', 'message': f'Launch failed: {e}'}
        return {'status': 'success'}

    def launcher_status(self):
        if sys.platform == 'win32':
            return ({'available': True, 'detail': 'Battle.net launcher detected'}
                    if _find_native_launcher()
                    else {'available': False, 'detail': 'Battle.net not installed'})

        prefix, wine_bin = _launcher_config()
        from runners.wine import find_wine_binary
        if not (wine_bin or find_wine_binary()):
            return {'available': False, 'detail': 'No Wine binary found'}
        if not prefix:
            return {'available': False, 'detail': 'Wine prefix not configured'}
        if not os.path.isdir(prefix):
            return {'available': False, 'detail': f'Prefix not found: {prefix}'}
        if _find_launcher_exe(prefix):
            return {'available': True, 'detail': 'Launcher ready'}
        return {'available': False,
                'detail': 'Battle.net.exe not found in prefix -- reinstall the launcher'}

    def js_api(self):
        return {
            'uninstall_url':  '/api/battle_net/uninstall/{appid}',
            'scrape_url':     '/api/battle_net/scrape-single/{appid}',
            'scrape_method':  'POST',
            'store_url':      None,
            'store_label':    None,
            'appid_label':    'Battle.net Product:',
            'sync_label':     'Sync Battle.net Library',
        }

    def manage_ui(self):
        if sys.platform == 'win32':
            launcher_section = {
                'title': 'Launcher',
                'items': (
                    [{'type': 'text', 'content': 'Battle.net is installed.'},
                     {'type': 'button', 'label': 'Start Launcher', 'action': {
                         'type': 'call', 'fn': 'battlenetStartLauncher',
                     }}]
                    if _find_native_launcher() else
                    [{'type': 'text', 'content': 'Battle.net is not installed.'},
                     {'type': 'button', 'label': 'Download Battle.net', 'action': {
                         'type': 'open_url',
                         'url': 'https://www.blizzard.com/apps/battle.net/desktop'}}]
                ),
            }
        else:
            items = [
                {'type': 'text', 'content':
                    'Install the Battle.net launcher into a Wine prefix, then sign in to it '
                    'once so PlayDate can read your library from its local files.'},
                {'type': 'launcher_config'},
            ]
            _prefix, _wine_bin = _launcher_config()
            if _prefix and _find_launcher_exe(_prefix):
                items.append({'type': 'text', 'content': 'Battle.net is installed.'})
                items.append({'type': 'button', 'label': 'Start Launcher', 'action': {
                    'type': 'call', 'fn': 'battlenetStartLauncher',
                }})
                items.append({'type': 'button', 'label': 'Open Folder', 'action': {
                    'type': 'call', 'fn': 'battlenetOpenFolder',
                }})
                items.append({'type': 'status_output', 'key': 'folder'})
            launcher_section = {'title': 'Launcher', 'items': items}

        return {
            'sections': [
                {
                    'title': 'Library',
                    'items': [
                        {'type': 'text', 'content':
                            "PlayDate can read your library two ways. Without an account connected, it "
                            "reads the launcher's own local files: installed games always, plus paid "
                            "games it's confident about from the account's cached licenses (Call of Duty "
                            'titles only count once installed -- Blizzard grants free-access licenses '
                            'that look identical to a purchase locally). Connecting the account below '
                            "adds Blizzard's own owned-games list on top, including games not yet "
                            'installed, plus real purchase dates.'},
                        {'type': 'info_endpoint', 'endpoint': '/api/battle_net/status'},
                        {'type': 'button', 'label': 'Sync Library',
                         'action': {'type': 'call', 'fn': 'battlenetSync'}},
                        {'type': 'status_output', 'key': 'main'},
                    ],
                },
                {
                    'title': 'Account (optional)',
                    'auth': {
                        'endpoint': '/api/battle_net/account/status',
                        'disconnected': [
                            {'type': 'text', 'content':
                                'Connect your Battle.net account for the authoritative owned-games '
                                "list -- covers games you haven't installed and resolves Call of Duty "
                                'ownership the local scan can only guess at. No developer account or API '
                                'key needed.'},
                            {'type': 'button', 'label': 'Connect Battle.net Account', 'action': {
                                'type': 'oauth_popup',
                                'title': 'Connect Battle.net Account',
                                'url_endpoint': '/api/battle_net/account/auth-url',
                                'callback_endpoint': '/api/battle_net/account/callback',
                                # Confirmed live 2026-09-04 via playdate.log's page-loaded
                                # trail: /api/logout kicks off a real OAuth2 code flow
                                # (oauth.battle.net -> us.account.battle.net/login/... ->
                                # back to account.battle.net/callback/oauth2/...), landing
                                # on the bare root https://account.battle.net/ once signed
                                # in -- not /overview. A plain 'account.battle.net' pattern
                                # matched the login FORM page too (us.account.battle.net
                                # contains that substring) and closed the popup before any
                                # typing; '://account.battle.net/' only matches the scheme
                                # directly followed by the bare host+root, which the
                                # us.account.battle.net login/password pages never are.
                                'redirect_pattern': '://account.battle.net/',
                                'cookie_name': '*',
                                'code_js': '',
                                'instructions': [
                                    'Click <strong>Open Battle.net Login</strong> and log in to your Battle.net account.',
                                    'The window closes itself once you\'re signed in.',
                                ],
                                'open_label': 'Open Battle.net Login',
                            }},
                        ],
                        'connected': [
                            {'type': 'connected_label'},
                            {'type': 'button', 'label': 'Disconnect', 'variant': 'muted', 'action': {
                                'type': 'post', 'endpoint': '/api/battle_net/account/disconnect',
                                'on_success': 'refresh_auth',
                            }},
                        ],
                    },
                },
                launcher_section,
            ],
        }

    def fetch_description(self, appid, platform_id):
        return None

    def rescrape(self, appid):
        from .battle_net import scrape_single
        return scrape_single(appid) or None

    def fragments(self):
        return {
            'base_head_styles': 'battle_net_base_head_styles.html',
            'tools_scripts':    'battle_net_tools_scripts.html',
        }


plugin = BattleNetPlugin()
