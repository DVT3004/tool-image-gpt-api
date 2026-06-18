# -*- coding: utf-8 -*-
"""Tao lai sheet HANH DONG nhieu frame cho nhan vat chinh = 6 frame / sheet (luoi 3x2),
KHONG dung AI tao luoi (de bi tran o). Thay vao do:
  1) Gen TUNG frame rieng (1 nhan vat / 1 anh, nen magenta FF00FF, le rong).
  2) Chroma-key + auto-crop tung frame.
  3) GHEP thanh sheet 3 cot x 2 hang, moi o cung size, canh chan (baseline) + giua,
     co LE trong moi o -> tuyet doi khong frame nao tran sang o khac.
Luu -> TuTien\\Assets\\Art\\Sprites\\c_attack.png / c_skill.png / c_run.png
Raw tung frame -> Assets\\Art\\_raw\\c_xxx_fN.png (co san thi BO QUA gen, tru khi FORCE=1).

Chay:
    cd flow-tool
    set PYTHONUTF8=1 & set PYTHONIOENCODING=utf-8 & set FLOW_MODEL=NARWHAL
    python regen6.py c_attack c_skill        # hoac khong tham so = ca 3
"""
import sys, os, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from flow_accounts import AccountManager
from flow_multi import MultiFlow
from flow_config import ASPECT_MAP
from PIL import Image
import numpy as np

BASE = pathlib.Path(r"C:\Users\Admin\Downloads\Game\TuTien\Assets")
RAW = BASE / "Art" / "_raw"
SPR = BASE / "Art" / "Sprites"
for d in (RAW, SPR):
    d.mkdir(parents=True, exist_ok=True)

FORCE = os.environ.get("FORCE", "0") == "1"

KEEP = (" Keep the EXACT SAME character identity as the reference image: same ROUND PLAIN WHITE"
        " panda-like head with a calm simple face (small black dot eyes, tiny nose, soft cheeks),"
        " same BROWN wide-brim woven straw traveler hat, same flowing BLACK cloak with a fluffy"
        " textured WHITE fur collar, same BLUE-GREY layered hanfu robe with white inner lining,"
        " wrap-over front and a tied sash belt with hanging ends."
        " KEEP THE EXACT SAME PROPORTIONS as the reference in EVERY frame: a BIG round head about"
        " two-fifths of the total body height, on a small sturdy chibi body with short arms and"
        " short legs; the head size and the body size MUST be IDENTICAL across all frames. Do NOT"
        " distort the anatomy, do NOT stretch, squash or shrink the body, draw correct clean anatomy"
        " with both arms and both hands clearly visible and both legs visible. Same flat bold-outline"
        " cartoon style and same colours. ONLY the body POSE changes between frames.")

# Anh DON: 1 nhan vat duy nhat, toan than, le rong, nen magenta phu kin, khong khung.
SINGLE = (" . SINGLE 2D game character sprite, ONE character ONLY, the WHOLE body fully inside the"
          " image with a GENEROUS empty margin on every side, three-quarter side view FACING RIGHT."
          " HIGHLY DETAILED clean hand-drawn cartoon: crisp confident bold black outlines, rich cel"
          " shading with clear light and shadow zones, a thin bright rim light along the edges,"
          " visible cloth folds and wrinkles flowing through the robe and cloak, soft fluffy strands"
          " of fur on the white collar, woven straw texture on the hat brim, layered robe hems and a"
          " neatly knotted sash with dangling ribbon ends, individual fingers clearly drawn gripping"
          " the sword hilt, the spirit sword glowing with a bright white-cyan core fading into a soft"
          " outer glow gradient. Polished, detailed, game-ready quality."
          " Solid uniform flat magenta background hex FF00FF that COMPLETELY fills the ENTIRE image"
          " edge to edge. Absolutely NO second character, NO box, NO panel, NO border, NO card, NO"
          " text, NO numbers, NO grid, NO watermark, NO UI. Any energy/sword effect stays SMALL and"
          " well inside the margin.")

# So cot luoi cho moi action (so frame = len(poses), nen chia het cho COLS).
COLS = {"c_attack": 4, "c_skill": 4, "c_run": 4}

