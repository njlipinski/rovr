# app/fits_header.py
"""Minimal FITS header reader.

ROI Studio stores each ROI's analyst-assigned metadata in the header of its own
image HDU, so building a summary slide means reading headers out of an ~18 MB
.fits. Everything here reads ASCII header cards and seeks past pixel data —
nothing decodes an image, and no third-party library is needed for that.

Only the parts of the standard ROI Studio actually emits are implemented:
fixed 80-byte cards in 2880-byte blocks, HIERARCH long keywords, and the
CONTINUE long-string convention. Values are returned as str, int, float or
bool; anything unrecognised is left as the raw trimmed string rather than
guessed at.
"""
import os

BLOCK = 2880   # FITS logical record size
CARD  = 80     # every card is exactly this wide, space-padded
_CARDS_PER_BLOCK = BLOCK // CARD


def _split_value(text):
    """Split a card's value field from its trailing comment.

    A quoted string may itself contain '/', so the comment delimiter is only
    honoured outside quotes. Inside a string, '' is an escaped single quote."""
    if not text:
        return ''
    text = text.strip()
    if not text.startswith("'"):
        head = text.split('/', 1)[0]
        return head.strip()

    out = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            break
        out.append(ch)
        i += 1
    return "'" + ''.join(out) + "'"


def _coerce(raw):
    """Turn a raw value field into a Python value."""
    raw = raw.strip()
    if raw.startswith("'"):
        # Quoted string: strip the quotes and FITS' trailing padding.
        return raw[1:-1].rstrip() if raw.endswith("'") else raw[1:].rstrip()
    if raw in ('T', 'F'):
        return raw == 'T'
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def _parse_card(card):
    """Return (keyword, raw_value_field) for one 80-byte card, or (None, None).

    HIERARCH is how FITS carries keywords longer than 8 characters — ROI Studio
    uses it for FEATURE_SUBTYPE and INSTRUMENT, among others."""
    if card.startswith('HIERARCH '):
        body = card[len('HIERARCH '):]
        if '=' not in body:
            return None, None
        key, value = body.split('=', 1)
        return key.strip(), value
    key = card[:8].strip()
    if not key or card[8:10] != '= ':
        return None, None
    return key, card[10:]


def parse_header_block(data):
    """Parse header cards out of `data`, stopping at END.

    Returns (header_dict, consumed_bytes). Repeated keywords keep the first
    value, except CONTINUE, which appends to the previous string."""
    header = {}
    last_key = None
    consumed = 0
    for offset in range(0, len(data), CARD):
        card = data[offset:offset + CARD]
        consumed = offset + CARD
        if len(card) < CARD:
            break
        name = card[:8].strip()
        if name == 'END':
            break
        if name in ('COMMENT', 'HISTORY', ''):
            continue
        if name == 'CONTINUE':
            # The previous value ended with '&' to mark it incomplete.
            piece = _coerce(_split_value(card[8:]))
            if last_key is not None and isinstance(header.get(last_key), str):
                header[last_key] = header[last_key].rstrip('&') + str(piece)
            continue
        key, raw = _parse_card(card)
        if key is None:
            continue
        if key not in header:
            header[key] = _coerce(_split_value(raw))
        last_key = key
    # Headers occupy whole 2880-byte blocks.
    return header, ((consumed + BLOCK - 1) // BLOCK) * BLOCK


def _data_size(header):
    """Bytes of pixel data following this header, padded to a whole block."""
    naxis = header.get('NAXIS', 0)
    if not isinstance(naxis, int) or naxis <= 0:
        return 0
    count = 1
    for i in range(1, naxis + 1):
        count *= header.get(f'NAXIS{i}', 0)
    bitpix = header.get('BITPIX', 8)
    size = abs(bitpix) // 8 * count
    size += header.get('PCOUNT', 0)
    size *= max(1, header.get('GCOUNT', 1))
    return ((size + BLOCK - 1) // BLOCK) * BLOCK


def iter_hdus(path, max_hdus=256):
    """Yield (header, data_bytes) per HDU, pixel data included.

    Costs the whole file, unlike read_headers. The walk is duplicated rather
    than shared to leave read_headers untouched."""
    seen = 0
    with open(path, 'rb') as f:
        first = f.read(BLOCK)
        if not first.startswith(b'SIMPLE'):
            raise ValueError(f"{os.path.basename(path)} is not a FITS file")
        f.seek(0)
        while seen < max_hdus:
            start = f.tell()
            chunk = f.read(BLOCK)
            if len(chunk) < BLOCK:
                break
            # A header can span several blocks; keep reading until END.
            text = chunk.decode('ascii', errors='replace')
            header, consumed = parse_header_block(text)
            while consumed >= len(text) and 'END' not in text[-BLOCK:]:
                more = f.read(BLOCK)
                if len(more) < BLOCK:
                    break
                text += more.decode('ascii', errors='replace')
                header, consumed = parse_header_block(text)
            size = _data_size(header)
            seen += 1
            f.seek(start + consumed)
            yield header, f.read(size)


def read_headers(path, max_hdus=256):
    """Return a list of header dicts, one per HDU, without reading pixel data.

    Raises ValueError if the file isn't FITS, or OSError if it can't be read.
    `max_hdus` is a guard against a corrupt file producing an endless walk."""
    headers = []
    with open(path, 'rb') as f:
        first = f.read(BLOCK)
        if not first.startswith(b'SIMPLE'):
            raise ValueError(f"{os.path.basename(path)} is not a FITS file")
        f.seek(0)
        while len(headers) < max_hdus:
            start = f.tell()
            chunk = f.read(BLOCK)
            if len(chunk) < BLOCK:
                break
            # A header can span several blocks; keep reading until END.
            text = chunk.decode('ascii', errors='replace')
            header, consumed = parse_header_block(text)
            while consumed >= len(text) and 'END' not in text[-BLOCK:]:
                more = f.read(BLOCK)
                if len(more) < BLOCK:
                    break
                text += more.decode('ascii', errors='replace')
                header, consumed = parse_header_block(text)
            headers.append(header)
            f.seek(start + consumed + _data_size(header))
    return headers
