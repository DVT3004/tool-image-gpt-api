# -*- coding: utf-8 -*-
"""
Tự lấy cookie đăng nhập Flow từ trình duyệt (Brave/Chrome/Edge) trên Windows.

Đọc file Cookies (SQLite) của trình duyệt, giải mã giá trị (AES-GCM v10/v11 +
DPAPI key trong Local State), rồi dựng chuỗi Cookie header cho các domain Google
cần thiết để gọi labs.google.

Lưu ý: nếu trình duyệt dùng "app-bound encryption" (cookie v20, Chromium mới),
script sẽ báo và bạn cần dán cookie thủ công vào flow_cookie.txt.
"""

import os
import json
import base64
import shutil
import sqlite3
import tempfile
import pathlib
from typing import Dict, List, Optional

try:
    import win32crypt  # từ pywin32
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    from Cryptodome.Cipher import AES
except ImportError:
    from Crypto.Cipher import AES


LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")

# Các trình duyệt Chromium phổ biến và đường dẫn User Data
BROWSERS = {
    "brave": rf"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\User Data",
    "chrome": rf"{LOCALAPPDATA}\Google\Chrome\User Data",
    "edge": rf"{LOCALAPPDATA}\Microsoft\Edge\User Data",
}

# Domain cookie cần để gọi labs.google / aisandbox
WANT_DOMAINS = ("google.com", "labs.google", ".google.com", ".labs.google")


class CookieError(Exception):
    pass


def is_browser_running(browser: str = "brave") -> bool:
    exe = {"brave": "brave.exe", "chrome": "chrome.exe", "edge": "msedge.exe"}.get(browser)
    if not exe:
        return False
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                             capture_output=True, text=True)
        return exe.lower() in out.stdout.lower()
    except Exception:
        return False


def close_browser(browser: str = "brave") -> bool:
    """Đóng hẳn trình duyệt để mở khoá file Cookies. Trả True nếu đã đóng."""
    exe = {"brave": "brave.exe", "chrome": "chrome.exe", "edge": "msedge.exe"}.get(browser)
    if not exe:
        return False
    import subprocess
    import time as _t
    subprocess.run(["taskkill", "/F", "/IM", exe, "/T"],
                   capture_output=True, text=True)
    for _ in range(20):
        if not is_browser_running(browser):
            return True
        _t.sleep(0.3)
    return not is_browser_running(browser)


BRAVE_EXES = [
    rf"{os.environ.get('ProgramFiles', '')}\BraveSoftware\Brave-Browser\Application\brave.exe",
    rf"{os.environ.get('ProgramFiles(x86)', '')}\BraveSoftware\Brave-Browser\Application\brave.exe",
    rf"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe",
]
CHROME_EXES = [
    rf"{os.environ.get('ProgramFiles', '')}\Google\Chrome\Application\chrome.exe",
    rf"{os.environ.get('ProgramFiles(x86)', '')}\Google\Chrome\Application\chrome.exe",
]
EDGE_EXES = [
    rf"{os.environ.get('ProgramFiles(x86)', '')}\Microsoft\Edge\Application\msedge.exe",
    rf"{os.environ.get('ProgramFiles', '')}\Microsoft\Edge\Application\msedge.exe",
]
EXE_MAP = {"brave": BRAVE_EXES, "chrome": CHROME_EXES, "edge": EDGE_EXES}


def _find_exe(browser: str) -> Optional[str]:
    for p in EXE_MAP.get(browser, []):
        if p and os.path.exists(p):
            return p
    return None


def grab_via_cdp(browser: str = "brave", debug_port: int = 9222,
                 keep_open: bool = False) -> str:
    """
    Lấy cookie qua Chrome DevTools Protocol.

    Mở trình duyệt (với profile thật của bạn) kèm cổng debug, để chính trình duyệt
    giải mã cookie trong bộ nhớ -> đọc được kể cả cookie app-bound (v20) và HttpOnly.

    Nếu trình duyệt đang chạy, hàm sẽ đóng nó trước (vì cờ debug chỉ áp dụng khi
    khởi động mới).
    """
    import time as _t
    import subprocess
    import urllib.request

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CookieError("Thiếu playwright. Chạy: pip install playwright")

    exe = _find_exe(browser)
    if not exe:
        raise CookieError(f"Không tìm thấy {browser}.exe để mở.")

    user_data = BROWSERS.get(browser)
    if not user_data:
        raise CookieError(f"Không rõ thư mục profile cho {browser}.")

    if is_browser_running(browser):
        close_browser(browser)
        _t.sleep(1.0)

    proc = subprocess.Popen([
        exe,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data}",
        "--profile-directory=Default",
        "--restore-last-session",
        "https://labs.google/fx/vi/tools/flow",
    ])

    # Chờ endpoint debug sẵn sàng
    version_url = f"http://127.0.0.1:{debug_port}/json/version"
    ws_ready = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(version_url, timeout=1) as r:
                if r.status == 200:
                    ws_ready = True
                    break
        except Exception:
            pass
        _t.sleep(0.5)
    if not ws_ready:
        raise CookieError("Không kết nối được cổng debug của trình duyệt.")

    collected: Dict[str, str] = {}
    try:
        with sync_playwright() as pw:
            browser_obj = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            # Chờ cookie nạp (đăng nhập), thử vài lần
            for _ in range(10):
                for ctx in browser_obj.contexts:
                    for c in ctx.cookies():
                        host = c.get("domain", "")
                        if any(d in host for d in WANT_DOMAINS):
                            collected[c["name"]] = c["value"]
                if "__Secure-next-auth.session-token" in collected or "session-token" in " ".join(collected):
                    break
                _t.sleep(1.0)
            browser_obj.close()
    finally:
        if not keep_open:
            try:
                proc.terminate()
            except Exception:
                pass
            close_browser(browser)

    if not collected:
        raise CookieError("Không đọc được cookie qua CDP. Bạn đã đăng nhập Flow chưa?")
    return "; ".join(f"{k}={v}" for k, v in collected.items())