# Moi action: danh sach POSE rieng, tang dan TUNG CHUT MOT (trai->phai, hang tren->duoi)
# de animation MUOT, KHONG nhay coc. 12 frame / action.
ACTIONS = {
    "c_attack": [
        "Calm guard stance, the cyan spirit sword held pointing forward to the RIGHT, roughly HORIZONTAL at 0 degrees, relaxed",
        "Begin raising the cyan sword, the blade now pointing to the upper RIGHT at about 30 degrees above horizontal, wind-up starting",
        "Raising more, the cyan blade pointing to the upper RIGHT at about 60 degrees above horizontal, body coiling back",
        "The cyan blade pointing STRAIGHT UP at about 90 degrees, arms raised, body coiled, knees bent ready",
        "The cyan blade tilted up and BACK over the head at about 120 degrees, full wind-up, weight on the back foot",
        "Peak wind-up, the cyan blade pointing BACK behind the head at about 150 degrees, fully coiled to strike",
        "Slash starts, the cyan blade swinging forward-down to about 120 degrees, a thin faint cyan crescent beginning at the edge",
        "Slash descending, the cyan blade at about 90 degrees coming down fast, a short bright cyan crescent trailing the blade",
        "Slash continuing, the cyan blade at about 60 degrees above horizontal sweeping down to the RIGHT, a longer bright cyan crescent arc",
        "Strong slash, the cyan blade at about 30 degrees above horizontal pointing forward-right, a long bright cyan crescent arc to the RIGHT",
        "Full extension, the cyan blade HORIZONTAL at 0 degrees extended out to the RIGHT, the longest bright cyan crescent slash trail to the RIGHT",
        "Follow-through, the cyan blade angled slightly DOWN to about minus 20 degrees toward the lower RIGHT, the cyan trail thinning and fading",
    ],
    "c_skill": [
        "Calm casting stance, the cyan spirit sword held low at the side pointing down-forward, quiet",
        "Raising the cyan sword to point STRAIGHT UP at about 90 degrees, faint thin cyan qi wisps starting around the blade",
        "Sword held STRAIGHT UP steady at 90 degrees, soft cyan qi beginning to swirl around the raised blade",
        "Sword held STRAIGHT UP steady at 90 degrees, brighter cyan qi spiraling around the blade",
        "Sword held STRAIGHT UP steady at 90 degrees, a small faint cyan orb of sword-qi starting to form at the blade tip",
        "Sword held STRAIGHT UP steady at 90 degrees, the cyan orb at the tip a bit bigger and brighter",
        "Sword held STRAIGHT UP steady at 90 degrees, a bright glowing compact cyan sword-qi orb fully formed at the tip, body leaning back slightly",
        "Bringing the sword down to aim, the cyan blade now at about 45 degrees pointing to the upper RIGHT, the glowing orb leading the tip",
        "Thrust beginning, the cyan sword swung to point forward to the RIGHT HORIZONTAL at 0 degrees, the orb leading the tip",
        "Thrust forward, the cyan sword fully extended HORIZONTAL to the RIGHT, releasing the gathered energy forward",
        "Release, a small compact cyan sword-qi burst launching from the blade tip flying to the RIGHT, arm extended",
        "Recovery, the arm recoiling back, residual soft cyan glow fading, lowering the sword to the calm stance",
    ],
    "c_run": [
        "Run cycle moving RIGHT, contact pose: right foot just striking the ground ahead, arms swinging, cloak streaming back",
        "Run cycle moving RIGHT, slight down pose: body lowering a little absorbing weight, cloak trailing",
        "Run cycle moving RIGHT, low recoil pose: body at its lowest, knees bent, leaning forward, cloak trailing",
        "Run cycle moving RIGHT, passing pose: legs passing under the body mid-stride, body leaning forward",
        "Run cycle moving RIGHT, rising pose: pushing up off the ground, body lifting, cloak starting to flare",
        "Run cycle moving RIGHT, high-reach pose: body lifted highest, airborne, cloak flaring back wide",
        "Run cycle moving RIGHT, opposite contact pose: LEFT foot striking the ground ahead, arms swung opposite, cloak streaming back",
        "Run cycle moving RIGHT, opposite slight down pose: body lowering a little, cloak trailing",
        "Run cycle moving RIGHT, opposite low recoil pose: body at its lowest with the other leg, leaning forward, cloak trailing",
        "Run cycle moving RIGHT, opposite passing pose: the other leg passing under the body mid-stride, leaning forward",
        "Run cycle moving RIGHT, opposite rising pose: pushing up off the ground with the other leg, cloak starting to flare",
        "Run cycle moving RIGHT, opposite high-reach pose: body lifted high airborne with the other leg, cloak flaring back wide",
    ],
}


