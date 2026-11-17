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


def main(pdf):
    s = DPI / 72
    x0, y0, w, h = int(CROP["x0"] * s), int(CROP["y0"] * s), int((CROP["x1"] - CROP["x0"]) * s), int((CROP["y1"] - CROP["y0"]) * s)
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-r", str(DPI), "-x", str(x0), "-y", str(y0), "-W", str(w), "-H", str(h), "-png", "-singlefile",
                    pdf, str(OUT / "site-plan")], check=True)
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
