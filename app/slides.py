# app/slides.py
"""Summary slide generation.

A summary slide is one PDF carrying everything a supervisor needs to review a
scene: the left-eye DCS with the analyst's ROI boxes, the right-eye RGB for
context, the spectra plot, and a table of the analyst's per-ROI metadata, in a
2x2 grid.

The three image panels are not drawn here. ROI Studio writes them beside the
.fits/.sel every time an analyst saves, so this module only composites what is
already on disk. Panels are placed at native pixel size and never cropped or
enlarged; anything smaller than its cell is centred with margin.

A scene can have anywhere from zero to fifteen ROIs, one per palette colour, and
the metadata table's type is sized so all fifteen fit a cell. Anything beyond
that spills onto a second page rather than being truncated."""

import os

import matplotlib.image as mpimg
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from app.paths import (
    SUMMARY_FORMAT, Panel, find_panel, find_scene_folder, find_fits_file,
    scene_file, summary_slide_paths,
)
from app.roi_metadata import (
    CONTINUATION_FIELDS, load_field_schema, present_fields, read_scene_rois, roi_color,
)

# Panels, in the 2x2 order they are laid out. The left DCS / right RGB pairing
# is deliberate and not symmetric: the DCS carries the ROI boxes that key to
# the spectra curves, and the opposite eye's RGB gives true-colour context.
# The metadata cell has no source image (suffix None).
#
# The RGB cell prefers the ROI-labelled image and falls back to the plain one,
# since only recent saves have the labelled version. The DCS cell has no
# labelled counterpart yet; give it the same treatment when one exists.
_LAYOUT = (
    (Panel.LEFT_DCS,                            "Left eye DCS"),
    ((Panel.RIGHT_RGB_NAMED, Panel.RIGHT_RGB),  "Right eye RGB"),
    (Panel.SPECTRA,                             "Spectra"),
    (None,                                      "ROI metadata"),
)

# Cell size matches ROI Studio's native DCS/RGB output, so the two panels that
# dominate the slide are composited without any resampling at all.
CELL_PX    = 1039
DPI        = 100
# Margins and gaps are hairlines: the page is over 2000px on a side, and every
# pixel spent on white space is a pixel not spent on the panels or the table.
MARGIN_PX  = 8
GAP_PX     = 8
TITLE_PX   = 52
CAPTION_PX = 30

WIDTH_PX  = MARGIN_PX * 2 + CELL_PX * 2 + GAP_PX
HEIGHT_PX = MARGIN_PX * 2 + TITLE_PX + (CAPTION_PX + CELL_PX) * 2 + GAP_PX

_BG        = 'white'
_TITLE_C   = '#1a1a1a'
_SUB_C     = '#555555'
_CAPTION_C = '#444444'
_MUTED_C   = '#999999'
_RULE_C    = '#dddddd'

# Type sizes are in points against a 100 DPI page more than 20 inches wide, so
# they read far smaller than the same number would on a letter page. These are
# sized for a slide viewed fit-to-width on a monitor.
_TITLE_FS       = 30
_SUB_FS         = 17
_CAPTION_FS     = 17
_PLACEHOLDER_FS = 24

# Row geometry for the metadata table, in pixels within its cell.
#
# The palette has 15 colors, so a scene cannot hold more than 15 ROIs, and the
# body type is sized so exactly that many rows fit a cell. That fixes the type
# size for every slide rather than letting it drift with ROI count. A table with
# fewer rows spreads them out (up to _ROW_STRETCH times the base height) instead
# of growing the text, so the bottom of the cell is not left empty.
_TABLE_HEAD_PX = 46
_ROWS_PER_CELL = 15
_ROW_STRETCH   = 2.0
# Body point size as a fraction of row height in pixels. Leaves room under the
# value line for a free-text field's wrapped line without the two colliding.
_BODY_FS_RATIO = 0.36
_BODY_FS_MIN   = 11
_GUTTER_CHARS  = 2
_SWATCH_PX     = 26
_SWATCH_GAP_PX = 8

