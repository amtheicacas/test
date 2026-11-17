"""Build the map's Site plan view from a plat / site-plan PDF whose lot numbers are real text.

    python tools/build_site_plan.py "Heron_Landing_Plat_Map.pdf"

Writes violations/static/violations/site-plan.png (the artwork, cropped to the map) and site-plan-lots.json (where each lot
number sits, as fractions of the image). Needs poppler's pdftoppm and pdftotext on the PATH. The crop box below is in PDF points
and fits the Meritage Homes Heron Landing plat; adjust it for a different PDF."""
import json
import re
import subprocess
import sys
from pathlib import Path

CROP = dict(x0=15, y0=88, x1=600, y1=700)  # PDF points: the map only, without the header and the legal footer
DPI = 200
N_LOTS = 95
OUT = Path(__file__).resolve().parent.parent / "violations" / "static" / "violations"


LOT_GREY = (227, 231, 222)  # the colour of an ordinary lot in the artwork


def clean_plan(path):
    """Takes the builder's marketing extras off the artwork: the yellow highlight on the model-home lots and the 'MODELS' / 'PRKG' captions.
    The yellow lots become ordinary lots and the captions are erased. Coordinates are those of the 1625x1700 crop at 200 dpi."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    px, (w, h) = im.load(), im.size
    bx0, by0, bx1, by1 = 822, 1272, 900, 1416                       # the three yellow lots (62, 63, 64) and their captions
    caption_x1 = 851                                                 # captions sit left of the lot numbers, which are kept
    caption = set()
    for y in range(by0, by1):
        for x in range(bx0, caption_x1):
            r, g, b = px[x, y]
            if (r + g + b) / 3 < 175 and not (r > 230 and b < 190):  # dark text (not the yellow fill)
                for dy in (-2, -1, 0, 1, 2):                         # include the anti-aliased halo
                    for dx in (-2, -1, 0, 1, 2):
                        caption.add((x + dx, y + dy))
    for y in range(by0, by1):
        for x in range(bx0, bx1):
            r, g, b = px[x, y]
            if (r >= 225 and r - b > 20 and g > 200) or (x, y) in caption:   # yellow fill and its anti-aliased edge, or caption text
                px[x, y] = LOT_GREY
    for y in range(by0, by1):                                        # the lot numbers were tinted by the yellow behind them: make them neutral
        for x in range(caption_x1, bx1):
            r, g, b = px[x, y]
            if r - b > 12 and (r + g + b) / 3 < 200:
                v = int((r + g + b) / 3 * 0.8)
                px[x, y] = (v, v, v)
    im.save(path, optimize=True)


def main(pdf):
    s = DPI / 72
    x0, y0, w, h = int(CROP["x0"] * s), int(CROP["y0"] * s), int((CROP["x1"] - CROP["x0"]) * s), int((CROP["y1"] - CROP["y0"]) * s)
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-r", str(DPI), "-x", str(x0), "-y", str(y0), "-W", str(w), "-H", str(h), "-png", "-singlefile",
                    pdf, str(OUT / "site-plan")], check=True)
    clean_plan(str(OUT / "site-plan.png"))
    html = subprocess.run(["pdftotext", "-bbox", pdf, "-"], check=True, capture_output=True, text=True).stdout
    lots = {}
    for a, b, c, d, word in re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', html):
        if word.isdigit() and 1 <= int(word) <= N_LOTS:
            cx, cy = (float(a) + float(c)) / 2, (float(b) + float(d)) / 2
            if CROP["y0"] < cy < CROP["y1"] - 15 and CROP["x0"] < cx < CROP["x1"]:  # skips "SR 72" on the road sign at the bottom
                lots.setdefault(int(word), []).append((cx, cy))
    missing = [n for n in range(1, N_LOTS + 1) if n not in lots]
    dup = [n for n, v in lots.items() if len(v) > 1]
    assert not missing and not dup, f"missing {missing}, duplicated {dup}"
    frac = {str(n): [round((v[0][0] * s - x0) / w, 5), round((v[0][1] * s - y0) / h, 5)] for n, v in sorted(lots.items())}
    (OUT / "site-plan-lots.json").write_text(json.dumps({"width": w, "height": h, "lots": frac}, separators=(",", ":")))
    print(f"wrote {OUT / 'site-plan.png'} ({w}x{h}) and positions for {len(frac)} lots")


if __name__ == "__main__":
    main(sys.argv[1])
