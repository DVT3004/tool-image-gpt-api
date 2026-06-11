# -*- coding: utf-8 -*-
"""
CLI tạo ảnh Flow - Hỗ trợ Đa tài khoản và Đa luồng.

Chuẩn bị tài khoản:
  Cách 1: Thả file cookie (ví dụ: acc1.txt, acc2.txt) chứa chuỗi cookie vào thư mục `cookies/`
  Cách 2: Quản lý qua CLI `flow_cookies_cli.py` (xem hướng dẫn ở file đó)
  Cách 3: Sử dụng GUI (`run_gui.bat`) để nạp tài khoản

Chạy:
  python generate.py "a cute cat sitting on a sofa"
  python generate.py "phong canh nui" --model NARWHAL --aspect PORTRAIT --n 4 --workers 4
"""

import sys
import argparse
import pathlib
import time
import random

from flow_accounts import AccountManager
from flow_multi import MultiFlow

ASPECT_MAP = {
    "LANDSCAPE": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "PORTRAIT": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "SQUARE": "IMAGE_ASPECT_RATIO_SQUARE",
}

COOKIE_FILE = pathlib.Path(__file__).parent / "flow_cookie.txt"


def main():
    parser = argparse.ArgumentParser(description="Tạo ảnh qua Google Labs Flow API (Đa tài khoản & Đa luồng)")
    parser.add_argument("prompt", help="Mô tả ảnh")
    parser.add_argument("--model", default="NARWHAL", help="Model ảnh (mặc định NARWHAL)")
    parser.add_argument("--aspect", default="LANDSCAPE",
                        choices=list(ASPECT_MAP.keys()), help="Tỉ lệ khung")
    parser.add_argument("--n", type=int, default=1, help="Số ảnh cần tạo")
    parser.add_argument("--seed", type=int, default=None, help="Seed (mặc định ngẫu nhiên)")
    parser.add_argument("--workers", type=int, default=4, help="Số luồng chạy song song")
    parser.add_argument("--out", default="output", help="Thư mục lưu ảnh")
    args = parser.parse_args()

    mgr = AccountManager()
    
    # Tự động nạp cookies từ thư mục cookies/
    mgr.autoload_cookie_files()
    
    # Fallback cho flow_cookie.txt cũ
    if not mgr.accounts and COOKIE_FILE.exists():
        cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
        if cookie:
            print(f"[Flow] Tự động nạp cookie từ {COOKIE_FILE} thành tài khoản 'default'...")
            mgr.add_or_update_with_cookies("default", cookie)
            
    healthy = mgr.healthy_accounts()
    if not healthy:
        print("[LỖI] Không có tài khoản Flow nào sẵn sàng (hoặc bị cooldown/chưa đăng nhập).")
        print("\nHướng dẫn thêm tài khoản:")
        print("  1. Thả các file cookie vào thư mục 'cookies/' (ví dụ: cookies/acc1.txt).")
        print("  2. Hoặc dùng CLI: python flow_cookies_cli.py import <tên_acc> --file <đường_dẫn_cookie>")
        print("  3. Hoặc mở GUI quản lý tài khoản.")
        sys.exit(1)
        
    print(f"[Flow] Đang chạy với {len(healthy)}/{len(mgr.accounts)} tài khoản sẵn sàng.")
    
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(exist_ok=True, parents=True)
    
    # Khởi tạo MultiFlow
    multi = MultiFlow(mgr, max_workers=args.workers)
    
    print(f"[Flow] Bắt đầu tạo {args.n} ảnh sử dụng tối đa {args.workers} luồng...")
    
    try:
        t0 = time.time()
        images = multi.generate(
            prompt=args.prompt,
            model=args.model,
            aspect=ASPECT_MAP[args.aspect],
            n=args.n,
            seed=args.seed,
        )
        t1 = time.time()
        
        print(f"\n=== HOÀN TẤT ({t1 - t0:.1f}s) ===")
        for i, img_bytes in enumerate(images):
            fname = out_dir / f"flow_{int(time.time())}_{i}_{random.randint(1000, 9999)}.png"
            fname.write_bytes(img_bytes)
            print(f" - {fname.resolve()}")
            
    except Exception as e:
        print(f"[LỖI] {e}")
        sys.exit(1)
    finally:
        mgr.stop_all()


if __name__ == "__main__":
    main()

