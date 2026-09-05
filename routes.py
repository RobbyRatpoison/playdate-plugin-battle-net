import logging

from flask import Blueprint, jsonify, request

from database import update_game_data

log = logging.getLogger(__name__)

bp = Blueprint('battle_net', __name__, url_prefix='/api/battle_net',
               template_folder='templates')


@bp.route('/status')
def bnet_status():
    """Feeds the Manage modal's info line: whether the launcher is set up and
    how many Battle.net games are in the library."""
    from .battle_net import is_configured
    from database import get_db
    db = get_db()
    try:
        n = db.execute(
            "SELECT COUNT(*) FROM games WHERE platform = 'battle_net'"
        ).fetchone()[0]
    finally:
        db.close()
    configured = is_configured()
    if not configured:
        text, color = 'Launcher not set up', 'var(--text-secondary)'
    else:
        text = f'{n} game{"s" if n != 1 else ""} in library'
        color = 'var(--accent-positive, #5c7e10)'
    return jsonify({'configured': configured, 'game_count': n,
                    'text': text, 'color': color})


@bp.route('/sync', methods=['POST'])
def bnet_sync():
    from .battle_net import start_library_sync
    return jsonify(start_library_sync())


@bp.route('/sync/status')
def bnet_sync_status():
    from .battle_net import get_sync_state
    return jsonify(get_sync_state())


@bp.route('/sync/cancel', methods=['POST'])
def bnet_sync_cancel():
    from .battle_net import cancel_library_sync
    cancel_library_sync()
    return jsonify({'status': 'ok'})


@bp.route('/account/auth-url')
def bnet_account_auth_url():
    from .account import get_auth_url
    return jsonify({'url': get_auth_url()})


@bp.route('/account/callback', methods=['POST'])
def bnet_account_callback():
    raw = ((request.json or {}).get('code') or '').strip()
    log.info('Battle.net account: /callback hit, payload %d chars', len(raw))
    if not raw:
        log.warning('Battle.net account: /callback hit with empty payload')
        return jsonify({'status': 'error', 'message': 'No login session captured'}), 400
    from .account import save_cookies
    ok, result = save_cookies(raw)
    if ok:
        return jsonify({'status': 'success', 'username': result})
    return jsonify({'status': 'error', 'message': result}), 400


@bp.route('/account/status')
def bnet_account_status():
    from .account import is_connected, get_display_name
    return jsonify({'connected': is_connected(), 'username': get_display_name()})


@bp.route('/account/disconnect', methods=['POST'])
def bnet_account_disconnect():
    from .account import clear
    clear()
    return jsonify({'status': 'success'})


@bp.route('/start-launcher', methods=['POST'])
def bnet_start_launcher():
    import plugins
    plugin = plugins.get('battle_net')
    if plugin is None:
        return jsonify({'status': 'error', 'message': 'Plugin not loaded'}), 500
    return jsonify(plugin.start_launcher())


@bp.route('/open-folder', methods=['POST'])
def bnet_open_folder():
    import os
    from .battle_net import get_prefix
    from runners.installdir import open_folder
    prefix = get_prefix()
    if not prefix:
        return jsonify({'status': 'error', 'message': 'No Wine prefix configured'}), 400
    # No single "games" subfolder -- each Battle.net game installs as a
    # sibling of Battle.net itself directly under Program Files (x86).
    open_folder(os.path.join(prefix, 'drive_c', 'Program Files (x86)'))
    return jsonify({'status': 'ok'})


@bp.route('/uninstall/<int:appid>', methods=['POST'])
def bnet_uninstall(appid):
    import plugins
    plugin = plugins.get('battle_net')
    if plugin is None:
        return jsonify({'status': 'error', 'message': 'Plugin not loaded'}), 500
    return jsonify(plugin.uninstall_game(appid))


@bp.route('/scrape-single/<int:appid>', methods=['POST'])
def bnet_scrape_single(appid):
    from .battle_net import scrape_single
    meta = scrape_single(appid)
    if meta is None:
        return jsonify({'status': 'error', 'message': 'Game not found'}), 404
    update_game_data(appid, **meta)
    return jsonify({'status': 'success', 'data': meta})