# Distance is the one field ROVR abbreviates. Its three values are long enough
# to set their column's width while carrying very little information, and its
# heading is wider than the abbreviations, so that is shortened too. An
# unrecognized value prints as written: this is a display shortcut, not a
# vocabulary ROVR enforces.
_DISTANCE_KEY     = 'DISTANCE'
_DISTANCE_ABBREV  = {'nearfield': 'NF', 'midfield': 'MF', 'farfield': 'FF'}
_HEADER_OVERRIDES = {_DISTANCE_KEY: 'Dist'}


def missing_panels(folder):
    """Captions of the panel images this folder doesn't have.

    Absence is not necessarily a fault. A right-eye RGB needs enough right-eye
    filters in the source observation to composite one, and some Pancam
    observations never captured them. Those panels render as placeholders 
    rather than failing, and are reported so a human can judge which gaps 
    are real."""
    if not folder:
        return []
    return [caption for suffix, caption in _LAYOUT
            if suffix and find_panel(folder, suffix) is None]


def missing_artifacts(folder):
    """Everything a complete save should hold but this folder doesn't, panels
    included. For reporting; only a missing .fits actually stops a slide."""
    if not folder:
        return []
    missing = [] if scene_file(folder, '.fits') else ['.fits']
    return missing + [f"{caption} image" for caption in missing_panels(folder)]


def _rect(x, y, w, h):
    """Convert a top-left-origin pixel box to matplotlib's bottom-left figure fractions."""
    return [x / WIDTH_PX, 1.0 - (y + h) / HEIGHT_PX, w / WIDTH_PX, h / HEIGHT_PX]


def _fig_text(fig, x, y, text, **kw):
    """Place text using top-left-origin pixel coordinates."""
    return fig.text(x / WIDTH_PX, 1.0 - y / HEIGHT_PX, text, **kw)


def _cell_origin(index):
    """Top-left pixel of the caption strip for cell `index` (0-3, reading order)."""
    row, col = divmod(index, 2)
    x = MARGIN_PX + col * (CELL_PX + GAP_PX)
    y = MARGIN_PX + TITLE_PX + row * (CAPTION_PX + CELL_PX + GAP_PX)
    return x, y


def _cell_axes(fig, x, y, w=CELL_PX, h=CELL_PX):
    """A blank axes covering a cell, with a 0-1 coordinate space for drawing into."""
    ax = fig.add_axes(_rect(x, y, w, h))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_axis_off()
    return ax


def _draw_image(fig, image_path, x, y):
    """Place an image at native size, centred in its cell. Never enlarged, so a
    panel smaller than the cell (the spectra plot is) keeps its own pixels
    rather than being interpolated up, and never cropped, so an unexpectedly
    large panel is scaled down whole."""
    img = mpimg.imread(image_path)
    img_h, img_w = img.shape[0], img.shape[1]
    scale = min(1.0, CELL_PX / img_w, CELL_PX / img_h)
    w, h = img_w * scale, img_h * scale
    ax = fig.add_axes(_rect(x + (CELL_PX - w) / 2, y + (CELL_PX - h) / 2, w, h))
    ax.imshow(img, interpolation='none' if scale == 1.0 else 'antialiased')
    ax.set_axis_off()


def _draw_placeholder(fig, x, y, message):
    ax = _cell_axes(fig, x, y)
    ax.text(0.5, 0.5, message, ha='center', va='center',
            fontsize=_PLACEHOLDER_FS, color=_MUTED_C)
    ax.add_patch(Rectangle((0, 0), 1, 1, fill=False, edgecolor=_RULE_C, linewidth=0.8))


def _display_value(key, raw):
    """A field's value as it should read in the table."""
    value = str(raw or '').strip()
    if key == _DISTANCE_KEY:
        return _DISTANCE_ABBREV.get(value.lower(), value)
    return value


def _header_label(key, label):
    """A column's heading. Overridden where ROI Studio's own label is wider than
    the values under it and would set the column width on its own."""
    return str(_HEADER_OVERRIDES.get(key, label) or key)


def _split_fields(fields):
    """(columns, continuation) - the fields that get a column of their own, and
    the long ones drawn on a line under each row."""
    return ([f for f in fields if f[0] not in CONTINUATION_FIELDS],
            [f for f in fields if f[0] in CONTINUATION_FIELDS])


