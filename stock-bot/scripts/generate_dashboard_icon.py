"""Generate assets/dashboard.ico for shortcuts and PyInstaller.

Run: python scripts/generate_dashboard_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "dashboard.ico"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (20, 20, 20, 255))
    draw = ImageDraw.Draw(img)
    margin = 36
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=42,
        fill=(30, 58, 95, 255),
        outline=(96, 165, 250, 255),
        width=6,
    )
    draw.text((78, 92), "PT", fill=(241, 245, 249, 255))
    img.save(
        OUT,
        format="ICO",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
