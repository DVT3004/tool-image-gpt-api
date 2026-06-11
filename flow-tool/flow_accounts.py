# -*- coding: utf-8 -*-
"""
Flow multi-account engine (v2 - kiểu 9Router)
=============================================
- Mỗi acc chỉ cần "Đăng nhập" 1 lần (cửa sổ thường) -> rồi "Kiểm tra" để LƯU COOKIE.
- Cookie được lưu trong accounts.json. Sau đó KHÔNG cần mở trình duyệt của acc nữa.
- Tạo ảnh: access_token + project gọi HTTP bằng cookie đã lưu (không cần trình duyệt).
- Token reCAPTCHA: dùng POOL WORKER (vài trình duyệt dùng chung). Mỗi lần mint:
  xoá cookie worker -> bơm cookie acc -> vào trang project -> execute. => 1 vài tab
  chứa được hết acc, chạy song song.
- Xoay vòng: acc 403/hết quota -> cooldown -> chuyển acc khác.
"""

import os
import re
import json
import time
import asyncio
import threading
import subprocess
import urllib.request
import pathlib
from typing import Optional, List, Dict, Any

try:
    import cookie_import
    from flow_log import log as _flog
except Exception:  # fallback nếu chạy lẻ
    cookie_import = None
    def _flog(msg, tag="info"):
        print(f"[{tag}] {msg}")

HERE = pathlib.Path(__file__).parent
ACCOUNTS_FILE = HERE / "accounts.json"
COOKIES_DIR = HERE / "cookies"          # thả file cookie vào đây để tự nạp
COOKIES_DIR.mkdir(exist_ok=True)
(COOKIES_DIR / "imported").mkdir(exist_ok=True)
WORKER_DATA = HERE / "worker_data"
WORKER_DATA.mkdir(exist_ok=True)
LOGIN_DATA = HERE / "login_data"   # user-data-dir cho login dedicated
LOGIN_DATA.mkdir(exist_ok=True)
MINT_DATA = HERE / "mint_data"     # user-data-dir RIÊNG cho mint reCAPTCHA (tách khỏi login)
MINT_DATA.mkdir(exist_ok=True)

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
RECAPTCHA_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
RECAPTCHA_ACTION = "IMAGE_GENERATION"
FLOW_URL = "https://labs.google/fx/tools/flow"
WORKER_BASE_PORT = 9500
NUM_WORKERS = int(os.environ.get("FLOW_WORKERS", "3"))
COOLDOWN_SECONDS = 120
MAX_CONCURRENCY_PER_ACC = int(os.environ.get("FLOW_MAX_PER_ACC", "4"))  # tối đa luồng/đồng thời mỗi acc
ACCESS_TOKEN_TTL = 3600          # access_token Flow sống ~1h
KEEPALIVE_EVERY = 120            # giây: chu kỳ quét keepalive
KEEPALIVE_REFRESH_BEFORE = 600   # giây: refresh khi token còn dưới ngưỡng này
COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}

BROWSERS = {
    "chrome": {"exes": [rf"{os.environ.get('ProgramFiles','')}\Google\Chrome\Application\chrome.exe",
                        rf"{os.environ.get('ProgramFiles(x86)','')}\Google\Chrome\Application\chrome.exe"],
               "user_data": rf"{LOCALAPPDATA}\Google\Chrome\User Data"},
    "coccoc": {"exes": [rf"{os.environ.get('ProgramFiles(x86)','')}\CocCoc\Browser\Application\browser.exe",
                        rf"{os.environ.get('ProgramFiles','')}\CocCoc\Browser\Application\browser.exe",
                        rf"{LOCALAPPDATA}\CocCoc\Browser\Application\browser.exe"],
               "user_data": rf"{LOCALAPPDATA}\CocCoc\Browser\User Data"},
    "brave": {"exes": [rf"{os.environ.get('ProgramFiles','')}\BraveSoftware\Brave-Browser\Application\brave.exe",
                       rf"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe"],
              "user_data": rf"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\User Data"},
}
WORKER_BROWSER = "chrome"   # trình duyệt cho worker pool

EXEC_JS = """
async ([key, action]) => {
  try {
    return await new Promise((resolve) => {
      const to = setTimeout(() => resolve({error:'timeout'}), 30000);
      const run = () => window.grecaptcha.enterprise.execute(key, {action})
        .then(t => { clearTimeout(to); resolve({token:t}); })
        .catch(e => { clearTimeout(to); resolve({error:String(e)}); });
      if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.ready)
        window.grecaptcha.enterprise.ready(run);
      else run();
    });
  } catch(e){ return {error:String(e)}; }
}
"""


def find_browser_exe(browser: str) -> Optional[str]:
    for p in BROWSERS.get(browser, {}).get("exes", []):
        if p and os.path.exists(p):
            return p
    return None


def list_existing_profiles(browser: str) -> List[Dict[str, str]]:
    ud = BROWSERS.get(browser, {}).get("user_data")
    out = []
    if not ud or not os.path.exists(os.path.join(ud, "Local State")):
        return out
    try:
        ls = json.load(open(os.path.join(ud, "Local State"), encoding="utf-8"))
        for d, info in ls.get("profile", {}).get("info_cache", {}).items():
            out.append({"dir": d, "name": info.get("name", d), "email": info.get("user_name", "")})
    except Exception:
        pass
    return out


def _sanitize_cookies(cookies):
    out = []
    for c in cookies or []:
        d = {k: v for k, v in c.items() if k in COOKIE_KEYS}
        if not d.get("name") or "value" not in d:
            continue
        if d.get("sameSite") not in ("Strict", "Lax", "None"):
            d["sameSite"] = "Lax"
        out.append(d)
    return out


class Account:
    def __init__(self, d):
        self.id = d["id"]
        self.name = d.get("name", d["id"])
        self.browser = d.get("browser", "chrome")
        self.mode = d.get("mode", "dedicated")
        self.profile_directory = d.get("profile_directory", "Default")
        self.enabled = bool(d.get("enabled", True))
        self.email = d.get("email", "")
        self.cookies = d.get("cookies", [])   # đã lưu (CDP format)
        # runtime
        self.status = "ready" if self.cookies else "login_needed"
        self.failures = 0
        self.uses = 0
        self.cooldown_until = 0.0
        self.last_error = ""
        self.verified = None   # None=chưa kiểm tra HTTP, True/False=kết quả /auth/session
        self.inflight = 0      # số luồng đang chạy của acc này
        self.token_ts = 0.0    # thời điểm lấy access_token gần nhất
        self.token_ttl = 0     # access_token sống bao lâu (giây)
        self.session_expires = ""  # hạn của phiên đăng nhập (từ /auth/session)

    @property
    def has_session_cookie(self):
        return any(c.get("name") == "__Secure-next-auth.session-token"
                   and "labs.google" in c.get("domain", "") for c in self.cookies)

    @property
    def logged_in(self):
        # Có cookie phiên VÀ chưa bị xác thực HTTP thất bại.
        # Nếu đã kiểm tra và server báo hết phiên (verified=False) -> coi như chưa đăng nhập.
        if self.verified is False:
            return False
        return self.has_session_cookie

    @property
    def login_user_data_dir(self):
        if self.mode == "existing":
            return BROWSERS[self.browser]["user_data"]
        return str(LOGIN_DATA / self.id)

    @property
    def login_profile(self):
        return self.profile_directory if self.mode == "existing" else "Default"

    def cookie_header(self):
        labs = {c["name"]: c["value"] for c in self.cookies if "labs.google" in c.get("domain", "")}
        return "; ".join(f"{k}={v}" for k, v in labs.items())

    def to_dict(self):
        return {"id": self.id, "name": self.name, "browser": self.browser, "mode": self.mode,
                "profile_directory": self.profile_directory, "enabled": self.enabled,
                "email": self.email, "cookies": self.cookies}

    def token_left(self) -> int:
        """Số giây access_token còn sống (0 nếu chưa có/không rõ)."""
        if not self.token_ts or not self.token_ttl:
            return 0
        return max(0, int(self.token_ttl - (time.time() - self.token_ts)))

    def public_state(self):
        return {"id": self.id, "name": self.name, "browser": self.browser, "mode": self.mode,
                "profile_directory": self.profile_directory, "enabled": self.enabled,
                "email": self.email, "status": self.status, "failures": self.failures,
                "uses": self.uses, "logged_in": self.logged_in,
                "cooldown": max(0, int(self.cooldown_until - time.time())),
                "has_cookies": len(self.cookies) > 0, "last_error": self.last_error[:120],
                "inflight": self.inflight, "token_left": self.token_left(),
                "session_expires": self.session_expires}