def _column_chars(fields, rois):
    """Character width each column needs: its heading, or its longest value."""
    def longest(key):
        return max((len(_display_value(key, r.get(key))) for r in rois), default=0)

    name_col = max(len("ROI"), max((len(str(r.get('name', ''))) for r in rois), default=0))
    return [name_col] + [max(len(_header_label(key, label)), longest(key))
                         for key, label in fields]


def _column_layout(chars, width_px):
    """Split a cell's width into columns proportional to the text each holds, so
    a long 'Texture' isn't given the same room as a three-word 'Float'."""
    weights = [c + _GUTTER_CHARS for c in chars]
    total = sum(weights) or 1.0
    xs, cursor = [], 0.0
    for w in weights:
        xs.append(cursor)
        cursor += width_px * w / total
    return xs, [width_px * w / total for w in weights]


def _fitted_body_fs(chars, row_px, width_px):
    """The largest body size that satisfies both constraints at once: the row
    height that lets _ROWS_PER_CELL rows fit, and the cell width that lets the
    widest row print without being cut.

    Below _BODY_FS_MIN the width constraint is abandoned rather than
    shrinking further, and _ellipsize trims what still doesn't fit."""
    by_row = row_px * _BODY_FS_RATIO
    needed = sum(c + _GUTTER_CHARS for c in chars) or 1
    usable = max(1.0, width_px - _SWATCH_PX - _SWATCH_GAP_PX)
    by_width = usable / (needed * 0.6 * (DPI / 72.0))
    return max(_BODY_FS_MIN, min(by_row, by_width))


def _draw_roi_table(fig, x, y, rois, fields, w=CELL_PX, h=CELL_PX, start=0):
    """Draw as many ROI rows as fit, returning the index of the first that didn't."""
    ax = _cell_axes(fig, x, y, w, h)
    ax.add_patch(Rectangle((0, 0), 1, 1, fill=False, edgecolor=_RULE_C, linewidth=0.8))

    if not rois:
        ax.text(0.5, 0.5, "No ROIs recorded for this scene",
                ha='center', va='center', fontsize=_PLACEHOLDER_FS, color=_MUTED_C)
        return len(rois)

    remaining = len(rois) - start
    avail = h - _TABLE_HEAD_PX
    # Base height is whatever fits _ROWS_PER_CELL, and the type is sized to it
    # once, here. Rows themselves may stretch when there are fewer ROIs; the
    # type does not follow, so every slide's table reads at the same size.
    base_row_px = avail / _ROWS_PER_CELL
    row_px = max(base_row_px, min(base_row_px * _ROW_STRETCH, avail / max(1, remaining)))
    # Epsilon: with row_px at exactly avail/_ROWS_PER_CELL, float division leaves
    # the quotient a hair under the row count and drops the last row.
    fits = max(1, int(avail / row_px + 1e-9))
    end = min(len(rois), start + fits)

    col_fields, cont_fields = _split_fields(fields)
    shown = rois[start:end]
    chars = _column_chars(col_fields, shown)
    xs, widths = _column_layout(chars, w)
    body_fs = _fitted_body_fs(chars, base_row_px, w)
    head_fs = body_fs
    sub_fs = body_fs * 0.78
    indent = xs[0] + _SWATCH_PX + _SWATCH_GAP_PX

    def tx(px):      # pixel offset within the cell -> axes fraction
        return px / w

    def ty(px):
        return 1.0 - px / h

    for i, (key, label) in enumerate(col_fields):
        ax.text(tx(xs[i + 1]), ty(_TABLE_HEAD_PX * 0.55), _header_label(key, label),
                fontsize=head_fs, color=_CAPTION_C, ha='left', va='center')
    ax.text(tx(xs[0]), ty(_TABLE_HEAD_PX * 0.55), "ROI",
            fontsize=head_fs, color=_CAPTION_C, ha='left', va='center')
    ax.plot([0, 1], [ty(_TABLE_HEAD_PX)] * 2, color=_RULE_C, linewidth=0.8)

    for n, roi in enumerate(shown):
        top = _TABLE_HEAD_PX + n * row_px
        mid = ty(top + row_px * 0.34)

        ax.add_patch(Rectangle(
            (tx(xs[0]), mid - (_SWATCH_PX / h) / 2), _SWATCH_PX / w, _SWATCH_PX / h,
            facecolor=roi_color(roi.get('name')), edgecolor='#666666', linewidth=0.5,
        ))
        ax.text(tx(indent), mid,
                _ellipsize(str(roi.get('name', '')),
                            widths[0] - _SWATCH_PX - _SWATCH_GAP_PX, body_fs),
                fontsize=body_fs, color='#222222', ha='left', va='center')

        for i, (key, _label) in enumerate(col_fields):
            value = _display_value(key, roi.get(key))
            if not value:
                continue
            ax.text(tx(xs[i + 1]), mid, _ellipsize(value, widths[i + 1], body_fs),
                    fontsize=body_fs, color='#222222', ha='left', va='center')

        # The continuation fields share one line under the row, so a row costs
        # two lines however many of them it carries, not one line each.
        extra = [v for v in (_display_value(k, roi.get(k)) for k, _ in cont_fields) if v]
        if extra:
            ax.text(tx(indent), ty(top + row_px * 0.74),
                    _ellipsize(" · ".join(extra), w - indent - _GUTTER_CHARS, sub_fs),
                    fontsize=sub_fs, color=_SUB_C, ha='left', va='center', style='italic')

        if n:
            ax.plot([0, 1], [ty(top)] * 2, color='#f0f0f0', linewidth=0.5)

    return end


