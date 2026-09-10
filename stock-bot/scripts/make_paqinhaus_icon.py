"""Build a Paqinhaus poster-style .ico for the Stock-bot desktop shortcut."""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "dashboard.ico"
PNG_OUT = ROOT / "assets" / "dashboard.png"
PIRATA = ROOT / "assets" / "fonts" / "PirataOne-Regular.ttf"

BG = (11, 11, 14, 255)
CYAN = (31, 168, 239, 255)
CREAM = (242, 235, 224, 255)
CARD = (26, 26, 31, 255)
GREEN = (126, 193, 58, 255)


def _font(size: int) -> ImageFont.ImageFont:
    if PIRATA.is_file():
        try:
            return ImageFont.truetype(str(PIRATA), size=size)
        except OSError:
            pass
    try:
        return ImageFont.truetype("C:/Windows/Fonts/georgia.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 32)
    d.rounded_rectangle(
        [pad, pad, size - pad - 1, size - pad - 1],
        radius=max(2, size // 8),
        fill=BG,
    )
    # Cyan hairline
    bar_h = max(2, size // 11)
    d.rectangle([pad, pad, size - pad - 1, pad + bar_h], fill=CYAN)

    inset = max(2, size // 9)
    top = pad + bar_h + max(1, size // 20)
    d.rounded_rectangle(
        [inset, top, size - inset - 1, size - inset - 1],
        radius=max(2, size // 12),
        fill=CARD,
        outline=(46, 44, 40, 255),
        width=max(1, size // 48),
    )

    # Wordmark band
    mark_h = 0
    if size >= 48:
        f = _font(max(14, int(size * 0.28)))
        label = "Sb"
        bbox = d.textbbox((0, 0), label, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        mark_h = th + max(2, size // 24)
        d.text(((size - tw) / 2, top + max(1, size // 40) - bbox[1]), label, font=f, fill=CREAM)
        # Cyan stamp under wordmark on large icons
        if size >= 96:
            stamp = "NYSE"
            sf = _font(max(8, size // 14))
            sb = d.textbbox((0, 0), stamp, font=sf)
            sw, sh = sb[2] - sb[0], sb[3] - sb[1]
            sx = (size - sw) // 2 - 4
            sy = top + mark_h - 2
            d.rounded_rectangle(
                [sx, sy, sx + sw + 8, sy + sh + 4],
                radius=3,
                fill=CYAN,
            )
            d.text((sx + 4, sy + 2 - sb[1]), stamp, font=sf, fill=BG)
            mark_h += sh + 8

    chart_l = inset + max(2, size // 12)
    chart_r = size - inset - max(2, size // 12)
    chart_t = top + max(mark_h, size // 7) + max(2, size // 32)
    chart_b = size - inset - max(3, size // 10)
    pts = [
        (0.00, 0.82),
        (0.18, 0.66),
        (0.34, 0.72),
        (0.50, 0.44),
        (0.66, 0.50),
        (0.82, 0.22),
        (1.00, 0.12),
    ]
    line = [
        (
            int(chart_l + t * (chart_r - chart_l)),
            int(chart_t + y * (chart_b - chart_t)),
        )
        for t, y in pts
    ]
    stroke = max(2, size // 20)
    if len(line) >= 2:
        # Soft fill under curve
        fill_pts = [(chart_l, chart_b)] + line + [(chart_r, chart_b)]
        d.polygon(fill_pts, fill=(31, 168, 239, 55))
        d.line(line, fill=CYAN, width=stroke, joint="curve")
    tx, ty = line[-1]
    r = max(2, size // 15)
    d.ellipse([tx - r, ty - r, tx + r, ty + r], fill=GREEN)
    return img


def _png_bytes(im: Image.Image) -> bytes:
    import io

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _write_ico(path: Path, images: list[Image.Image]) -> None:
    """Write a multi-size ICO with PNG-compressed entries (Vista+)."""
    entries: list[tuple[int, int, bytes]] = []
    for im in images:
        w, h = im.size
        entries.append((w, h, _png_bytes(im.convert("RGBA"))))

    count = len(entries)
    offset = 6 + 16 * count
    header = struct.pack("<HHH", 0, 1, count)
    dir_chunks: list[bytes] = []
    blobs: list[bytes] = []
    for w, h, blob in entries:
        wb = 0 if w >= 256 else w
        hb = 0 if h >= 256 else h
        dir_chunks.append(struct.pack("<BBBBHHII", wb, hb, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)

    path.write_bytes(header + b"".join(dir_chunks) + b"".join(blobs))


def main() -> int:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    frames = [_render(s) for s in sizes]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    _write_ico(OUT, frames)
    frames[-1].save(PNG_OUT, format="PNG")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(frames)} sizes)")
    print(f"wrote {PNG_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
