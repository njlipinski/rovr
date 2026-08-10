"""Per-machine settings stored in %APPDATA%/rovr/local.json.
Used for paths that differ across machines (e.g. ROI Studio location).
"""
import json
import os

_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'rovr')
_FILE = os.path.join(_DIR, 'local.json')


def _load():
    try:
        with open(_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data):
    os.makedirs(_DIR, exist_ok=True)
    with open(_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_roi_studio_path():
    return _load().get('roi_studio_path', '')


def set_roi_studio_path(path):
    data = _load()
    data['roi_studio_path'] = path
    _save(data)


def get_column_widths(key):
    return _load().get('column_widths', {}).get(key)


def set_column_widths(key, widths):
    data = _load()
    data.setdefault('column_widths', {})[key] = widths
    _save(data)


def get_splitter_sizes(key):
    """Return the saved [fraction, ...] of total size for a splitter's panes, or None."""
    return _load().get('splitter_sizes', {}).get(key)


def set_splitter_sizes(key, fractions):
    data = _load()
    data.setdefault('splitter_sizes', {})[key] = fractions
    _save(data)


def get_dark_mode():
    return bool(_load().get('dark_mode', False))


def set_dark_mode(enabled):
    data = _load()
    data['dark_mode'] = bool(enabled)
    _save(data)


def get_ui_scale():
    return float(_load().get('ui_scale', 1.0))


def set_ui_scale(scale):
    data = _load()
    data['ui_scale'] = float(scale)
    _save(data)


def get_dialog_size(key):
    return _load().get('dialog_sizes', {}).get(key)


def set_dialog_size(key, width, height):
    data = _load()
    data.setdefault('dialog_sizes', {})[key] = [width, height]
    _save(data)


def get_all_scene_viewed_times():
    """Return {scene_id_str: 'YYYY-MM-DD HH:MM:SS'} of last-viewed timestamps."""
    return _load().get('scene_viewed', {})


def set_scene_viewed_at(scene_id):
    """Record that the current user just viewed this scene's notes (clears the new-activity indicator)."""
    from datetime import datetime
    data = _load()
    data.setdefault('scene_viewed', {})[str(scene_id)] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _save(data)