def _ellipsize(text, width_px, fontsize):
    """Trim to what fits a column. Approximate - DejaVu averages ~0.6em per
    character - but it only ever shortens, so a wrong guess costs a character,
    never an overlap that hides another column.

    fontsize is in points and width_px in pixels, so the point size is converted
    at the page's DPI first. Leaving that out made every column look ~40% wider
    than it is, which only stayed invisible while the type was small."""
    char_px = fontsize * (DPI / 72.0) * 0.6
    max_chars = max(4, int(width_px / char_px))
    return text if len(text) <= max_chars else text[:max_chars - 1] + '…'


def _new_figure():
    fig = Figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI, facecolor=_BG)
    FigureCanvasAgg(fig)
    return fig


def _scene_subtitle(scene, roi_count):
    bits = [b for b in (
        scene['rover'],
        f"sol {scene['sol']:04d}" if scene['sol'] is not None else None,
        scene['seq_id'],
        f"PMA {scene['pma']}" if scene['pma'] is not None else None,
    ) if b]
    bits.append(f"{roi_count} ROI" + ("" if roi_count == 1 else "s"))
    return "   ·   ".join(str(b) for b in bits)


def build_summary_slide(pancam_path, scene, folder=None):
    """Render this scene's summary slide and write it beside its source images
    and into the master collection.

    Returns (beside_sources_path, master_path). Raises FileNotFoundError if the
    scene has no ROI Studio folder yet, and OSError if a write fails. A missing
    panel or unreadable .fits is not an error. That cell renders as a
    placeholder, since a slide that is three-quarters useful beats no slide.
    """
    folder = folder or find_scene_folder(pancam_path, scene)
    if not folder:
        raise FileNotFoundError(
            f"No ROI Studio folder found for scene '{scene['name']}' "
            "nothing has been saved for it yet."
        )

    # Only a missing .fits stops a slide. Without it there is no ROI metadata
    # and no way to tell an interrupted save from a complete one, so it is
    # reported rather than guessed at, and never patched from a neighboring
    # revision, which is the folder-mixing this module exists to prevent.
    # Missing panels are different: see missing_panels() for why they are
    # frequently legitimate. Those cells render as placeholders.
    if scene_file(folder, '.fits') is None:
        raise FileNotFoundError(
            f"'{scene['name']}': {os.path.basename(folder)} has no .fits file. "
            "The scene needs re-saving from ROI Studio."
        )

    rois, roi_error = _load_rois(pancam_path, scene)
    fields = present_fields(rois, load_field_schema()) if rois else []

    fig = _new_figure()
    # Title and subtitle share one line: the name left, the scene's identifiers
    # right. Right-aligning the subtitle rather than running it on after the
    # title avoids measuring the title's width, and cannot collide with it for
    # any name the tree actually holds.
    title_y = MARGIN_PX + TITLE_PX * 0.55
    _fig_text(fig, MARGIN_PX, title_y, scene['name'],
                fontsize=_TITLE_FS, color=_TITLE_C, ha='left', va='center')
    _fig_text(fig, WIDTH_PX - MARGIN_PX, title_y,
                _scene_subtitle(scene, len(rois)),
                fontsize=_SUB_FS, color=_SUB_C, ha='right', va='center')

    overflow_from = None
    for i, (suffix, caption) in enumerate(_LAYOUT):
        x, y = _cell_origin(i)
        _fig_text(fig, x + 4, y + CAPTION_PX * 0.65, caption,
                    fontsize=_CAPTION_FS, color=_CAPTION_C, ha='left', va='center')
        cell_y = y + CAPTION_PX

        if suffix is None:
            if roi_error:
                _draw_placeholder(fig, x, cell_y, roi_error)
            else:
                shown = _draw_roi_table(fig, x, cell_y, rois, fields)
                if shown < len(rois):
                    overflow_from = shown
            continue

        panel = find_panel(folder, suffix)
        if panel:
            _draw_image(fig, panel, x, cell_y)
        else:
            _draw_placeholder(fig, x, cell_y, f"{caption}\nnot available")

    figures = [fig]
    if overflow_from is not None:
        figures.append(_overflow_page(scene, rois, fields, overflow_from))

    beside, master = summary_slide_paths(pancam_path, scene, folder)
    for dest in (beside, master):
        _write_slide(figures, dest)
    return beside, master


