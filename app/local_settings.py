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
