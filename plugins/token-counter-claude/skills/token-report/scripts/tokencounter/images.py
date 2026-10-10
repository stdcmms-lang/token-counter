"""Image accounting.

Images are not BPE-tokenizable; they are billed by a formula over pixel dimensions.  A
text-only extractor silently undercounts an image-heavy rollout by two orders of magnitude
(ARCHITECTURE.md section 5.4), so images get their own category.

Only enough of the base64 payload is decoded to read the dimension header -- verified
sufficient at 4 KiB for every image in the development corpus (1,100 PNG, 389 JPEG).

The billing formula is model-specific and unpublished for current models, so this reports a
**range** across the two published formula families rather than a single number, and the
spread is folded into the reconciliation residual.
"""
import base64
import math
import re
import struct

PREFIX_BYTES = 4096
DATA_URL_RE = re.compile(r'^data:image/([a-zA-Z0-9.+-]+);base64,')


def _decode_prefix(image_url, nbytes=PREFIX_BYTES):
    if not isinstance(image_url, str):
        return None
    m = DATA_URL_RE.match(image_url)
    if not m:
        return None
    b64 = image_url[m.end():]
    # base64 decodes in 4-char groups; take a whole number of groups.
    take = (nbytes // 3 + 2) * 4
    chunk = b64[:take]
    chunk = chunk[:len(chunk) - (len(chunk) % 4)]
    try:
        return base64.b64decode(chunk, validate=False)
    except Exception:
        return None


def _png(b):
    if len(b) >= 24 and b[:8] == b'\x89PNG\r\n\x1a\n' and b[12:16] == b'IHDR':
        w, h = struct.unpack('>II', b[16:24])
        return 'png', w, h
    return None


def _jpeg(b):
    if len(b) < 4 or b[0:2] != b'\xff\xd8':
        return None
    i = 2
    n = len(b)
    while i + 3 < n:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker == 0xFF:              # fill byte: a marker may be padded by any run of FF
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return None
        seglen = struct.unpack('>H', b[i + 2:i + 4])[0]
        # SOF0..SOF15 except DHT(C4), JPGA(C8), DAC(CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h, w = struct.unpack('>HH', b[i + 5:i + 9])
            return 'jpeg', w, h
        i += 2 + seglen
    return None


def _gif(b):
    if len(b) >= 10 and b[:3] == b'GIF':
        w, h = struct.unpack('<HH', b[6:10])
        return 'gif', w, h
    return None


def _webp(b):
    if len(b) < 30 or b[:4] != b'RIFF' or b[8:12] != b'WEBP':
        return None
    fmt = b[12:16]
    if fmt == b'VP8X':
        w = int.from_bytes(b[24:27], 'little') + 1
        h = int.from_bytes(b[27:30], 'little') + 1
        return 'webp', w, h
    if fmt == b'VP8 ' and len(b) >= 30:
        w = struct.unpack('<H', b[26:28])[0] & 0x3FFF
        h = struct.unpack('<H', b[28:30])[0] & 0x3FFF
        return 'webp', w, h
    if fmt == b'VP8L' and len(b) >= 25:
        bits = int.from_bytes(b[21:25], 'little')
        return 'webp', (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _bmp(b):
    if len(b) >= 26 and b[:2] == b'BM':
        w, h = struct.unpack('<ii', b[18:26])
        return 'bmp', abs(w), abs(h)
    return None


_SNIFFERS = (_png, _jpeg, _gif, _webp, _bmp)


def dimensions(image_url):
    """``(format, width, height)`` from the payload prefix, or ``None`` if unrecognised."""
    b = _decode_prefix(image_url)
    if not b:
        return None
    for fn in _SNIFFERS:
        try:
            got = fn(b)
        except Exception:
            got = None
        if got:
            return got
    return None


def patch_tokens(w, h, patch=32, max_patches=1536):
    """Patch-family formula (GPT-4.1-mini / GPT-5 style).

    The image is scaled to fit within `max_patches` patches of `patch` pixels a side.
    """
    if w <= 0 or h <= 0:
        return 0
    pw, ph = math.ceil(w / patch), math.ceil(h / patch)
    if pw * ph <= max_patches:
        return pw * ph
    shrink = math.sqrt(max_patches * patch * patch / (w * h))
    w2, h2 = w * shrink, h * shrink
    pw = math.floor(w2 / patch) or 1
    scale2 = (pw * patch) / w2
    ph = math.ceil(h2 * scale2 / patch) or 1
    while pw * ph > max_patches:
        ph -= 1
        if ph <= 0:
            ph = 1
            break
    return pw * ph


def tile_tokens(w, h, base=85, per_tile=170):
    """Tile-family formula (GPT-4o style): fit 2048, shortest side 768, 512px tiles."""
    if w <= 0 or h <= 0:
        return 0
    if max(w, h) > 2048:
        s = 2048 / max(w, h)
        w, h = w * s, h * s
    if min(w, h) > 768:
        s = 768 / min(w, h)
        w, h = w * s, h * s
    tiles = math.ceil(w / 512) * math.ceil(h / 512)
    return base + per_tile * tiles


def estimate(image_url):
    """``{'format', 'width', 'height', 'lo', 'hi'}`` or ``{'format': None, ...}``.

    Unknown formats resolve to the ambiguous state (``lo=0``, ``hi=None``), never to a
    silent zero.
    """
    got = dimensions(image_url)
    if not got:
        return {'format': None, 'width': None, 'height': None, 'lo': 0, 'hi': None}
    fmt, w, h = got
    a, b = patch_tokens(w, h), tile_tokens(w, h)
    return {'format': fmt, 'width': w, 'height': h, 'lo': min(a, b), 'hi': max(a, b)}
