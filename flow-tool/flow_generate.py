# -*- coding: utf-8 -*-
"""Runner headless: tạo ảnh qua Flow MultiFlow mà KHÔNG cần mở GUI.

Dùng cùng backend với gui_modern.py (AccountManager + MultiFlow).
    python flow_generate.py "<prompt>" [--n 1] [--aspect 1:1] [--model GEM_PIX_2]
Ảnh lưu vào output/. In đường dẫn file kết quả ra stdout (dòng 'SAVED: ...').
"""
import argparse
import os
import pathlib
import random
import sys
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

from flow_accounts import AccountManager          # noqa: E402
from flow_multi import MultiFlow                  # noqa: E402
from flow_config import ASPECT_MAP                # noqa: E402

OUT_DIR = HERE / "output"
OUT_DIR.mkdir(exist_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--aspect", default="1:1", choices=list(ASPECT_MAP.keys()))
    ap.add_argument("--model", default="GEM_PIX_2")
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args()

    mgr = AccountManager()
    states = mgr.states()
    ready = sum(1 for s in states if s.get("status") == "ready")
    print("Tài khoản: %d | sẵn sàng: %d" % (len(states), ready), flush=True)

    multi = MultiFlow(mgr, max_workers=1)
    aspect = ASPECT_MAP.get(args.aspect, "IMAGE_ASPECT_RATIO_SQUARE")
    print("Đang tạo %d ảnh (model=%s, aspect=%s)..." % (args.n, args.model, args.aspect),
          flush=True)

    imgs = multi.generate(prompt=args.prompt, model=args.model, aspect=aspect,
                          n=args.n, seed=None, retries=args.retries,
                          log=lambda m: print("[flow]", m, flush=True),
                          detailed=True)
    saved = []
    for j, d in enumerate(imgs):
        b = d.get("bytes")
        if not b:
            continue
        ext = "png"
        if b[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif b[:4] == b"RIFF":
            ext = "webp"
        fn = OUT_DIR / ("flow_%d_%d_%d.%s" % (int(time.time()), j,
                                              random.randint(1000, 9999), ext))
        fn.write_bytes(b)
        saved.append(str(fn))
        print("SAVED:", fn, flush=True)

    if not saved:
        print("KHÔNG tạo được ảnh nào.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
