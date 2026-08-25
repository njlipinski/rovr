# app/roi_metadata.py
"""Reads the analyst-assigned ROI metadata that ROI Studio writes into a scene's
.fits, and the field schema that describes how to present it.

ROI Studio gives every ROI its own image HDU per eye, named after the colour it
drew the ROI in, and stamps that HDU's header with the analyst's answers. The
left- and right-eye HDUs of one ROI carry identical metadata, so only the left
eye is read.

The vocabulary of allowed answers is deliberately NOT mirrored here. ROVR
displays values and never validates them, so new categories need no ROVR
release. What is needed is field order and labels, and those are read at run
time from ROI Studio's own resources file — which means they track whatever
version of ROI Studio is installed.
"""
import json
import os
import re

from app.local_settings import get_roi_studio_path

# ROI Studio's schema file, relative to the installed executable. The Windows
# build is PyInstaller onedir; the macOS build wraps the same tree in a bundle,
# and the exact level varies by build, so several candidates are tried.
_SCHEMA_RELPATHS = (
    os.path.join('_internal', 'resources', 'pcam_roi_metadata.json'),
    os.path.join('resources', 'pcam_roi_metadata.json'),
    os.path.join('Contents', 'Resources', 'resources', 'pcam_roi_metadata.json'),
    os.path.join('Contents', 'MacOS', '_internal', 'resources', 'pcam_roi_metadata.json'),
)

# Used only when ROI Studio's schema file can't be found, so a slide still
# renders on a machine that has never launched it. Order matches the shipped
# file; labels are regenerated from the keyword if a new field shows up.
_FALLBACK_FIELDS = (
    ('FEATURE',         'Feature'),
    ('FEATURE_SUBTYPE', 'Feature subtype'),
    ('FLOAT',           'Float'),
    ('TEXTURE',         'Texture'),
    ('DISTANCE',        'Distance'),
    ('DESCRIPTION',     'Description'),
)

# The table's columns, after the ROI name column the slide draws itself. Closed
# list: these and nothing else, drawn whether or not any ROI fills them.
TABLE_COLUMNS = ('FEATURE', 'FEATURE_SUBTYPE', 'DISTANCE')

# Fields drawn on a line under their row rather than in a table column, in the
# order they read on that line. Description is far longer than any other field
# and left in the grid it sets the column widths for the whole table. Float and
# texture join it to give the grid back to the fields that identify an ROI; they
# carry no label there, since the values read clearly enough in context.
CONTINUATION_FIELDS = ('FLOAT', 'TEXTURE', 'DESCRIPTION')

# Header cards that identify an HDU as an ROI rather than describing the scene.
_ROI_NAME_KEY = 'NAME'
_ROI_EYE_KEY  = 'EYE'
_PRIMARY_EYE  = 'left'


def _pretty_label(key):
    """'FEATURE_SUBTYPE' -> 'Feature subtype', for a field not in the schema."""
    return key.replace('_', ' ').capitalize()


def load_field_schema(roi_studio_path=None):
    """Return [(key, label), ...] in the order ROI Studio presents them.

    Falls back to a built-in list if ROI Studio isn't installed or its schema
    file has moved — a missing schema degrades the table's column order and
    labels, never the values."""
    exe = roi_studio_path if roi_studio_path is not None else get_roi_studio_path()
    if exe:
        root = exe if os.path.isdir(exe) else os.path.dirname(exe)
        for rel in _SCHEMA_RELPATHS:
            candidate = os.path.join(root, rel)
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    fields = json.load(f).get('fields', [])
            except (OSError, json.JSONDecodeError, AttributeError):
                break
            parsed = [
                (f['key'], f.get('label') or _pretty_label(f['key']))
                for f in fields if isinstance(f, dict) and f.get('key')
            ]
            if parsed:
                return parsed
            break
    return list(_FALLBACK_FIELDS)


