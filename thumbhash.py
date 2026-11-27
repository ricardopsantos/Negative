# summary: ThumbHash encoder (opaque images) for timeline placeholders; the format of github.com/evanw/thumbhash.
"""A ThumbHash is ~25 bytes from which a client draws a blurred preview before the thumbnail arrives. The Immich phone
app and the web app (app.js thumbHashToDataURL) both decode it. Encoding follows the reference implementation
(MIT), vectorised with numpy; rounding is JavaScript's Math.round so hashes match the reference byte for byte."""
import base64
import math

import numpy as np


def _round(x):
    return int(math.floor(x + 0.5))


def _channel(ch, nx, ny):
    h, w = ch.shape
    fx = np.cos(np.pi / w * np.outer(np.arange(nx), np.arange(w) + 0.5))  # nx × w
    fy = np.cos(np.pi / h * np.outer(np.arange(ny), np.arange(h) + 0.5))  # ny × h
    coef = fy @ ch @ fx.T / (w * h)  # ny × nx
    dc, ac = 0.0, []
    for cy in range(ny):
        cx = 0
        while cx * ny < nx * (ny - cy):
            if cx or cy:
                ac.append(float(coef[cy, cx]))
            else:
                dc = float(coef[0, 0])
            cx += 1
    scale = max((abs(f) for f in ac), default=0.0)
    if scale:
        ac = [0.5 + 0.5 / scale * f for f in ac]
    return dc, ac, scale


def encode(im):
    """ThumbHash bytes of a PIL image (any mode; alpha is ignored: photos are opaque)."""
    im = im.convert("RGB")
    if max(im.size) > 100:
        im = im.copy()
        im.thumbnail((100, 100))
    w, h = im.size
    rgb = np.asarray(im, dtype=np.float64) / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lx = max(1, _round(7 * w / max(w, h)))
    ly = max(1, _round(7 * h / max(w, h)))
    l_dc, l_ac, l_scale = _channel((r + g + b) / 3, max(3, lx), max(3, ly))
    p_dc, p_ac, p_scale = _channel((r + g) / 2 - b, 3, 3)
    q_dc, q_ac, q_scale = _channel(r - g, 3, 3)
    landscape = w > h
    h24 = (_round(63 * l_dc) | (_round(31.5 + 31.5 * p_dc) << 6) | (_round(31.5 + 31.5 * q_dc) << 12)
           | (_round(31 * l_scale) << 18))
    h16 = (ly if landscape else lx) | (_round(63 * p_scale) << 3) | (_round(63 * q_scale) << 9) | (int(landscape) << 15)
    out = [h24 & 255, (h24 >> 8) & 255, h24 >> 16, h16 & 255, h16 >> 8]
    for i, f in enumerate(l_ac + p_ac + q_ac):
        u = _round(15 * f)
        if i & 1:
            out[-1] |= u << 4
        else:
            out.append(u)
    return bytes(out)


def encode_b64(im):
    return base64.b64encode(encode(im)).decode()
