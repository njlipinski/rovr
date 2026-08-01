# app/slides.py
"""Summary slide generation.

A summary slide is one PDF carrying everything a supervisor needs to review a
scene: the left-eye DCS with the analyst's ROI boxes, the right-eye RGB for
context, the spectra plot, and a table of the analyst's per-ROI metadata — in a
2x2 grid.

The three image panels are not drawn here. ROI Studio writes them beside the
.fits/.sel every time an analyst saves, so this module only composites what is
already on disk. Panels are placed at native pixel size and never cropped or
enlarged; anything smaller than its cell is centred with margin.

A scene can have anywhere from zero to fifteen or more ROIs, so the metadata
table shrinks to fit and spills onto a second page rather than being truncated.

No Qt and no SQL — a slide is a pure function of a scene row and the Pancam
tree, so it can be rendered off the UI thread and tested without a database.
"""
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
    FREE_TEXT_FIELDS, load_field_schema, present_fields, read_scene_rois, roi_color,
)

# Panels, in the 2x2 order they are laid out. The left DCS / right RGB pairing
# is deliberate and not symmetric: the DCS carries the ROI boxes that key to
# the spectra curves, and the opposite eye's RGB gives true-colour context.
# The metadata cell has no source image (suffix None).
_LAYOUT = (
    (Panel.LEFT_DCS,  "Left eye — DCS"),
    (Panel.RIGHT_RGB, "Right eye — RGB"),
    (Panel.SPECTRA,   "Spectra"),
    (None,            "ROI metadata"),
)

# Cell size matches ROI Studio's native DCS/RGB output, so the two panels that
# dominate the slide are composited without any resampling at all.
CELL_PX    = 1039
DPI        = 100
MARGIN_PX  = 24
GAP_PX     = 24
TITLE_PX   = 76
CAPTION_PX = 44

WIDTH_PX  = MARGIN_PX * 2 + CELL_PX * 2 + GAP_PX
HEIGHT_PX = MARGIN_PX * 2 + TITLE_PX + (CAPTION_PX + CELL_PX) * 2 + GAP_PX

_BG        = 'white'
_TITLE_C   = '#1a1a1a'
_SUB_C     = '#555555'
_CAPTION_C = '#444444'
_MUTED_C   = '#999999'
_RULE_C    = '#dddddd'

# Row geometry for the metadata table, in pixels within its cell.
# Rows expand to fill the cell up to _ROW_MAX_PX, then shrink toward
# _ROW_MIN_PX as ROI count climbs. At the minimum, about 29 rows fit a cell —
# past that the table continues on a second page. Scenes here run 0-15 ROIs, so
# in practice the maximum governs and the overflow path is a safety net.
_TABLE_HEAD_PX = 34
_ROW_MAX_PX    = 100
_ROW_MIN_PX    = 34
_SWATCH_PX     = 15


def missing_artifacts(folder):
    """Names of the files a complete save should hold but this folder doesn't.

    A .fits and its panel images are written together by ROI Studio, so one
    without the others means an interrupted or hand-edited save. Reported
    rather than worked around: the alternative is silently composing a slide
    from two different revisions."""
    if not folder:
        return []
    missing = []
    if scene_file(folder, '.fits') is None:
        missing.append('.fits')
    for suffix, caption in _LAYOUT:
        if suffix and find_panel(folder, suffix) is None:
            missing.append(f"{caption} image")
    return missing


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
    ax.text(0.5, 0.5, message, ha='center', va='center', fontsize=15, color=_MUTED_C)
    ax.add_patch(Rectangle((0, 0), 1, 1, fill=False, edgecolor=_RULE_C, linewidth=0.8))


def _column_layout(fields, width_px):
    """Split a cell's width into a name column plus one column per field.

    Widths are weighted by how much text each field actually holds, so a long
    'Feature subtype' isn't given the same room as a three-word 'Float'."""
    weights = [1.4] + [max(1.0, len(label) / 6.0) for _key, label in fields]
    total = sum(weights) or 1.0
    xs, cursor = [], 0.0
    for w in weights:
        xs.append(cursor)
        cursor += width_px * w / total
    return xs, [width_px * w / total for w in weights]