# ── ROI swatch colours ────────────────────────────────────────────────────────
# An ROI's name IS its colour, which is what ties a table row to its box in the
# DCS and its curve in the spectra plot. ROI Studio writes the display name
# ('forest', 'scarlet'), so that is what arrives in the .fits.
#
# Its palette is defined in ROI Studio's own ColorManager._init_pcam_palette(),
# which maps each display name to a MERSpect key that is itself a base hue with
# a shade offset. Those keys are mirrored below and the offsets applied here, so
# the palette's internal relationships hold: scarlet is brighter than red,
# maroon is the darkest red, salmon the lightest. The RGB values are close
# enough to read as the right colour rather than exact matches, and the swatch
# is a scanning aid only. Every row also prints the name, which is authoritative.
_MCZ_KEY_BY_ROI_NAME = {
    'red':       'red-1',
    'green':     'green',
    'blue':      'blue',
    'cyan':      'cyan',
    'forest':    'green-2',
    'yellow':    'yellow',
    'magenta':   'magenta',
    'salmon':    'red+2',
    'teal':      'cyan-2',
    'goldenrod': 'orange-1',
    'sienna':    'orange-2',
    'navy':      'blue-2',
    'scarlet':   'red',
    'maroon':    'red-2',
    'purple':    'purple',
}

# Base hues the keys above resolve against. 'orange' is a base only: it carries
# goldenrod and sienna, and is not itself an ROI colour.
_BASE_COLORS = {
    'green':   (0.00, 0.80, 0.27),
    'blue':    (0.00, 0.27, 1.00),
    'cyan':    (0.00, 0.90, 0.90),
    'yellow':  (0.93, 0.93, 0.00),
    'magenta': (1.00, 0.00, 0.67),
    'red':     (0.88, 0.00, 0.00),
    'orange':  (1.00, 0.55, 0.00),
    'purple':  (0.60, 0.20, 0.80),
}
_SHADE_RE = re.compile(r'^([a-z]+)\s*([+-])\s*(\d+)$', re.IGNORECASE)
_SHADE_STEP = 0.20
_UNKNOWN_COLOR = (0.50, 0.50, 0.50)


def roi_color(name):
    """Return an RGB triple for an ROI's colour name, e.g. 'forest'.

    A raw MERSpect key ('cyan-2') resolves too, so a .fits written before ROI
    Studio normalized names to the display form still gets its colour."""
    key = (name or '').strip().lower()
    key = _MCZ_KEY_BY_ROI_NAME.get(key, key)
    shift = 0
    m = _SHADE_RE.match(key)
    if m:
        key = m.group(1)
        shift = int(m.group(3)) * (1 if m.group(2) == '+' else -1)
    base = _BASE_COLORS.get(key)
    if base is None:
        return _UNKNOWN_COLOR
    if not shift:
        return base
    # Clamped: nothing stops a name carrying a large offset, and a shade that
    # ran past black or white would be an out-of-range colour rather than a
    # very dark or very pale one.
    factor = min(1.0, _SHADE_STEP * abs(shift))
    if shift < 0:
        return tuple(min(1.0, max(0.0, c * (1 - factor))) for c in base)
    return tuple(min(1.0, max(0.0, c + (1.0 - c) * factor)) for c in base)


# ── Reading a scene's ROIs ────────────────────────────────────────────────────

def read_scene_rois(fits_path):
    """Return [{'name': ..., <FIELD>: value, ...}, ...], one entry per ROI.

    Returns an empty list for a scene with no ROIs — that is a legitimate state
    the slide has to render, not an error. Raises OSError or ValueError if the
    file can't be read or isn't FITS."""
    from app.fits_header import read_headers

    headers = read_headers(fits_path)
    left = [h for h in headers
            if h.get(_ROI_NAME_KEY) and str(h.get(_ROI_EYE_KEY, '')).lower() == _PRIMARY_EYE]
    # A file that never records an eye still describes its ROIs; fall back to
    # every named HDU rather than reporting none.
    if not left:
        left = [h for h in headers if h.get(_ROI_NAME_KEY)]

    rois = []
    for h in left:
        roi = {k: v for k, v in h.items() if isinstance(v, str) or isinstance(v, (int, float))}
        roi['name'] = str(h.get(_ROI_NAME_KEY, '')).strip()
        rois.append(roi)
    return rois


def present_fields(rois, schema=None):
    """Return the [(key, label), ...] the table draws: the fixed columns, then
    whichever continuation fields any ROI fills.

    Columns are fixed, not discovered: provenance cards share the header with
    the analyst's answers, and discovering columns picked those up too. Labels
    still come from the schema."""
    schema = schema if schema is not None else load_field_schema()
    labels = dict(schema)

    def labelled(key):
        return key, labels.get(key) or _pretty_label(key)

    return [labelled(k) for k in TABLE_COLUMNS] + [
        labelled(k) for k in CONTINUATION_FIELDS
        if any(str(roi.get(k, '')).strip() for roi in rois)
    ]