class _Worker:
    """1 trình duyệt worker dùng chung để mint token (bơm cookie acc vào)."""
    def __init__(self, idx):
        self.idx = idx
        self.port = WORKER_BASE_PORT + idx
        self.udd = str(WORKER_DATA / f"w{idx}")
        self.proc = None
        self.cdp = None
        self.ctx = None
        self.page = None
        self.lock = None  # asyncio.Lock
        self.busy = False


class AccountManager:
    def __init__(self):
        self.accounts: List[Account] = []
        self._loop = None
        self._thread = None
        self._rr = 0
        self._sel_lock = threading.Lock()
        self._workers: List[_Worker] = []
        self._workers_started = False
        self._pw = None                 # playwright dùng chung cho mint token
        self._mint_sessions: Dict[str, dict] = {}   # acc_id -> trình duyệt mint (profile đã login)
        self._manual_sessions: Dict[str, dict] = {}  # acc_id -> phiên "log tay" đang bắt request
        self._launched_procs = []        # các tiến trình trình duyệt do tool mở (để đóng khi thoát)
        self.hide_browser = True         # chạy ngầm: mở trình duyệt NGOÀI màn hình (không hiện lên)
        self._keepalive_stop = False
        self.load()
        self._start_keepalive()

    def _acc_port(self, acc) -> int:
        """1 cổng debug RIÊNG & cố định cho mỗi acc -> mỗi tài khoản = 1 trình duyệt
        riêng theo đúng profile của nó (không dùng chung, không tranh cổng)."""
        # Ưu tiên theo vị trí trong danh sách -> đảm bảo KHÁC nhau giữa các acc.
        try:
            i = next(idx for idx, a in enumerate(self.accounts) if a.id == acc.id)
        except Exception:
            i = abs(hash(acc.id)) % 200
        return WORKER_BASE_PORT + 100 + (i % 200)

    # ----- KEEPALIVE: giữ token sống -----
    def _start_keepalive(self):
        t = threading.Thread(target=self._keepalive_loop, daemon=True)
        t.start()

    def _keepalive_loop(self):
        """Định kỳ làm mới token cho các acc đã đăng nhập (qua cookie, không cần login lại).
        Cookie phiên còn sống thì luôn xin được access_token mới -> token không bao giờ chết."""
        # chờ một nhịp để app khởi động xong
        time.sleep(10)
        while not self._keepalive_stop:
            try:
                for acc in list(self.accounts):
                    if not acc.enabled or not acc.has_session_cookie:
                        continue
                    # Đang tạo ảnh (inflight) -> KHÔNG động vào session để tránh
                    # NextAuth xoay cookie giữa chừng làm access_token bị 401.
                    if getattr(acc, "inflight", 0) > 0:
                        continue
                    # refresh khi chưa có token hoặc sắp hết hạn
                    if acc.token_ts == 0 or acc.token_left() < KEEPALIVE_REFRESH_BEFORE:
                        vr = self.verify_account(acc)
                        if vr.get("ok"):
                            if acc.status in ("login_needed", "Lỗi login"):
                                acc.status = "ready"
                            _flog(f"keepalive {acc.name}: token mới, còn ~{acc.token_left()//60} phút", "acc")
                        elif vr.get("ok") is False:
                            _flog(f"keepalive {acc.name}: phiên hết hạn, cần đăng nhập lại", "acc")
                        self.save()
            except Exception as e:
                _flog(f"keepalive lỗi: {e}", "acc")
            # ngủ theo chu kỳ
            for _ in range(KEEPALIVE_EVERY):
                if self._keepalive_stop:
                    break
                time.sleep(1)

    # ----- loop nền -----
    def _run_loop(self):
        self._loop = asyncio.ProactorEventLoop() if hasattr(asyncio, "ProactorEventLoop") else asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def ensure_loop(self):
        if self._loop and self._loop.is_running():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        while self._loop is None or not self._loop.is_running():
            threading.Event().wait(0.02)

    def _submit(self, coro, timeout=120):
        self.ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ----- lưu/đọc -----
    def load(self):
        self.accounts = []
        if ACCOUNTS_FILE.exists():
            try:
                for d in json.load(open(ACCOUNTS_FILE, encoding="utf-8")).get("accounts", []):
                    self.accounts.append(Account(d))
            except Exception as e:
                print("[accounts] load error:", e)
        # Tự nạp cookie từ file thả vào thư mục cookies/
        try:
            self.autoload_cookie_files()
        except Exception as e:
            _flog(f"autoload error: {e}", "cookie")

    def save(self):
        ACCOUNTS_FILE.write_text(json.dumps(
            {"accounts": [a.to_dict() for a in self.accounts]}, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, acc_id):
        return (next((a for a in self.accounts if a.id == acc_id), None)
                or next((a for a in self.accounts if a.name == acc_id), None))

    def add_account(self, name, browser="chrome", mode="dedicated", profile_directory="Default"):
        base = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "acc"
        acc_id, i = base, 1
        while self.get(acc_id):
            i += 1; acc_id = f"{base}_{i}"
        acc = Account({"id": acc_id, "name": name, "browser": browser, "mode": mode,
                       "profile_directory": profile_directory, "enabled": True})
        self.accounts.append(acc); self.save()
        return acc

    def delete_account(self, acc_id):
        self.accounts = [a for a in self.accounts if a.id != acc_id]
        self.save()

    def set_enabled(self, acc_id, enabled):
        acc = self.get(acc_id)
        if acc:
            acc.enabled = enabled; self.save()

    # ----- NẠP COOKIE THỦ CÔNG (không cần mở trình duyệt) -----
    def import_cookies(self, acc_id, raw):
        """Nạp cookie cho 1 acc từ chuỗi header / JSON / Netscape."""
        if cookie_import is None:
            return {"ok": False, "error": "Thiếu module cookie_import"}
        acc = self.get(acc_id)
        if not acc:
            return {"ok": False, "error": "Không tìm thấy acc"}
        cookies = cookie_import.parse_cookies(raw)
        if not cookies:
            return {"ok": False, "error": "Không parse được cookie nào"}
        acc.cookies = cookies
        has_sess = cookie_import.has_session(cookies)
        acc.verified = None   # cookie mới -> trạng thái xác thực chưa rõ
        acc.status = "ready" if has_sess else "login_needed"
        if has_sess:
            acc.cooldown_until = 0
            acc.failures = 0
            acc.last_error = ""
        else:
            acc.last_error = "Thiếu __Secure-next-auth.session-token (labs.google)"
        self.save()
        _flog(f"import cookie -> {acc.name}: {len(cookies)} cookie, session={has_sess}", "cookie")
        return {"ok": True, "logged_in": acc.logged_in, "count": len(cookies),
                "has_session": has_sess, "account": acc.public_state()}

    def add_or_update_with_cookies(self, name, raw, browser="manual", mode="manual"):
        """Tạo acc mới (nếu chưa có) rồi nạp cookie. Trùng tên -> cập nhật."""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Thiếu tên acc"}
        acc = next((a for a in self.accounts if a.name == name or a.id == name), None)
        if not acc:
            acc = self.add_account(name=name, browser=browser, mode=mode)
        return self.import_cookies(acc.id, raw)

    def autoload_cookie_files(self):
        """Quét thư mục cookies/ : mỗi file <tên>.txt|.json -> nạp cho acc cùng tên,
        nạp xong chuyển vào cookies/imported/. Trả về số file đã nạp."""
        if cookie_import is None:
            return 0
        loaded = 0
        for f in list(COOKIES_DIR.glob("*.txt")) + list(COOKIES_DIR.glob("*.json")):
            if f.stem.lower() in ("readme", "readme.txt") or f.stem.startswith("_"):
                continue
            try:
                raw = f.read_text(encoding="utf-8", errors="ignore").strip()
                if not raw:
                    continue
                res = self.add_or_update_with_cookies(f.stem, raw)
                if res.get("ok"):
                    loaded += 1
                    dest = COOKIES_DIR / "imported" / f"{f.stem}_{int(time.time())}{f.suffix}"
                    try:
                        f.replace(dest)
                    except Exception:
                        pass
                    _flog(f"autoload {f.name}: {res.get('count')} cookie, "
                          f"session={res.get('has_session')}", "cookie")
                else:
                    _flog(f"autoload {f.name} LỖI: {res.get('error')}", "cookie")
            except Exception as e:
                _flog(f"autoload {f.name} EXC: {e}", "cookie")
        return loaded

    # ----- launch helper -----
    def _close_browser_procs(self, browser):
        exe = {"chrome": "chrome.exe", "coccoc": "browser.exe", "brave": "brave.exe"}.get(browser)
        if exe:
            subprocess.run(["taskkill", "/F", "/IM", exe, "/T"], capture_output=True)
            time.sleep(1.2)

    def _port_alive(self, port):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    def _launch(self, exe, udd, profile, port, url=None, headless=False, close_browser=None,
                force_visible=False):
        if self._port_alive(port):
            return
        if close_browser:
            self._close_browser_procs(close_browser)
        # Dọn trình duyệt automation CŨ (orphan từ phiên trước) đang khóa profile này
        # -> nếu không, Chrome thứ 2 trên cùng user-data-dir bị forward, cổng debug ko mở.
        # An toàn: chỉ diệt tiến trình có ĐÚNG user-data-dir này, không đụng Chrome thường.
        self._kill_profile_browser(udd)
        args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
                f"--profile-directory={profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,IsolateOrigins,site-per-process"]
        # Chạy ngầm: đẩy cửa sổ ra ngoài màn hình (vẫn render thật -> reCAPTCHA ổn,
        # nhưng không hiện lên trước mặt người dùng). Không áp dụng khi headless thật
        # hoặc khi force_visible (chế độ "log tay" cần bạn nhìn thấy & thao tác).
        if getattr(self, "hide_browser", False) and not headless and not force_visible:
            args += ["--window-position=-32000,-32000", "--window-size=1100,800",
                     "--start-minimized"]
        if headless:
            args.append("--headless=new")
        if url:
            args.append(url)
        proc = subprocess.Popen(args)
        try:
            self._launched_procs.append(proc)
        except Exception:
            pass
        for _ in range(60):
            if self._port_alive(port):
                return
            time.sleep(0.5)
        raise RuntimeError(f"Không mở được cổng debug {port}")

    def _kill_profile_browser(self, udd):
        """Diệt CHỈ trình duyệt đang mở user-data-dir 'udd' (profile automation của tool).
        Lọc theo command line nên KHÔNG ảnh hưởng Chrome thường của người dùng."""
        try:
            needle = str(udd).replace("'", "''")
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or "
                  "Name='brave.exe' or Name='msedge.exe' or Name='browser.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*" + needle + "*' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=25)
            time.sleep(1.0)
        except Exception:
            pass

    # ----- login + capture cookie -----
    def login_account(self, acc_id):
        acc = self.get(acc_id)
        if not acc:
            return {"ok": False, "error": "not found"}
        exe = find_browser_exe(acc.browser)
        if not exe:
            return {"ok": False, "error": f"Không tìm thấy {acc.browser}"}
        try:
            subprocess.Popen([exe, f"--user-data-dir={acc.login_user_data_dir}",
                              f"--profile-directory={acc.login_profile}",
                              "--no-first-run", "--no-default-browser-check", FLOW_URL])
            acc.status = "login_needed"
            return {"ok": True, "message": "Đăng nhập Google + mở Flow trong cửa sổ. "
                    "Xong ĐÓNG cửa sổ rồi bấm 'Kiểm tra' để lưu cookie."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _capture_async(self, acc: Account):
        """Mở browser acc (cổng debug) đọc cookie google+labs, lưu lại, đóng."""
        from playwright.async_api import async_playwright
        port = self._acc_port(acc)  # cổng tạm riêng
        exe = find_browser_exe(acc.browser)
        close = acc.browser if acc.mode == "existing" else None
        # launch (sync) trong executor để không chặn loop
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._launch(exe, acc.login_user_data_dir, acc.login_profile, port,
                                       FLOW_URL, headless=False, close_browser=close))
        pw = await async_playwright().start()
        try:
            cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = cdp.contexts[0] if cdp.contexts else await cdp.new_context()
            # đảm bảo có page labs.google để cookie nạp
            page = None
            for p in ctx.pages:
                if "labs.google" in (p.url or ""):
                    page = p; break
            if not page:
                page = await ctx.new_page()
                await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            cks = []
            for _ in range(6):
                cks = await ctx.cookies()
                if any(c.get("name") == "__Secure-next-auth.session-token" for c in cks):
                    break
                await asyncio.sleep(1.0)
            wanted = [c for c in cks if "google" in c.get("domain", "")]
            acc.cookies = _sanitize_cookies(wanted)
            await cdp.close()
        finally:
            await pw.stop()
        return acc.logged_in

    def check_login(self, acc_id):
        acc = self.get(acc_id)
        if not acc:
            return False
        # 1) Đọc cookie từ trình duyệt profile của tool (cổng debug)
        try:
            self._submit(self._capture_async(acc), timeout=120)
        except Exception as e:
            acc.last_error = str(e)
        _flog(f"check {acc.name}: capture(profile tool) -> "
              f"{len(acc.cookies)} cookie, session={acc.has_session_cookie}", "acc")

        # 2) Nếu chưa thấy phiên -> thử đọc cookie từ TRÌNH DUYỆT THẬT của bạn
        if not acc.has_session_cookie:
            try:
                import cookie_grabber as cg
                cks = cg.grab_cookies_struct(acc.browser, keep_open=True)
                g = _sanitize_cookies([c for c in cks if "google" in c.get("domain", "")])
                if any(c.get("name") == "__Secure-next-auth.session-token" for c in g):
                    acc.cookies = g
                    _flog(f"check {acc.name}: lấy cookie từ Chrome thật -> {len(g)} cookie", "acc")
            except Exception as e:
                _flog(f"check {acc.name}: grab Chrome thật lỗi: {e}", "acc")

        # 3) Xác thực THẬT bằng HTTP
        vr = self.verify_account(acc)
        if vr.get("ok"):
            acc.status = "ready"
            acc.cooldown_until = 0
            acc.failures = 0
            acc.last_error = ""
            if vr.get("email"):
                acc.email = vr["email"]
            _flog(f"check {acc.name}: ĐĂNG NHẬP OK ({vr.get('email')})", "acc")
        else:
            acc.status = "login_needed"
            acc.last_error = vr.get("detail") or "Phiên đã hết / acc đã đăng xuất."
            _flog(f"check {acc.name}: CHƯA đăng nhập - {acc.last_error}", "acc")
        self.save()
        return bool(vr.get("ok"))

    def verify_account(self, acc) -> dict:
        """Gọi thật /fx/api/auth/session bằng cookie đã lưu để biết acc CÒN đăng nhập không.
        Cập nhật acc.verified. Trả {ok, email, detail}."""
        result = {"ok": False, "email": "", "detail": ""}
        if not acc.has_session_cookie:
            acc.verified = False
            result["detail"] = "Chưa có cookie phiên (session-token)."
            return result
        try:
            import requests
        except Exception:
            acc.verified = None  # không kiểm được -> để nguyên
            result["ok"] = None
            result["detail"] = "Thiếu thư viện requests để kiểm tra."
            return result
        s = requests.Session()
        s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/131.0.0.0 Safari/537.36")
        for c in (acc.cookies or []):
            name = c.get("name"); val = c.get("value")
            if not name or val is None:
                continue
            dom = (c.get("domain") or "").lstrip(".") or "labs.google"
            try:
                s.cookies.set(name, val, domain=dom, path=c.get("path", "/"))
            except Exception:
                pass
        try:
            r = s.get("https://labs.google/fx/api/auth/session", timeout=20)
            if r.status_code == 200:
                j = r.json() or {}
                tok = j.get("access_token")
                email = (j.get("user") or {}).get("email", "")
                if tok:
                    acc.verified = True
                    acc.token_ts = time.time()
                    acc.token_ttl = ACCESS_TOKEN_TTL
                    acc.session_expires = j.get("expires", "") or acc.session_expires
                    # NextAuth có thể XOAY session-token qua Set-Cookie. Lưu giá trị MỚI
                    # ngược lại acc.cookies để lượt tạo ảnh sau không dùng cookie cũ -> 401.
                    try:
                        jar = {c.name: c for c in s.cookies}
                        changed = False
                        for c in acc.cookies:
                            nc = jar.get(c.get("name"))
                            if nc is not None and nc.value and nc.value != c.get("value"):
                                c["value"] = nc.value
                                changed = True
                        if changed:
                            self.save()
                    except Exception:
                        pass
                    result.update(ok=True, email=email)
                else:
                    acc.verified = False
                    result["detail"] = "Server không trả access_token (đã đăng xuất / hết phiên)."
            else:
                acc.verified = False
                result["detail"] = f"session HTTP {r.status_code}"
        except Exception as e:
            acc.verified = None  # lỗi mạng -> không kết luận
            result["ok"] = None
            result["detail"] = f"Lỗi mạng: {e}"
        return result

    # tương thích cũ
    def start_account(self, acc_id, headless=False):
        return self.check_login(acc_id)

    async def _has_session(self, ctx) -> bool:
        try:
            cks = await ctx.cookies()
        except Exception:
            return False
        return any(c.get("name") == "__Secure-next-auth.session-token" for c in cks)

    @staticmethod
    async def _try_click(page, selector, timeout=800) -> bool:
        """Bấm phần tử nếu có & đang hiển thị. Trả True nếu đã bấm."""
        try:
            el = await page.wait_for_selector(selector, timeout=timeout, state="visible")
            if el:
                await el.click()
                return True
        except Exception:
            pass
        return False

    @staticmethod
    async def _try_fill(page, selector, value, timeout=800) -> bool:
        """Điền giá trị vào ô input nếu có & đang hiển thị. Trả True nếu đã điền."""
        try:
            el = await page.wait_for_selector(selector, timeout=timeout, state="visible")
            if el:
                await el.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await el.fill(value)
                return True
        except Exception:
            pass
        return False

    async def _auto_login_async(self, acc: Account, email, password,
                                recovery_email=None, max_wait=300):
        """
        Semi-auto login Gmail -> Flow.

        - Tự điền email / mật khẩu / email phụ và bấm các nút Tiếp theo / Tiếp tục.
        - Nếu gặp 2FA / captcha / Google chặn: GIỮ cửa sổ mở để bạn tự hoàn tất.
        - Liên tục dò cookie phiên (`__Secure-next-auth.session-token`) trong tối đa
          `max_wait` giây; chụp cookie ngay khi phiên xuất hiện.
        """
        from playwright.async_api import async_playwright
        port = self._acc_port(acc)
        exe = find_browser_exe(acc.browser)
        if not exe:
            raise RuntimeError(f"Không tìm thấy trình duyệt {acc.browser}")

        acc.status = "Đang mở trình duyệt..."
        self.save()

        close = acc.browser if acc.mode == "existing" else None
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._launch(exe, acc.login_user_data_dir, acc.login_profile, port,
                                       FLOW_URL, headless=False, close_browser=close)
        )

        pw = await async_playwright().start()
        cdp = None
        deadline = time.time() + max_wait
        try:
            cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = cdp.contexts[0] if cdp.contexts else await cdp.new_context()

            await asyncio.sleep(2.0)

            while time.time() < deadline:
                # Đã đăng nhập xong?
                if await self._has_session(ctx):
                    acc.status = "Đăng nhập OK"
                    self.save()
                    break

                pages = ctx.pages
                if not pages:
                    page = await ctx.new_page()
                    try:
                        await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                    pages = [page]

                acted = False
                for page in pages:
                    url = page.url or ""

                    # 0. Trang Flow: bấm nút bắt đầu / đăng nhập để mở luồng Google OAuth
                    if "labs.google" in url:
                        for sel in (
                            'button:has-text("Create with Google Flow")',
                            'a:has-text("Create with Google Flow")',
                            'button:has-text("Sign in")',
                            'a:has-text("Sign in")',
                            'button:has-text("Đăng nhập")',
                            'a:has-text("Đăng nhập")',
                        ):
                            if await self._try_click(page, sel, timeout=600):
                                acc.status = "Mở đăng nhập Google..."
                                self.save()
                                await asyncio.sleep(2.5)
                                acted = True
                                break
                        if acted:
                            break

                    # 1. Màn hình chọn tài khoản đã đăng nhập trước đó
                    if "accounts.google" in url:
                        if await self._try_click(page, f'div[data-identifier="{email}"]', timeout=600):
                            acc.status = "Chọn tài khoản..."
                            self.save()
                            await asyncio.sleep(2.0)
                            acted = True
                            break

                    # 2. Ô email
                    if "identifier" in url or "signin/v2/identifier" in url:
                        if await self._try_fill(page, 'input[type="email"]', email, timeout=1000):
                            acc.status = "Đang điền Email..."
                            self.save()
                            await asyncio.sleep(0.4)
                            await self._try_click(
                                page,
                                '#identifierNext button, button:has-text("Tiếp theo"), button:has-text("Next")',
                                timeout=1500)
                            await asyncio.sleep(2.0)
                            acted = True
                            break

                    # 3. Ô mật khẩu
                    if await self._try_fill(page, 'input[type="password"]:not([aria-hidden="true"])',
                                            password, timeout=1000):
                        acc.status = "Đang điền Mật khẩu..."
                        self.save()
                        await asyncio.sleep(0.4)
                        await self._try_click(
                            page,
                            '#passwordNext button, button:has-text("Tiếp theo"), button:has-text("Next")',
                            timeout=1500)
                        await asyncio.sleep(2.0)
                        acted = True
                        break

                    # 4. Xác thực bằng email khôi phục
                    if recovery_email and ("challenge" in url or "signin" in url):
                        if await self._try_click(
                            page,
                            'div[data-challengetype="12"], li:has-text("email khôi phục"), '
                            'div:has-text("Xác nhận email khôi phục"), '
                            'div:has-text("Confirm your recovery email")',
                                timeout=800):
                            acc.status = "Chọn xác thực phụ..."
                            self.save()
                            await asyncio.sleep(2.0)
                            acted = True
                            break
                        if await self._try_fill(
                                page, 'input[name="knowledgePreregisteredEmailResponse"]',
                                recovery_email, timeout=800):
                            acc.status = "Điền Email phụ..."
                            self.save()
                            await asyncio.sleep(0.4)
                            await self._try_click(
                                page,
                                'button:has-text("Tiếp theo"), button:has-text("Next")',
                                timeout=1500)
                            await asyncio.sleep(2.0)
                            acted = True
                            break

                    # 5. Màn hình đồng ý / tiếp tục (consent, "Stay signed in", cấp quyền)
                    if await self._try_click(
                        page,
                        'button:has-text("Tiếp tục"), button:has-text("Continue"), '
                        'button:has-text("Cho phép"), button:has-text("Allow"), '
                        'button:has-text("Xác nhận"), button:has-text("Confirm")',
                            timeout=600):
                        acc.status = "Xác nhận quyền truy cập..."
                        self.save()
                        await asyncio.sleep(2.0)
                        acted = True
                        break

                if not acted:
                    # Có thể đang ở 2FA / captcha / Google chặn -> để người dùng tự xử lý
                    if any("challenge" in (p.url or "") for p in pages):
                        acc.status = "Chờ bạn xác nhận 2FA/captcha..."
                        self.save()
                        await asyncio.sleep(2.0)
                    elif any("rejected" in (p.url or "") or "deniedsi" in (p.url or "") for p in pages):
                        acc.status = "Google chặn - hãy đăng nhập tay trong cửa sổ"
                        self.save()
                        await asyncio.sleep(2.0)
                    elif (not any("labs.google" in (p.url or "") for p in pages)
                          and not any("accounts.google" in (p.url or "") for p in pages)):
                        # Đã qua đăng nhập Google nhưng không còn ở Flow -> mở lại Flow
                        # để NextAuth tạo cookie __Secure-next-auth.session-token.
                        acc.status = "Quay lại Flow lấy phiên..."
                        self.save()
                        try:
                            page = pages[0] if pages else await ctx.new_page()
                            await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=45000)
                        except Exception:
                            pass
                        await asyncio.sleep(2.5)
                    else:
                        acc.status = "Đang đăng nhập..."
                        self.save()
                        await asyncio.sleep(2.0)

            # Chụp cookie nếu đã có phiên
            cks = await ctx.cookies()
            wanted = [c for c in cks if "google" in c.get("domain", "")]
            has_sess = any(c.get("name") == "__Secure-next-auth.session-token" for c in cks)
            if wanted and has_sess:
                acc.cookies = _sanitize_cookies(wanted)
                acc.status = "ready"
                acc.failures = 0
                acc.cooldown_until = 0
                acc.last_error = ""
                self.save()
                return True
            else:
                acc.status = "login_needed"
                acc.last_error = "Hết thời gian chờ mà chưa lấy được phiên (session-token)."
                self.save()
                return False
        finally:
            # KHÔNG đóng trình duyệt: giữ phiên cho lần kiểm tra/đăng nhập tay nếu cần
            if cdp is not None:
                try:
                    await cdp.close()
                except Exception:
                    pass
            await pw.stop()

    def auto_login(self, acc_id, email, password, recovery_email=None, max_wait=300):
        acc = self.get(acc_id)
        if not acc:
            return False
        acc.email = email
        self.save()
        try:
            # Cho coroutine thêm 60s đệm so với deadline bên trong để return sạch sẽ.
            res = self._submit(
                self._auto_login_async(acc, email, password, recovery_email, max_wait=max_wait),
                timeout=max_wait + 60)
            if not res:
                return False
            # Đã chụp được cookie -> XÁC THỰC THẬT bằng HTTP trước khi báo thành công.
            vr = self.verify_account(acc)
            if vr.get("ok"):
                acc.status = "ready"
                acc.cooldown_until = 0
                acc.failures = 0
                acc.last_error = ""
                if vr.get("email"):
                    acc.email = vr["email"]
                self.save()
                return True
            acc.status = "login_needed"
            acc.last_error = vr.get("detail") or "Lấy được cookie nhưng phiên không hợp lệ."
            self.save()
            return False
        except Exception as e:
            acc.last_error = str(e)
            acc.status = "Lỗi login"
            self.save()
            return False

    # ----- dò TÊN MODEL THẬT từ request của Flow -----
    async def _sniff_models_async(self, acc: Account, max_wait=240):
        """
        Mở Flow trong profile đã login của acc và lắng nghe request tạo ảnh để lấy
        mã model thật (`imageModelName`). Người dùng vào Flow tạo 1 ảnh với model
        muốn dùng -> tool đọc đúng mã model gửi lên.
        """
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        pw = self._pw

        exe = find_browser_exe(acc.browser)
        if not exe:
            raise RuntimeError(f"Không tìm thấy trình duyệt {acc.browser}")
        port = self._acc_port(acc)
        close = acc.browser if acc.mode == "existing" else None
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._launch(exe, acc.login_user_data_dir, acc.login_profile,
                                       port, FLOW_URL, headless=False, close_browser=close))
        cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = cdp.contexts[0] if cdp.contexts else await cdp.new_context()

        found = set()
        model_info = {}   # modelKey -> {"name":..., "type":...}
        DEBUG_FILE = HERE / "sniff_requests.log"

        def _dbg(msg):
            _flog(msg, tag="sniff")
            try:
                with open(DEBUG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            except Exception:
                pass

        def _scan(obj):
            """Tìm các 'entry model' (dict có modelKey/imageModelName...) và lấy kèm
            tên hiển thị + loại để phân biệt model ẢNH với model VIDEO."""
            if isinstance(obj, dict):
                _entry(obj)
                for v in obj.values():
                    _scan(v)
            elif isinstance(obj, list):
                for it in obj:
                    _scan(it)

        MODEL_KEYS = ("modelkey", "imagemodelname", "modelname", "modelid")
        NAME_KEYS = ("displayname", "name", "title", "label")
        TYPE_KEYS = ("mediatype", "type", "category", "modality", "tool", "mediagenerationtype")

        def _entry(d):
            mk = None
            for k, v in d.items():
                if str(k).lower() in MODEL_KEYS and isinstance(v, str) and v:
                    mk = v
                    break
            if not mk:
                return
            name = ""
            mtype = ""
            for k, v in d.items():
                lk = str(k).lower()
                if not name and lk in NAME_KEYS and isinstance(v, str):
                    name = v
                if not mtype and lk in TYPE_KEYS and isinstance(v, str):
                    mtype = v
            if mk not in found:
                found.add(mk)
                model_info[mk] = {"name": name, "type": mtype}
                _dbg(f"MODEL -> {mk} | name='{name}' | type='{mtype}'")

        # Các URL đáng quan tâm (tạo ảnh + danh sách model/capability)
        KEYS = ("GenerateImage", "batchGenerateImages", "model", "Model",
                "capabilit", "Capabilit")

        def on_request(req):
            try:
                url = req.url
                # Lưu NGUYÊN VĂN request tạo ảnh / upload ảnh -> để lấy định dạng img2img
                low = url.lower()
                if ("batchgenerateimages" in low or "upload" in low or "media:batch" in low
                        or ":upload" in low or "/media" in low):
                    pd = req.post_data
                    try:
                        with open(HERE / "img2img_capture.log", "a", encoding="utf-8") as f:
                            f.write(f"\n==== {req.method} {url} ====\n{(pd or '')[:20000]}\n")
                        _dbg(f"  >> Đã lưu request vào img2img_capture.log ({req.method} {url[:80]})")
                    except Exception:
                        pass
                if any(s in url for s in KEYS):
                    _dbg(f"REQ {req.method} {url[:170]}")
                    pd = req.post_data
                    if pd:
                        try:
                            _scan(json.loads(pd))
                        except Exception:
                            _dbg(f"  post_data(raw,{len(pd)}b): {pd[:600]}")
            except Exception:
                pass

        async def on_response(resp):
            try:
                url = resp.url
                if any(s in url for s in KEYS):
                    _dbg(f"RESP {resp.status} {url[:170]}")
                    ctype = ""
                    try:
                        ctype = (resp.headers or {}).get("content-type", "")
                    except Exception:
                        pass
                    if "json" in ctype or "text" in ctype or ctype == "":
                        body = await resp.text()
                        # Lưu NGUYÊN VĂN body của endpoint liệt kê model để soi mã thật
                        if "models" in url or "statuses" in url or "capabilit" in url.lower():
                            try:
                                fn = HERE / "models_statuses.json"
                                with open(fn, "a", encoding="utf-8") as f:
                                    f.write(f"\n==== {url} ====\n{body}\n")
                                _dbg(f"  >> Đã lưu nguyên văn body vào {fn.name} ({len(body)}b)")
                            except Exception:
                                pass
                        try:
                            _scan(json.loads(body))
                        except Exception:
                            _dbg(f"  body({len(body)}b): {body[:1000]}")
            except Exception:
                pass

        def _wire(p):
            p.on("request", on_request)
            p.on("response", lambda r: asyncio.create_task(on_response(r)))

        ctx.on("page", lambda p: _wire(p))
        for p in ctx.pages:
            _wire(p)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(FLOW_URL, wait_until="networkidle", timeout=45000)
        except Exception:
            try:
                await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        _dbg("=== TỰ ĐỘNG dò model (không cần bạn tạo ảnh). Đang đọc cấu hình Flow... ===")

        async def _open_image_models():
            """Bấm sang tab Hình ảnh + mở dropdown model để Flow nạp danh sách MODEL ẢNH
            (model ảnh thường chỉ nạp khi rời ngữ cảnh video)."""
            for sel in ("text=Hình ảnh", "text=Hình Ảnh", "text=Image", "text=Images",
                        "button:has-text('Hình ảnh')"):
                try:
                    el = page.locator(sel).first
                    if await el.count():
                        await el.click(timeout=2500)
                        _dbg(f"Đã bấm tab ảnh: {sel}")
                        await asyncio.sleep(1.2)
                        break
                except Exception as e:
                    _dbg(f"click tab {sel} lỗi: {e}")
            for sel in ("text=Nano Banana", "text=Imagen", "[role=combobox]",
                        "button:has-text('Banana')"):
                try:
                    el = page.locator(sel).first
                    if await el.count():
                        await el.click(timeout=2500)
                        _dbg(f"Đã mở dropdown model: {sel}")
                        await asyncio.sleep(1.2)
                        break
                except Exception as e:
                    _dbg(f"open dropdown {sel} lỗi: {e}")

        async def _grab_from_page():
            """Đọc model nhúng trong trang: Next.js __NEXT_DATA__ + biến cấu hình toàn cục."""
            try:
                data = await page.evaluate(
                    "() => {\n"
                    "  const out = {};\n"
                    "  try { if (window.__NEXT_DATA__) out.next = window.__NEXT_DATA__; } catch(e){}\n"
                    "  try {\n"
                    "    const scripts = [...document.querySelectorAll('script')];\n"
                    "    out.json = scripts.map(s => s.textContent || '')\n"
                    "      .filter(t => t.includes('odelName') || t.toLowerCase().includes('imagemodel'))\n"
                    "      .slice(0, 5);\n"
                    "  } catch(e){}\n"
                    "  return out;\n"
                    "}"
                )
                if not data:
                    return
                before = len(found)
                # quét object next
                if isinstance(data, dict):
                    _scan(data.get("next"))
                    for blob in (data.get("json") or []):
                        try:
                            _scan(json.loads(blob))
                        except Exception:
                            # tìm thô các mã model trong text script
                            import re
                            for m in re.findall(r'"[a-z]*[Mm]odel[Nn]ame"\s*:\s*"([^"]+)"', blob):
                                if m and m not in found:
                                    found.add(m)
                                    _dbg(f"MODEL (script) -> {m}")
                if len(found) > before:
                    _dbg(f"Lấy model từ trang: {sorted(found)}")
            except Exception as e:
                _dbg(f"grab_from_page lỗi: {e}")

        # Ép Flow nạp danh sách model ẢNH trước khi quét
        await _open_image_models()

        deadline = time.time() + max_wait
        last_n = 0
        stable_since = None
        while time.time() < deadline:
            await _grab_from_page()
            await asyncio.sleep(2.0)
            if len(found) != last_n:
                last_n = len(found)
                stable_since = time.time()
                _dbg(f"-> Đã có {last_n} model: {sorted(found)}")
            elif found and stable_since and (time.time() - stable_since) > 12:
                _dbg("Danh sách model đã ổn định -> dừng sớm.")
                break
        _dbg(f"=== Kết thúc. Tất cả model dò được: {sorted(found)} ===")

        # Lọc chỉ giữ MODEL ẢNH, bỏ model video (veo...) / âm thanh.
        def _is_image(mk):
            info = model_info.get(mk, {})
            name = (info.get("name") or "").lower()
            typ = (info.get("type") or "").lower()
            mkl = mk.lower()
            if mkl.startswith("veo") or "video" in typ or "video" in name or "audio" in typ:
                return False
            if "image" in typ or any(s in name for s in ("nano banana", "banana", "imagen")):
                return True
            # Không rõ loại -> giữ lại (trừ video) để khỏi sót model ảnh
            return True

        image_models = sorted((m for m in found if _is_image(m)), key=str.lower)
        result = [{"code": m, "label": (model_info.get(m, {}).get("name") or m)}
                  for m in image_models]
        _dbg(f"=== Model ẢNH (đã lọc bỏ video): {result} ===")

        try:
            await cdp.close()
        except Exception:
            pass
        return result

    def sniff_models(self, acc_id, max_wait=240):
        acc = self.get(acc_id)
        if not acc:
            return []
        try:
            return self._submit(self._sniff_models_async(acc, max_wait=max_wait),
                                 timeout=max_wait + 30)
        except Exception as e:
            acc.last_error = str(e)
            return []

    # ----- "LOG TAY": mở Chromium cho bạn thao tác + BẮT TOÀN BỘ request -----
    MANUAL_CAPTURE_FILE = HERE / "manual_capture.log"

    async def _manual_capture_async(self, acc: Account, log=None):
        """
        Mở Chromium (HIỆN cửa sổ) ở profile đã đăng nhập của acc để BẠN TỰ thao tác
        (đăng nhập / tạo ảnh / sửa ảnh...). Tool lắng nghe & ghi lại NGUYÊN VĂN mọi
        request + response liên quan (URL, method, headers, body) vào manual_capture.log
        để dùng làm thêm chức năng sau này.

        Hàm trả về NGAY sau khi gắn listener; trình duyệt giữ mở tới khi bạn đóng hoặc
        bấm DỪNG. Listener vẫn chạy nhờ event-loop nền.
        """
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        pw = self._pw

        exe = find_browser_exe(acc.browser)
        if not exe:
            raise RuntimeError(f"Không tìm thấy trình duyệt {acc.browser}")
        port = self._acc_port(acc)
        close = acc.browser if acc.mode == "existing" else None
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._launch(exe, acc.login_user_data_dir, acc.login_profile,
                                       port, FLOW_URL, headless=False, close_browser=close,
                                       force_visible=True))
        cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = cdp.contexts[0] if cdp.contexts else await cdp.new_context()

        # Bơm cookie đã lưu (nếu có) -> profile chắc chắn ở trạng thái đăng nhập.
        if acc.cookies:
            try:
                await ctx.add_cookies(acc.cookies)
            except Exception:
                pass

        self._manual_sessions[acc.id] = {"cdp": cdp, "ctx": ctx}
        CAP = self.MANUAL_CAPTURE_FILE

        def _write(line):
            """Ghi NGUYÊN VĂN vào file capture."""
            try:
                with open(CAP, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

        def _brief(msg):
            """Log 1 dòng tóm tắt lên GUI (không dội nguyên văn cho đỡ rối)."""
            if log:
                try:
                    log(msg)
                except Exception:
                    pass

        # Chỉ ghi các request "đáng giá" (XHR/fetch tới Google/labs) cho gọn file.
        HOST_HINTS = ("labs.google", "aisandbox", "googleapis.com", "clients6.google",
                      "accounts.google", "play.google")

        def _interesting(url, rtype=""):
            low = (url or "").lower()
            if rtype in ("xhr", "fetch"):
                return True
            return any(h in low for h in HOST_HINTS)

        def on_request(req):
            try:
                url = req.url
                if not _interesting(url, req.resource_type):
                    return
                ts = time.strftime("%H:%M:%S")
                lines = [f"\n==== REQ {ts} {req.method} {url} ===="]
                try:
                    hdrs = req.headers or {}
                    # Giữ lại header hữu ích (đặc biệt authorization) để dựng API.
                    for k, v in hdrs.items():
                        lines.append(f"H {k}: {v}")
                except Exception:
                    pass
                pd = None
                try:
                    pd = req.post_data
                except Exception:
                    pd = None
                if pd:
                    lines.append("BODY: " + pd[:50000])
                _write("\n".join(lines))
                _brief(f"REQ {req.method} {url[:90]}")
            except Exception:
                pass

        async def on_response(resp):
            try:
                url = resp.url
                req = resp.request
                if not _interesting(url, req.resource_type if req else ""):
                    return
                ctype = ""
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                except Exception:
                    pass
                head = f"\n---- RESP {time.strftime('%H:%M:%S')} {resp.status} {url} ({ctype}) ----"
                body = ""
                if any(t in ctype for t in ("json", "text", "javascript")) or ctype == "":
                    try:
                        body = await resp.text()
                    except Exception:
                        body = ""
                _write(head + ("\n" + body[:50000] if body else ""))
            except Exception:
                pass

        def _wire(p):
            try:
                p.on("request", on_request)
                p.on("response", lambda r: asyncio.create_task(on_response(r)))
            except Exception:
                pass

        ctx.on("page", lambda p: _wire(p))
        for p in ctx.pages:
            _wire(p)

        # Mở sẵn 1 tab Flow nếu chưa có để bạn thao tác.
        if not ctx.pages:
            try:
                page = await ctx.new_page()
                await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        # QUAN TRỌNG: kéo cửa sổ về giữa màn hình & focus (vì app mặc định chạy ngầm
        # đẩy cửa sổ ra -32000). Nếu trình duyệt đang bị ẩn/minimize -> đưa ra trước mặt
        # để bạn thao tác tay được.
        try:
            page0 = ctx.pages[0] if ctx.pages else await ctx.new_page()
            cdpsess = await ctx.new_cdp_session(page0)
            info = await cdpsess.send("Browser.getWindowForTarget")
            win_id = info.get("windowId")
            if win_id is not None:
                # về normal trước (thoát minimized), rồi đặt vị trí/kích thước hiển thị
                try:
                    await cdpsess.send("Browser.setWindowBounds",
                                       {"windowId": win_id,
                                        "bounds": {"windowState": "normal"}})
                except Exception:
                    pass
                await cdpsess.send("Browser.setWindowBounds",
                                   {"windowId": win_id,
                                    "bounds": {"left": 80, "top": 60,
                                               "width": 1280, "height": 860,
                                               "windowState": "normal"}})
            try:
                await page0.bring_to_front()
            except Exception:
                pass
        except Exception as e:
            _flog(f"manual {acc.name}: không kéo được cửa sổ ra: {e}", "acc")

        _write(f"\n######## BẮT ĐẦU LOG TAY [{acc.name}] {time.strftime('%Y-%m-%d %H:%M:%S')} "
               f"########")
        return str(CAP)

    def start_manual_capture(self, acc_id, log=None, timeout=120):
        """Bật chế độ 'log tay' cho 1 acc. Trả {ok, file} hoặc {ok:False, error}."""
        acc = self.get(acc_id)
        if not acc:
            return {"ok": False, "error": "Không tìm thấy tài khoản"}
        if acc.id in self._manual_sessions:
            return {"ok": True, "file": str(self.MANUAL_CAPTURE_FILE),
                    "message": "Phiên log tay đang chạy."}
        try:
            path = self._submit(self._manual_capture_async(acc, log=log), timeout=timeout)
            return {"ok": True, "file": path}
        except Exception as e:
            acc.last_error = str(e)
            return {"ok": False, "error": str(e)}

    async def _stop_manual_async(self, acc_id):
        sess = self._manual_sessions.pop(acc_id, None)
        if sess and sess.get("cdp"):
            try:
                await sess["cdp"].close()
            except Exception:
                pass

    def stop_manual_capture(self, acc_id=None):
        """Dừng phiên log tay (1 acc hoặc tất cả). Trình duyệt sẽ được ngắt CDP."""
        ids = [acc_id] if acc_id else list(self._manual_sessions.keys())
        for aid in ids:
            try:
                self._submit(self._stop_manual_async(aid), timeout=15)
            except Exception:
                pass
        return {"ok": True, "stopped": ids}

    def manual_capture_running(self, acc_id=None):
        if acc_id:
            return acc_id in self._manual_sessions
        return len(self._manual_sessions) > 0

    async def _ensure_workers(self):
        from playwright.async_api import async_playwright
        if self._workers_started:
            return
        if not hasattr(self, "_worker_init_lock") or self._worker_init_lock is None:
            self._worker_init_lock = asyncio.Lock()
        async with self._worker_init_lock:
            if self._workers_started:
                return
            exe = find_browser_exe(WORKER_BROWSER)
            if not exe:
                raise RuntimeError(f"Worker cần {WORKER_BROWSER} nhưng chưa cài")
            pw = await async_playwright().start()
            self._pw_workers = pw
            for i in range(NUM_WORKERS):
                w = _Worker(i)
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda w=w: self._launch(exe, w.udd, "Default", w.port, FLOW_URL, headless=False))
                w.cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{w.port}")
                w.ctx = w.cdp.contexts[0] if w.cdp.contexts else await w.cdp.new_context()
                w.lock = asyncio.Lock()
                self._workers.append(w)
            self._workers_started = True


    async def _mint_async(self, acc: Account, project_id: str):
        """
        Mint token reCAPTCHA NGAY TRONG profile đã đăng nhập của acc.
        - Dùng chung 1 trình duyệt / 1 cổng debug với lúc login (self._acc_port).
        - Nếu profile chưa có phiên (vd acc nạp cookie tay) thì bơm cookie đã lưu.
        => 1 acc = 1 trình duyệt, đúng profile vừa login, không mở pool riêng.
        """
        from playwright.async_api import async_playwright
        _flog(f"mint {acc.name}: bắt đầu (project={project_id})", "acc")
        if self._pw is None:
            self._pw = await async_playwright().start()
        pw = self._pw

        sess = self._mint_sessions.get(acc.id)
        if sess is None:
            sess = {"lock": asyncio.Lock(), "cdp": None, "ctx": None, "page": None}
            self._mint_sessions[acc.id] = sess

        async with sess["lock"]:
            # mở (hoặc tái dùng) trình duyệt profile của acc
            if sess["cdp"] is None:
                exe = find_browser_exe(acc.browser)
                if not exe:
                    raise RuntimeError(f"Không tìm thấy trình duyệt {acc.browser}")
                port = self._acc_port(acc)
                close = acc.browser if acc.mode == "existing" else None
                _flog(f"mint {acc.name}: mở {acc.browser} (port {port}, profile login)...", "acc")
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._launch(exe, acc.login_user_data_dir, acc.login_profile,
                                               port, FLOW_URL, headless=False, close_browser=close))
                _flog(f"mint {acc.name}: kết nối CDP 127.0.0.1:{port}...", "acc")
                cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                ctx = cdp.contexts[0] if cdp.contexts else await cdp.new_context()
                sess["cdp"] = cdp
                sess["ctx"] = ctx
                _flog(f"mint {acc.name}: trình duyệt sẵn sàng.", "acc")

            ctx = sess["ctx"]
            # KHÔNG bơm acc.cookies (cũ) vào profile: profile đã đăng nhập sẵn với cookie
            # SỐNG; bơm cookie cũ sẽ ghi đè làm hỏng phiên -> 401. Để profile tự dùng.

            page = sess["page"]
            if page is None:
                page = await ctx.new_page()
                sess["page"] = page
            url = f"https://labs.google/fx/tools/flow/project/{project_id}" if project_id else FLOW_URL
            token = None
            last_err = None
            for attempt in range(1, 4):     # thử mint tối đa 3 lần
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    last_err = f"goto:{e}"
                ready = False
                try:
                    await page.wait_for_function(
                        "() => window.grecaptcha && window.grecaptcha.enterprise && "
                        "typeof window.grecaptcha.enterprise.execute === 'function'", timeout=25000)
                    ready = True
                except Exception:
                    last_err = "grecaptcha.enterprise.execute chưa sẵn sàng"
                await asyncio.sleep(1.0 if ready else 2.0)
                try:
                    res = await page.evaluate(EXEC_JS, [RECAPTCHA_KEY, RECAPTCHA_ACTION])
                except Exception as e:
                    res = {"error": str(e)}
                if isinstance(res, dict) and res.get("token"):
                    token = res["token"]
                    _flog(f"mint {acc.name}: token OK (lần {attempt}, len={len(token)})", "acc")
                    break
                last_err = (res.get("error") if isinstance(res, dict) else "unknown") or "empty"
                _flog(f"mint {acc.name} lần {attempt} thất bại: {last_err}", "acc")
                await asyncio.sleep(1.5)
            if not token:
                acc.last_error = f"no_recaptcha_token ({last_err})"
        return token

    def get_token(self, acc: Account, project_id=None, timeout=150):
        if not acc.cookies:
            acc.last_error = "chưa có cookie (chưa đăng nhập/kiểm tra)"
            return None
        try:
            return self._submit(self._mint_async(acc, project_id), timeout=timeout)
        except Exception as e:
            import traceback
            acc.last_error = str(e)
            _flog(f"mint {acc.name} NGOẠI LỆ: {type(e).__name__}: {str(e)[:200]}", "acc")
            try:
                _flog("mint trace: " + " | ".join(
                    traceback.format_exc().strip().splitlines()[-3:]), "acc")
            except Exception:
                pass
            return None

    def get_cookie_header(self, acc: Account, timeout=30):
        return acc.cookie_header()

    # ---- Lấy/tạo PROJECT NGAY TRONG trình duyệt đã đăng nhập (fetch của trang) ----
    # Dùng khi gọi trpc bằng requests bị 401 dù cookie hợp lệ (server chặn request
    # không phải từ trình duyệt). Chạy trong page labs.google -> cookie sống + origin
    # chuẩn nên luôn thành công.
    _PROJECT_JS = """
    async () => {
      const B = 'https://labs.google';
      const mk = (o) => encodeURIComponent(JSON.stringify(o));
      const out = {ok:false};
      try {
        // access_token từ phiên SỐNG của profile
        try {
          const a = await fetch(B+'/fx/api/auth/session', {credentials:'include'});
          if (a.ok) { const aj = await a.json(); out.token = aj.access_token || null; }
        } catch(e) {}
        // project: tái dùng nếu có, không thì tạo mới
        const inp = mk({json:{pageSize:20,toolName:"PINHOLE",cursor:null},
                        meta:{values:{cursor:["undefined"]}}});
        let r = await fetch(B+'/fx/api/trpc/project.searchUserProjects?input='+inp,
                            {credentials:'include', headers:{'content-type':'application/json'}});
        if (r.ok) {
          const j = await r.json();
          const ps = j?.result?.data?.json?.result?.projects || [];
          if (ps.length) { out.ok=true; out.id=ps[0].projectId; out.mode='reuse'; return out; }
        }
        let cr = await fetch(B+'/fx/api/trpc/project.createProject',
            {method:'POST', credentials:'include',
             headers:{'content-type':'application/json'},
             body: JSON.stringify({json:{projectTitle:'Auto', toolName:'PINHOLE'}})});
        if (cr.ok) {
          const j = await cr.json();
          out.ok=true; out.id=j?.result?.data?.json?.result?.projectId; out.mode='create';
          return out;
        }
        out.status = cr.status; return out;
      } catch(e) { out.error = String(e); return out; }
    }
    """

    async def _project_via_browser_async(self, acc: Account):
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        pw = self._pw
        sess = self._mint_sessions.get(acc.id)
        if sess is None:
            sess = {"lock": asyncio.Lock(), "cdp": None, "ctx": None, "page": None}
            self._mint_sessions[acc.id] = sess
        async with sess["lock"]:
            if sess["cdp"] is None:
                exe = find_browser_exe(acc.browser)
                if not exe:
                    raise RuntimeError(f"Không tìm thấy trình duyệt {acc.browser}")
                port = self._acc_port(acc)
                close = acc.browser if acc.mode == "existing" else None
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._launch(exe, acc.login_user_data_dir, acc.login_profile,
                                               port, FLOW_URL, headless=False, close_browser=close))
                cdp = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                ctx = cdp.contexts[0] if cdp.contexts else await cdp.new_context()
                sess["cdp"] = cdp
                sess["ctx"] = ctx
            ctx = sess["ctx"]
            # KHÔNG bơm acc.cookies (cũ): để profile dùng cookie SỐNG của chính nó.
            page = sess["page"]
            if page is None:
                page = await ctx.new_page()
                sess["page"] = page
            try:
                await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            res = await page.evaluate(self._PROJECT_JS)
            tok_len = len(res.get("token") or "") if isinstance(res, dict) else 0
            _flog(f"project(browser) {acc.name}: ok={res.get('ok')} mode={res.get('mode')} "
                  f"id={res.get('id')} token_len={tok_len} status={res.get('status')}", "acc")
            if isinstance(res, dict) and res.get("ok") and res.get("id"):
                return {"id": res["id"], "token": res.get("token")}
            acc.last_error = f"project_via_browser: {res}"
            return None

    def get_project_via_browser(self, acc: Account, timeout=90):
        try:
            return self._submit(self._project_via_browser_async(acc), timeout=timeout)
        except Exception as e:
            acc.last_error = str(e)
            _flog(f"project(browser) {acc.name} LỖI: {e}", "acc")
            return None

    # ----- scheduler -----
    def healthy_accounts(self):
        now = time.time()
        return [a for a in self.accounts
                if a.enabled and a.cooldown_until <= now and a.logged_in]

    def has_capacity(self) -> bool:
        """Có acc khỏe nào còn chỗ (dưới ngưỡng 4 luồng) không?"""
        return any(a.inflight < MAX_CONCURRENCY_PER_ACC for a in self.healthy_accounts())

    def pick_account(self):
        """Chọn acc khỏe còn dưới 4 luồng. Ưu tiên acc đang rảnh nhất -> tự xoay
        sang nick 2,3,4... khi nick trước đã đủ 4 luồng. Trả None nếu hết chỗ."""
        with self._sel_lock:
            pool = [a for a in self.healthy_accounts() if a.inflight < MAX_CONCURRENCY_PER_ACC]
            if not pool:
                return None
            # ưu tiên: ít luồng đang chạy -> ít lỗi -> ít dùng
            pool.sort(key=lambda a: (a.inflight, a.failures, a.uses))
            acc = pool[0]
            acc.inflight += 1
            acc.uses += 1
            return acc

    def release_account(self, acc):
        if acc is not None:
            acc.inflight = max(0, acc.inflight - 1)

    def report_success(self, acc):
        acc.failures = 0; acc.status = "ready"; acc.last_error = ""

    def report_failure(self, acc, reason="", quota=False):
        acc.failures += 1; acc.last_error = reason
        # Lỗi xác thực -> phiên hết hạn / đăng xuất: đánh dấu để UI hiện "chưa đăng nhập".
        low = (reason or "").lower()
        if "401" in low or "unauthorized" in low or "token_expired" in low:
            # TỰ ĐỘNG xác thực lại bằng cookie đã lưu (HTTP, nhanh) trước khi kết luận
            # "cần đăng nhập lại". Nếu cookie còn hạn -> tự khôi phục, KHÔNG bắt login.
            try:
                vr = self.verify_account(acc)
            except Exception:
                vr = {"ok": False}
            if vr.get("ok"):
                acc.verified = True
                acc.cooldown_until = time.time() + 15   # nghỉ ngắn rồi thử lại
                acc.status = "ready"
                _flog(f"{acc.name}: 401 nhưng cookie còn hạn -> tự khôi phục, nghỉ 15s", "acc")
            else:
                acc.verified = False
                acc.status = "login_needed"
                _flog(f"{acc.name} -> phiên hết hạn thật (cần đăng nhập lại): {reason[:80]}", "acc")
            return
        if quota or acc.failures >= 2:
            # Lỗi recaptcha là lỗi TẠM THỜI (browser chưa mint được token), không phải
            # bị ban/quota -> cooldown ngắn để acc sớm thử lại (nhất là khi chỉ 1 acc).
            cd = 20 if ("recaptcha" in low and not quota) else COOLDOWN_SECONDS
            acc.cooldown_until = time.time() + cd
            acc.status = "cooldown"
            _flog(f"{acc.name} -> cooldown {cd}s ({reason[:80]})", "acc")
        else:
            _flog(f"{acc.name} lỗi (#{acc.failures}): {reason[:80]}", "acc")

    def start_all_enabled(self, headless=False):
        # với mô hình v2 không cần mở trình duyệt acc; chỉ cần đã có cookie
        pass

    def states(self):
        return [a.public_state() for a in self.accounts]

    def stop_all(self):
        self._keepalive_stop = True
        async def _close():
            for sess in self._mint_sessions.values():
                try:
                    if sess.get("cdp"):
                        await sess["cdp"].close()
                except Exception:
                    pass
            for sess in self._manual_sessions.values():
                try:
                    if sess.get("cdp"):
                        await sess["cdp"].close()
                except Exception:
                    pass
            for w in self._workers:
                try:
                    await w.cdp.close()
                except Exception:
                    pass
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:
                pass
            try:
                if getattr(self, "_pw_workers", None):
                    await self._pw_workers.stop()
            except Exception:
                pass
        try:
            self._submit(_close(), timeout=15)
        except Exception:
            pass
        # Đóng các tiến trình trình duyệt do tool mở -> tránh orphan khóa profile cho lần sau
        for p in list(self._launched_procs):
            try:
                p.terminate()
            except Exception:
                pass
        self._launched_procs.clear()