def _draw_roi_table(fig, x, y, rois, fields, w=CELL_PX, h=CELL_PX, start=0):
    """Draw as many ROI rows as fit, returning the index of the first that didn't.

    Rows shrink toward _ROW_MIN_PX before any are dropped, so the common case
    (a handful of ROIs) is roomy and a heavily-annotated scene still fits."""
    ax = _cell_axes(fig, x, y, w, h)
    ax.add_patch(Rectangle((0, 0), 1, 1, fill=False, edgecolor=_RULE_C, linewidth=0.8))

    if not rois:
        ax.text(0.5, 0.5, "No ROIs recorded for this scene",
                ha='center', va='center', fontsize=14, color=_MUTED_C)
        return len(rois)

    remaining = len(rois) - start
    avail = h - _TABLE_HEAD_PX
    row_px = min(_ROW_MAX_PX, max(_ROW_MIN_PX, avail / max(1, remaining)))
    fits = max(1, int(avail // row_px))
    end = min(len(rois), start + fits)

    xs, widths = _column_layout(fields, w)
    body_fs = max(6.5, min(9.5, row_px * 0.20))
    head_fs = body_fs + 0.5

    def tx(px):      # pixel offset within the cell -> axes fraction
        return px / w

    def ty(px):
        return 1.0 - px / h

    for i, (_key, label) in enumerate(fields):
        ax.text(tx(xs[i + 1]), ty(_TABLE_HEAD_PX * 0.55), label,
                fontsize=head_fs, color=_CAPTION_C, ha='left', va='center')
    ax.text(tx(xs[0]), ty(_TABLE_HEAD_PX * 0.55), "ROI",
            fontsize=head_fs, color=_CAPTION_C, ha='left', va='center')
    ax.plot([0, 1], [ty(_TABLE_HEAD_PX)] * 2, color=_RULE_C, linewidth=0.8)

    free_text = [k for k, _ in fields if k in FREE_TEXT_FIELDS]
    for n, roi in enumerate(rois[start:end]):
        top = _TABLE_HEAD_PX + n * row_px
        mid = ty(top + row_px * 0.38)

        sw = _SWATCH_PX / w
        ax.add_patch(Rectangle(
            (tx(xs[0]), mid - (_SWATCH_PX / h) / 2), sw, _SWATCH_PX / h,
            facecolor=roi_color(roi.get('name')), edgecolor='#666666', linewidth=0.5,
        ))
        ax.text(tx(xs[0] + _SWATCH_PX + 6), mid, str(roi.get('name', '')),
                fontsize=body_fs, color='#222222', ha='left', va='center')

        for i, (key, _label) in enumerate(fields):
            if key in FREE_TEXT_FIELDS:
                continue
            value = str(roi.get(key, '') or '').strip()
            if not value:
                continue
            ax.text(tx(xs[i + 1]), mid, _ellipsize(value, widths[i + 1], body_fs),
                    fontsize=body_fs, color='#222222', ha='left', va='center')

        for key in free_text:
            value = str(roi.get(key, '') or '').strip()
            if value:
                ax.text(tx(xs[0] + _SWATCH_PX + 6), ty(top + row_px * 0.78),
                        _ellipsize(value, w * 0.92, body_fs - 0.5),
                        fontsize=body_fs - 0.5, color=_SUB_C, ha='left', va='center',
                        style='italic')

        if n:
            ax.plot([0, 1], [ty(top)] * 2, color='#f0f0f0', linewidth=0.5)

    return end


def _ellipsize(text, width_px, fontsize):
    """Trim to what fits a column. Approximate — DejaVu averages ~0.6em per
    character — but it only ever shortens, so a wrong guess costs a character,
    never an overlap that hides another column."""
    max_chars = max(4, int(width_px / (fontsize * 0.62)))
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
    panel or unreadable .fits is not an error — that cell renders as a
    placeholder, since a slide that is three-quarters useful beats no slide.
    """
    folder = folder or find_scene_folder(pancam_path, scene)
    if not folder:
        raise FileNotFoundError(
            f"No ROI Studio folder found for scene '{scene['name']}' — "
            "nothing has been saved for it yet."
        )

    # A complete save has a .fits and all three panels. Anything less means the
    # current folder is broken, and the fix is to report it — reaching into an
    # older revision for the missing piece is exactly the folder-mixing this
    # module resolves a folder once to prevent.
    missing = missing_artifacts(folder)
    if missing:
        raise FileNotFoundError(
            f"'{scene['name']}': {os.path.basename(folder)} is missing "
            f"{', '.join(missing)}. The scene needs re-saving from ROI Studio."
        )

    rois, roi_error = _load_rois(pancam_path, scene)
    fields = present_fields(rois, load_field_schema()) if rois else []

    fig = _new_figure()
    _fig_text(fig, MARGIN_PX, MARGIN_PX + TITLE_PX * 0.42, scene['name'],
              fontsize=21, color=_TITLE_C, ha='left', va='center')
    _fig_text(fig, MARGIN_PX, MARGIN_PX + TITLE_PX * 0.80,
              _scene_subtitle(scene, len(rois)),
              fontsize=11, color=_SUB_C, ha='left', va='center')

    overflow_from = None
    for i, (suffix, caption) in enumerate(_LAYOUT):
        x, y = _cell_origin(i)
        _fig_text(fig, x + 4, y + CAPTION_PX * 0.65, caption,
                  fontsize=13, color=_CAPTION_C, ha='left', va='center')
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
              f"{scene['name']} — ROI metadata (continued)",
              fontsize=19, color=_TITLE_C, ha='left', va='center')
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