def _load_rois(pancam_path, scene):
    """Return (rois, error_message). A scene legitimately can have no ROIs, so
    an empty list is not an error; only an unreadable .fits is."""
    fits_path = find_fits_file(pancam_path, scene)
    if not fits_path:
        return [], "ROI metadata\n(no .fits file found)"
    try:
        return read_scene_rois(fits_path), None
    except (OSError, ValueError) as e:
        return [], f"ROI metadata\ncould not be read:\n{e}"


def _overflow_page(scene, rois, fields, start):
    """A full-width continuation table for scenes with more ROIs than the 2x2
    cell holds."""
    fig = _new_figure()
    _fig_text(fig, MARGIN_PX, MARGIN_PX + TITLE_PX * 0.42,
                f"{scene['name']} ROI metadata (continued)",
                fontsize=_TITLE_FS - 4, color=_TITLE_C, ha='left', va='center')
    width = WIDTH_PX - MARGIN_PX * 2
    height = HEIGHT_PX - MARGIN_PX * 2 - TITLE_PX
    _draw_roi_table(fig, MARGIN_PX, MARGIN_PX + TITLE_PX, rois, fields,
                    w=width, h=height, start=start)
    return fig


def _write_slide(figures, dest):
    """Save to a temp name in the destination directory, then replace the real
    file. The Pancam tree is shared, and a half-written slide left behind by a
    dropped network connection would be indistinguishable from a good one."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.tmp"
    try:
        if SUMMARY_FORMAT == 'pdf':
            with PdfPages(tmp) as pdf:
                for fig in figures:
                    pdf.savefig(fig, facecolor=fig.get_facecolor())
        else:
            figures[0].savefig(tmp, format=SUMMARY_FORMAT,
                                facecolor=figures[0].get_facecolor())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def slide_is_current(pancam_path, scene, folder=None):
    """True if a summary slide exists beside its sources and is at least as new
    as every file it was built from.

    This is what lets the Summary Slide button be cheap: a supervisor opening a
    scene whose slide was generated at submission does no work, and one whose
    analyst re-saved since gets a rebuild without anybody having to ask."""
    folder = folder or find_scene_folder(pancam_path, scene)
    if not folder:
        return False
    beside, master = summary_slide_paths(pancam_path, scene, folder)
    if not (os.path.isfile(beside) and os.path.isfile(master)):
        return False
    slide_mtime = min(os.path.getmtime(beside), os.path.getmtime(master))
    sources = [find_panel(folder, suffix) for suffix, _ in _LAYOUT if suffix]
    sources.append(find_fits_file(pancam_path, scene))
    return all(
        os.path.getmtime(src) <= slide_mtime
        for src in sources if src and os.path.exists(src)
    )
