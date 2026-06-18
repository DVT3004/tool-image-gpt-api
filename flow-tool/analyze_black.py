# -*- coding: utf-8 -*-
"""Quet cac PNG sprite/icon -> bao cao cai nao bi DEN/HONG."""
import pathlib
from PIL import Image
import numpy as np

DIRS = [
    pathlib.Path(r"C:\Users\Admin\Downloads\Game\TuTien\Assets\Art\Sprites"),
    pathlib.Path(r"C:\Users\Admin\Downloads\Game\TuTien\Assets\Resources\Icons"),
]

def analyze(p):
    img = Image.open(p).convert("RGBA")
    arr = np.asarray(img).astype(np.float32)
    a = arr[:, :, 3]
    total = a.size
    opaque = a > 30
    n_op = int(opaque.sum())
    frac_op = n_op / total
    if n_op == 0:
        return dict(name=p.stem, dir=p.parent.name, size=img.size, frac_opaque=0.0,
                    dark_frac=1.0, mean=0.0, verdict="EMPTY/ALL-TRANSPARENT")
    rgb = arr[:, :, :3]
    lum = 0.299*rgb[:,:,0] + 0.587*rgb[:,:,1] + 0.114*rgb[:,:,2]
    op_lum = lum[opaque]
    dark_frac = float((op_lum < 45).mean())
    mean = float(op_lum.mean())
    verdict = "ok"
    if frac_op < 0.02:
        verdict = "ALMOST-EMPTY"
    elif dark_frac > 0.70 or mean < 35:
        verdict = "BLACK"
    elif dark_frac > 0.55:
        verdict = "MOSTLY-DARK?"
    return dict(name=p.stem, dir=p.parent.name, size=img.size, frac_opaque=round(frac_op,3),
                dark_frac=round(dark_frac,3), mean=round(mean,1), verdict=verdict)

rows = []
for d in DIRS:
    if not d.exists():
        continue
    for p in sorted(d.glob("*.png")):
        try:
            rows.append(analyze(p))
        except Exception as e:
            rows.append(dict(name=p.stem, dir=d.name, verdict="ERROR:"+repr(e)))

bad = [r for r in rows if r.get("verdict") not in ("ok",)]
print("=== FLAGGED (black/empty/dark) ===")
for r in sorted(bad, key=lambda x: x.get("verdict","")):
    print(f"{r['verdict']:>22}  {r['dir']:>8}/{r['name']:<16} opaque={r.get('frac_opaque')} dark={r.get('dark_frac')} mean={r.get('mean')} size={r.get('size')}")
print()
print("=== OK ===")
for r in rows:
    if r.get("verdict")=="ok":
        print(f"  ok  {r['dir']:>8}/{r['name']:<16} opaque={r['frac_opaque']} dark={r['dark_frac']} mean={r['mean']}")
print("\nTOTAL", len(rows), "FLAGGED", len(bad))
