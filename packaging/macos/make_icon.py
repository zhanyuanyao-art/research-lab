"""Render the app icon (an archive card with four direction tabs) to icon.icns via PyMuPDF, sips and iconutil."""
import pathlib
import shutil
import subprocess

import pymupdf

HERE = pathlib.Path(__file__).parent
BUILD = HERE / "build"
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <rect x="100" y="100" width="824" height="824" rx="186" ry="186" fill="#1d1d21"/>
  <rect x="272" y="262" width="104" height="120" rx="22" fill="#5b5bd6"/>
  <rect x="396" y="262" width="104" height="120" rx="22" fill="#0e8f82"/>
  <rect x="520" y="262" width="104" height="120" rx="22" fill="#b87a12"/>
  <rect x="644" y="262" width="104" height="120" rx="22" fill="#c23f86"/>
  <rect x="236" y="330" width="552" height="436" rx="44" fill="#f4f4f5"/>
  <rect x="300" y="420" width="300" height="30" rx="15" fill="#18181b"/>
  <rect x="300" y="500" width="424" height="22" rx="11" fill="#b4b4bc"/>
  <rect x="300" y="556" width="400" height="22" rx="11" fill="#b4b4bc"/>
  <rect x="300" y="612" width="424" height="22" rx="11" fill="#b4b4bc"/>
  <rect x="300" y="668" width="250" height="22" rx="11" fill="#b4b4bc"/>
</svg>"""


def main() -> pathlib.Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    svg_path = BUILD / "icon.svg"
    svg_path.write_text(SVG, encoding="utf-8")
    png = BUILD / "icon-1024.png"
    doc = pymupdf.open(svg_path)
    doc[0].get_pixmap(alpha=True).save(png)
    iconset = BUILD / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)], check=True, capture_output=True)
    icns = BUILD / "icon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    return icns


if __name__ == "__main__":
    print(main())