def key_magenta(arr):
    r = arr[:, :, 0].astype(np.int16); g = arr[:, :, 1].astype(np.int16); b = arr[:, :, 2].astype(np.int16)
    mask = (r > 135) & (b > 135) & (g < 135) & ((r - g) > 40) & ((b - g) > 40)
    arr[mask, 3] = 0
    edge = (~mask) & (r > g + 35) & (b > g + 35) & (arr[:, :, 3] > 0)
    avg = ((r + b) // 2); newv = ((g + avg) // 2).astype(np.uint8)
    arr[edge, 0] = np.minimum(arr[edge, 0], newv[edge])
    arr[edge, 2] = np.minimum(arr[edge, 2], newv[edge])
    return arr


def autocrop(im):
    a = np.array(im)
    alpha = a[:, :, 3]
    ys, xs = np.where(alpha > 16)
    if len(xs) == 0:
        return im
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return im.crop((int(x0), int(y0), int(x1), int(y1)))


def find_raw(name):
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = RAW / ("%s.%s" % (name, ext))
        if p.exists():
            return p
    return None


def gen_frame(multi, model, ref_bytes, action, idx, pose):
    fname = "%s_f%d" % (action, idx)
    if not FORCE and find_raw(fname):
        print("SKIP(exists)", fname, flush=True)
        return True
    prompt = pose + KEEP + SINGLE
    try:
        imgs = multi.generate(prompt=prompt, model=model, aspect=ASPECT_MAP["3:4"], n=1,
                              seed=None, retries=2, image_bytes=ref_bytes,
                              log=lambda m: None, detailed=True)
    except Exception as e:
        print("GEN-ERROR", fname, repr(e), flush=True); return False
    for d in imgs:
        b = d.get("bytes")
        if not b:
            continue
        ext = "png" if b[:8] == b"\x89PNG\r\n\x1a\n" else ("jpg" if b[:3] == b"\xff\xd8\xff" else "webp")
        (RAW / ("%s.%s" % (fname, ext))).write_bytes(b)
        print("GEN-SAVED", fname, ext, flush=True); return True
    print("GEN-NOIMG", fname, flush=True); return False


def build_sheet(action):
    poses = ACTIONS[action]
    n = len(poses)
    cols = COLS.get(action, 4)
    rows = (n + cols - 1) // cols
    crops = []
    for i in range(n):
        p = find_raw("%s_f%d" % (action, i))
        if not p:
            print("MISS-FRAME", action, i, flush=True); return False
        im = Image.open(p).convert("RGBA")
        im = Image.fromarray(key_magenta(np.array(im)), "RGBA")
        crops.append(autocrop(im))
    # --- CHUAN HOA TI LE: dua chieu cao moi frame ve gan median (clamp) ---
    # chong viec AI gen moi frame to/nho khac nhau -> nhan vat phinh/teo giua cac frame.
    # van giu squash/stretch NHE cua dong tac (clamp 0.90..1.10).
    hs = sorted(c.height for c in crops)
    med = hs[len(hs) // 2]
    norm = []
    for c in crops:
        f = med / float(c.height)
        f = max(0.90, min(1.10, f))
        nw = max(1, int(round(c.width * f)))
        nh = max(1, int(round(c.height * f)))
        norm.append(c.resize((nw, nh), Image.LANCZOS))
    crops = norm
    maxw = max(c.width for c in crops)
    maxh = max(c.height for c in crops)
    mar = int(round(max(maxw, maxh) * 0.10)) + 4   # le moi canh
    cellw = maxw + 2 * mar
    cellh = maxh + 2 * mar
    sheet = Image.new("RGBA", (cellw * cols, cellh * rows), (0, 0, 0, 0))
    for i, c in enumerate(crops):
        col = i % cols; row = i // cols
        cx = col * cellw + (cellw - c.width) // 2          # giua ngang
        cy = row * cellh + (cellh - mar - c.height)        # canh chan (baseline) o day o, chua le
        sheet.paste(c, (cx, cy), c)
    out = SPR / (action + ".png")
    sheet.save(out)
    print("SHEET", action, "->", sheet.size, "cell", (cellw, cellh),
          "grid", "%dx%d" % (cols, rows), "frames=%d" % n, flush=True)
    return True


def main():
    names = [n for n in (sys.argv[1:] or list(ACTIONS)) if n in ACTIONS]
    model = os.environ.get("FLOW_MODEL", "NARWHAL")
    print("MODEL", model, "FORCE", FORCE, flush=True)
    refp = find_raw("base_ref")
    if not refp:
        print("REF-MISS base_ref", flush=True); return 1
    ref_bytes = refp.read_bytes()
    mgr = AccountManager()
    multi = MultiFlow(mgr, max_workers=2)
    for action in names:
        print("=== ACTION", action, "===", flush=True)
        ok = True
        for i, pose in enumerate(ACTIONS[action]):
            if not gen_frame(multi, model, ref_bytes, action, i, pose):
                ok = False
        if ok:
            build_sheet(action)
        else:
            print("SKIP-BUILD (missing frames)", action, flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