def grab_cookies_struct(browser: str = "brave", debug_port: int = 9222,
                        keep_open: bool = False) -> list:
    """
    Lấy cookie dạng có cấu trúc (list dict kiểu Playwright) qua CDP, để bơm vào
    trình duyệt khác (vd trình duyệt của captcha solver) cho phiên đăng nhập sẵn.

    Trả về list các dict: {name, value, domain, path, expires, httpOnly, secure, sameSite}.
    """
    import time as _t
    import subprocess
    import urllib.request

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CookieError("Thiếu playwright. Chạy: pip install playwright")

    exe = _find_exe(browser)
    if not exe:
        raise CookieError(f"Không tìm thấy {browser}.exe để mở.")
    user_data = BROWSERS.get(browser)

    if is_browser_running(browser):
        close_browser(browser)
        _t.sleep(1.0)

    proc = subprocess.Popen([
        exe,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data}",
        "--profile-directory=Default",
        "--restore-last-session",
        "https://labs.google/fx/vi/tools/flow",
    ])

    version_url = f"http://127.0.0.1:{debug_port}/json/version"
    ready = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(version_url, timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        _t.sleep(0.5)
    if not ready:
        raise CookieError("Không kết nối được cổng debug của trình duyệt.")

    cookies = []
    try:
        with sync_playwright() as pw:
            bobj = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            seen = {}
            for _ in range(10):
                for ctx in bobj.contexts:
                    for c in ctx.cookies():
                        if any(d in c.get("domain", "") for d in WANT_DOMAINS):
                            seen[(c["name"], c.get("domain"))] = c
                if any(k[0] == "__Secure-next-auth.session-token" for k in seen):
                    break
                _t.sleep(1.0)
            cookies = list(seen.values())
            bobj.close()
    finally:
        if not keep_open:
            try:
                proc.terminate()
            except Exception:
                pass
            close_browser(browser)

    if not cookies:
        raise CookieError("Không đọc được cookie qua CDP. Bạn đã đăng nhập Flow chưa?")
    return cookies


def _get_aes_key(user_data_dir: str) -> bytes:
    local_state = pathlib.Path(user_data_dir) / "Local State"
    if not local_state.exists():
        raise CookieError(f"Không thấy Local State: {local_state}")
    data = json.loads(local_state.read_text(encoding="utf-8"))
    enc_key_b64 = data["os_crypt"]["encrypted_key"]
    enc_key = base64.b64decode(enc_key_b64)
    # bỏ tiền tố 'DPAPI'
    if enc_key[:5] == b"DPAPI":
        enc_key = enc_key[5:]
    if not HAS_WIN32:
        raise CookieError("Thiếu pywin32 (win32crypt) để giải mã key.")
    key = win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
    return key


def _decrypt_value(encrypted: bytes, key: bytes) -> str:
    if not encrypted:
        return ""
    prefix = encrypted[:3]
    if prefix in (b"v10", b"v11"):
        nonce = encrypted[3:15]
        ciphertext = encrypted[15:-16]
        tag = encrypted[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        try:
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8", "ignore")
        except Exception:
            return ""
    if prefix == b"v20":
        # App-bound encryption (Chromium mới) - không hỗ trợ tự động
        raise CookieError("APP_BOUND")
    # Cookie cũ mã hoá bằng DPAPI trực tiếp
    if HAS_WIN32:
        try:
            return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode(
                "utf-8", "ignore")
        except Exception:
            return ""
    return ""


def _raw_read_shared(path: pathlib.Path) -> bytes:
    """Đọc file kể cả khi bị trình duyệt khoá, bằng cờ chia sẻ đầy đủ của Win32."""
    import win32file
    import win32con
    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        0,
        None,
    )
    try:
        chunks = []
        while True:
            hr, data = win32file.ReadFile(handle, 1024 * 1024)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        handle.Close()


def _copy_db(cookie_file: pathlib.Path) -> pathlib.Path:
    """Copy DB ra temp để đọc kể cả khi trình duyệt đang mở."""
    tmp = pathlib.Path(tempfile.gettempdir()) / f"_flow_cookies_{os.getpid()}_{cookie_file.parent.parent.name}.db"
    # Thử copy thường trước
    try:
        shutil.copy2(cookie_file, tmp)
        return tmp
    except Exception:
        pass
    # Khoá độc quyền -> đọc bằng Win32 chia sẻ
    try:
        tmp.write_bytes(_raw_read_shared(cookie_file))
        return tmp
    except Exception as e:
        raise CookieError(f"Không copy được Cookies DB (đang bị khoá): {e}")


def _find_cookie_files(user_data_dir: str) -> List[pathlib.Path]:
    base = pathlib.Path(user_data_dir)
    if not base.exists():
        return []
    found = []
    # Default + các Profile khác
    for prof in ["Default"] + [p.name for p in base.glob("Profile *")]:
        for sub in ["Network/Cookies", "Cookies"]:
            f = base / prof / sub
            if f.exists():
                found.append(f)
    return found


def grab_cookies(browser: str = "brave") -> str:
    """
    Lấy chuỗi Cookie header cho domain Google từ trình duyệt chỉ định.
    Trả về chuỗi dạng "name1=value1; name2=value2; ...".
    """
    user_data = BROWSERS.get(browser)
    if not user_data:
        raise CookieError(f"Trình duyệt không hỗ trợ: {browser}")

    cookie_files = _find_cookie_files(user_data)
    if not cookie_files:
        raise CookieError(f"Không tìm thấy file Cookies cho {browser}: {user_data}")

    key = _get_aes_key(user_data)

    collected: Dict[str, str] = {}
    app_bound = False

    for cf in cookie_files:
        tmp = _copy_db(cf)
        try:
            con = sqlite3.connect(str(tmp))
            con.text_factory = bytes
            cur = con.cursor()
            cur.execute(
                "SELECT host_key, name, encrypted_value FROM cookies"
            )
            for host_key, name, enc_val in cur.fetchall():
                host = host_key.decode("utf-8", "ignore")
                if not any(d in host for d in WANT_DOMAINS):
                    continue
                name = name.decode("utf-8", "ignore")
                try:
                    val = _decrypt_value(enc_val, key)
                except CookieError as e:
                    if str(e) == "APP_BOUND":
                        app_bound = True
                        continue
                    raise
                if val:
                    collected[name] = val
            con.close()
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    if not collected:
        if app_bound:
            raise CookieError(
                "Trình duyệt dùng app-bound encryption (cookie v20) - không giải mã tự động "
                "được. Hãy dán cookie thủ công vào flow_cookie.txt (xem README mục 2)."
            )
        raise CookieError(
            "Không lấy được cookie Google nào. Bạn đã đăng nhập Flow trên trình duyệt này chưa?"
        )

    return "; ".join(f"{k}={v}" for k, v in collected.items())


def grab_smart(browser: str = "brave") -> str:
    """
    Lấy cookie theo cách tốt nhất:
    1. Thử đọc & giải mã DB (nhanh, không mở trình duyệt) — cần trình duyệt đóng.
    2. Nếu app-bound (v20) hoặc DB bị khoá -> dùng CDP (mở trình duyệt + đọc qua debug).
    """
    try:
        return grab_cookies(browser)
    except CookieError as e:
        msg = str(e)
        if "app-bound" in msg or "v20" in msg or "bị khoá" in msg or "locked" in msg.lower():
            return grab_via_cdp(browser)
        raise


def grab_and_save(browser: str = "brave",
                  out_file: Optional[str] = None,
                  auto_close: bool = False) -> str:
    """
    Lấy cookie và ghi vào flow_cookie.txt. Trả về chuỗi cookie.

    auto_close=True: nếu file bị khoá (trình duyệt đang mở), tự đóng trình duyệt
    rồi thử lại. Bạn cần mở lại trình duyệt sau đó.
    """
    cookie = grab_smart(browser)
    out = pathlib.Path(out_file) if out_file else (
        pathlib.Path(__file__).parent / "flow_cookie.txt")
    out.write_text(cookie, encoding="utf-8")
    return cookie


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Lấy cookie Flow từ trình duyệt")
    p.add_argument("--browser", default="brave", choices=list(BROWSERS.keys()))
    p.add_argument("--save", action="store_true", help="Ghi vào flow_cookie.txt")
    p.add_argument("--auto-close", action="store_true",
                   help="Tự đóng trình duyệt nếu file cookie bị khoá")
    args = p.parse_args()
    try:
        if args.save:
            c = grab_and_save(args.browser, auto_close=args.auto_close)
            print(f"[OK] Đã lưu cookie ({len(c)} ký tự) vào flow_cookie.txt")
            print(f"     Có session-token: {'session-token' in c}")
        else:
            c = grab_cookies(args.browser)
            has_session = "session-token" in c
            print(f"[OK] Lấy được cookie ({len(c)} ký tự). "
                  f"Có session-token: {has_session}")
    except CookieError as e:
        print(f"[LỖI] {e}")
