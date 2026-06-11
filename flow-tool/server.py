# -*- coding: utf-8 -*-
"""
HTTP server cục bộ để tool vẽ ảnh gọi qua HTTP - Hỗ trợ Đa tài khoản & Đa luồng.

Chạy:
  python server.py            # mặc định http://127.0.0.1:8799
  python server.py --port 9000

API:
  POST /generate
    body JSON: {
       "prompt": "a cat",
       "model": "NARWHAL",          (tuỳ chọn)
       "aspect": "LANDSCAPE",       (LANDSCAPE/PORTRAIT/SQUARE, tuỳ chọn)
       "n": 1,                       (tuỳ chọn)
       "seed": 123,                  (tuỳ chọn)
       "workers": 4                  (số luồng chạy song song, tuỳ chọn)
    }
    -> { "ok": true, "images": ["<đường dẫn ảnh>", ...] }
    -> { "ok": false, "error": "..." }

  GET /health  -> { "ok": true }
"""

import sys
import json
import argparse
import pathlib
import time
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flow_accounts import AccountManager
from flow_multi import MultiFlow

ASPECT_MAP = {
    "LANDSCAPE": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "PORTRAIT": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "SQUARE": "IMAGE_ASPECT_RATIO_SQUARE",
}

COOKIE_FILE = pathlib.Path(__file__).parent / "flow_cookie.txt"

_mgr: AccountManager = None
_out_dir: pathlib.Path = None


def _json(handler, code, obj):
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # tắt log mặc định

    def do_OPTIONS(self):
        _json(self, 200, {"ok": True})

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            healthy = _mgr.healthy_accounts()
            _json(self, 200, {"ok": True, "healthy_accounts": len(healthy), "total_accounts": len(_mgr.accounts)})
        else:
            _json(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/generate":
            _json(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            _json(self, 400, {"ok": False, "error": f"JSON lỗi: {e}"})
            return

        prompt = req.get("prompt")
        if not prompt:
            _json(self, 400, {"ok": False, "error": "Thiếu 'prompt'"})
            return

        aspect = ASPECT_MAP.get(str(req.get("aspect", "LANDSCAPE")).upper(),
                                ASPECT_MAP["LANDSCAPE"])
        
        healthy = _mgr.healthy_accounts()
        if not healthy:
            _json(self, 503, {"ok": False, "error": "Không có tài khoản Flow nào sẵn sàng (bị cooldown hoặc chưa đăng nhập)"})
            return

        n = int(req.get("n", 1))
        workers = int(req.get("workers", 4))
        seed = req.get("seed")
        if seed is not None:
            try:
                seed = int(seed)
            except ValueError:
                seed = None

        try:
            multi = MultiFlow(_mgr, max_workers=workers)
            images_bytes = multi.generate(
                prompt=prompt,
                model=req.get("model", "NARWHAL"),
                aspect=aspect,
                n=n,
                seed=seed,
            )
            
            images = []
            for i, img_bytes in enumerate(images_bytes):
                fname = _out_dir / f"flow_{int(time.time())}_{i}_{random.randint(1000, 9999)}.png"
                fname.write_bytes(img_bytes)
                images.append(str(fname.resolve()))
                
            _json(self, 200, {"ok": True, "images": images})
        except Exception as e:
            _json(self, 500, {"ok": False, "error": str(e)})


def main():
    global _mgr, _out_dir
    parser = argparse.ArgumentParser(description="Flow image server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--out", default="output")
    args = parser.parse_args()

    _out_dir = pathlib.Path(args.out)
    _out_dir.mkdir(exist_ok=True, parents=True)

    _mgr = AccountManager()
    _mgr.autoload_cookie_files()
    
    # Fallback từ cookie cũ
    if not _mgr.accounts and COOKIE_FILE.exists():
        cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
        if cookie:
            print(f"[Server] Tự động nạp cookie từ {COOKIE_FILE} thành tài khoản 'default'...")
            _mgr.add_or_update_with_cookies("default", cookie)

    healthy = _mgr.healthy_accounts()
    print(f"[Server] Đang chạy với {len(healthy)}/{len(_mgr.accounts)} tài khoản sẵn sàng.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[Server] Sẵn sàng tại http://{args.host}:{args.port}")
    print("[Server] POST /generate  |  GET /health  |  Ctrl+C để dừng")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Đang dừng...")
    finally:
        _mgr.stop_all()
        server.server_close()


if __name__ == "__main__":
    main()

