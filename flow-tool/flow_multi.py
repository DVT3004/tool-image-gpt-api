# -*- coding: utf-8 -*-
"""
MultiFlow - tạo ảnh Flow qua nhiều tài khoản, xoay vòng + chạy song song.
Mỗi acc tự quản access_token + projectId riêng (theo cookie của acc).
"""

import json
import time
import uuid
import random
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List

import requests

from flow_accounts import AccountManager, Account

try:
    from flow_log import log as _flog
except Exception:
    def _flog(msg, tag="info"):
        print(f"[{tag}] {msg}")

SESSION_URL = "https://labs.google/fx/api/auth/session"
CREATE_PROJECT_URL = "https://labs.google/fx/api/trpc/project.createProject"
SANDBOX_BASE = "https://aisandbox-pa.googleapis.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# Bật/tắt ghi log chi tiết request API tạo ảnh.
API_LOG = True
API_LOG_FILE = pathlib.Path(__file__).parent / "api_requests.log"
_api_lock = threading.Lock()


def _api_log(msg):
    """Ghi log request/response API tạo ảnh ra api_requests.log + flow.log."""
    if not API_LOG:
        return
    _flog(msg, tag="api")
    try:
        with _api_lock:
            with open(API_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


class _AccSession:
    """Phiên HTTP + access_token + project cho 1 acc."""
    def __init__(self, mgr: AccountManager, acc: Account):
        self.mgr = mgr
        self.acc = acc
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            # Trình duyệt luôn gửi Origin/Referer tới labs.google + aisandbox. Nhiều
            # endpoint trpc CHẶN request thiếu chúng (401) dù cookie hợp lệ -> thêm vào.
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
        })
        self.access_token = None
        self.access_ts = 0.0
        self.project_id = None
        self.last_detail = ""
        self._cookie_loaded = False
        self._browser_token = None      # access_token lấy từ trình duyệt (phiên sống)
        self.lock = threading.Lock()

    def refresh_cookie(self, force=False):
        """Nạp cookie đã lưu của acc vào COOKIE JAR (theo domain) thay vì header tĩnh.
        Như vậy requests sẽ tôn trọng Set-Cookie (NextAuth xoay cookie) ở các request sau."""
        if self._cookie_loaded and not force:
            return True
        # bỏ header Cookie tĩnh nếu lỡ có, để jar điều khiển
        self.session.headers.pop("Cookie", None)
        try:
            self.session.cookies.clear()
        except Exception:
            pass
        n = 0
        for c in (self.acc.cookies or []):
            name = c.get("name"); val = c.get("value")
            if not name or val is None:
                continue
            dom = (c.get("domain") or "").lstrip(".") or "labs.google"
            try:
                self.session.cookies.set(name, val, domain=dom, path=c.get("path", "/"))
                n += 1
            except Exception:
                pass
        self._cookie_loaded = n > 0
        return self._cookie_loaded

    def get_access_token(self, force=False) -> Optional[str]:
        if not force and self.access_token and time.time() - self.access_ts < 50 * 60:
            return self.access_token
        self.refresh_cookie(force=force)
        r = self.session.get(SESSION_URL, timeout=30)
        if r.status_code != 200:
            self.last_detail = f"session HTTP {r.status_code}: {r.text[:120]}"
            return None
        tok = (r.json() or {}).get("access_token")
        if tok:
            self.access_token = tok
            self.access_ts = time.time()
        else:
            self.last_detail = "session ko co access_token (cookie het han?)"
        return tok

    def _find_existing_project(self) -> Optional[str]:
        """Tái dùng project sẵn có của nick qua GET searchUserProjects (cùng kiểu auth
        với /auth/session). Né createProject (POST trpc hay 401)."""
        try:
            inp = {"json": {"pageSize": 20, "toolName": "PINHOLE", "cursor": None},
                   "meta": {"values": {"cursor": ["undefined"]}}}
            r = self.session.get(
                "https://labs.google/fx/api/trpc/project.searchUserProjects",
                params={"input": json.dumps(inp, separators=(",", ":"))}, timeout=30)
            if r.status_code != 200:
                return None
            projs = ((((r.json() or {}).get("result") or {}).get("data") or {})
                     .get("json", {}).get("result", {}).get("projects") or [])
            if projs:
                return projs[0].get("projectId")
        except Exception as e:
            self.last_detail = f"searchUserProjects err: {e}"
        return None

    def get_project(self) -> Optional[str]:
        if self.project_id:
            return self.project_id
        # 1) Thử nhanh qua requests (nếu server cho phép).
        pid = self._find_existing_project()
        if pid:
            self.project_id = pid
            return pid
        # 2) requests bị 401 (server chặn) -> lấy/tạo project NGAY TRONG trình duyệt
        #    đã đăng nhập (fetch của trang). Trả kèm access_token của phiên SỐNG.
        try:
            info = self.mgr.get_project_via_browser(self.acc)
        except Exception as e:
            info = None
            self.last_detail = f"project_via_browser err: {e}"
        if isinstance(info, dict) and info.get("id"):
            self.project_id = info["id"]
            if info.get("token"):
                self._browser_token = info["token"]   # token hợp lệ từ profile
            return self.project_id
        # 3) fallback cuối: createProject qua requests.
        body = {"json": {"projectTitle": f"Auto {self.acc.id}", "toolName": "PINHOLE"}}
        r = None
        for attempt in range(2):
            r = self.session.post(CREATE_PROJECT_URL, json=body,
                                  headers={"Content-Type": "application/json"}, timeout=30)
            if r.status_code != 401:
                break
            self.refresh_cookie(force=True)
            self.get_access_token(force=True)
        if r.status_code != 200:
            self.last_detail = f"createProject HTTP {r.status_code}: {r.text[:150]}"
            return None
        try:
            self.project_id = r.json()["result"]["data"]["json"]["result"]["projectId"]
        except Exception as e:
            self.last_detail = f"createProject parse: {e} | {r.text[:150]}"
            return None
        return self.project_id

    def upload_image(self, project_id, image_bytes, access_token=None) -> Optional[str]:
        """Upload 1 ảnh local lên Flow -> trả mediaId (name) để dùng làm ảnh gốc.
        Endpoint: POST /v1/flow/uploadImage, body {clientContext, imageBytes(base64)}."""
        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {"clientContext": {"projectId": project_id, "tool": "PINHOLE"},
                "imageBytes": b64}
        url = f"{SANDBOX_BASE}/v1/flow/uploadImage"
        # ƯU TIÊN token TRÌNH DUYỆT (phiên sống) -> uploadImage không bị 401 như trpc.
        tok = access_token or self._browser_token or self.access_token
        r = None
        for attempt in range(2):
            r = self.session.post(url, data=json.dumps(body),
                                  headers={"Authorization": f"Bearer {tok}",
                                           "Content-Type": "text/plain;charset=UTF-8"},
                                  timeout=120)
            if r.status_code != 401:
                break
            self.refresh_cookie(force=True)
            tok = self._browser_token or self.get_access_token(force=True) or tok
        if r.status_code != 200:
            self.last_detail = f"uploadImage HTTP {r.status_code}: {r.text[:150]}"
            return None
        try:
            return r.json()["media"]["name"]
        except Exception as e:
            self.last_detail = f"uploadImage parse: {e} | {r.text[:150]}"
            return None


class MultiFlow:
    def __init__(self, manager: AccountManager, max_workers: int = 4, max_account_retries: int = 3):
        self.mgr = manager
        self.max_workers = max_workers
        self.max_account_retries = max_account_retries
        self._sessions = {}
        self._sessions_lock = threading.Lock()

    def _sess(self, acc: Account) -> _AccSession:
        with self._sessions_lock:
            s = self._sessions.get(acc.id)
            if not s:
                s = _AccSession(self.mgr, acc)
                self._sessions[acc.id] = s
            return s

    def _build_body(self, prompt, model, aspect, seed, project_id, recaptcha_token,
                    image_inputs=None, input_type="IMAGE_INPUT_TYPE_BASE_IMAGE"):
        cc = {
            "recaptchaContext": {"token": recaptcha_token,
                                 "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"},
            "projectId": project_id, "tool": "PINHOLE",
            "sessionId": ";" + str(int(time.time() * 1000)),
        }
        # image_inputs: danh sách mediaId. input_type:
        #   IMAGE_INPUT_TYPE_REFERENCE  -> img2img (ảnh đưa vào làm tham chiếu)
        #   IMAGE_INPUT_TYPE_BASE_IMAGE -> sửa ảnh đã tạo (ảnh gốc để edit)
        img_inputs = [{"imageInputType": input_type, "name": mid}
                      for mid in (image_inputs or [])]
        return {
            "clientContext": cc,
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "useNewMedia": True,
            "requests": [{
                "clientContext": cc,
                "imageModelName": model,
                "imageAspectRatio": aspect,
                "structuredPrompt": {"parts": [{"text": prompt}]},
                "seed": seed if seed is not None else random.randint(1, 2_147_483_646),
                "imageInputs": img_inputs,
            }],
        }

    def _gen_with_account(self, acc: Account, prompt, model, aspect, seed,
                          image_bytes=None,
                          input_type="IMAGE_INPUT_TYPE_REFERENCE") -> Optional[bytes]:
        """Tạo 1 ảnh bằng 1 acc cụ thể. Raise nếu token bị từ chối (để xoay acc).
        Nếu có image_bytes -> upload làm ảnh THAM CHIẾU (img2img) theo prompt."""
        s = self._sess(acc)
        with s.lock:
            access = s.get_access_token()   # có thể là token cũ; sẽ ưu tiên token trình duyệt
            project = s.get_project()        # set self._browser_token (token phiên SỐNG)
            if not project:
                raise RuntimeError(f"no_project: {s.last_detail}")
        # Ưu tiên access_token lấy từ TRÌNH DUYỆT (phiên sống) -> aisandbox chấp nhận.
        access = s._browser_token or access
        if not access:
            raise RuntimeError(f"no_access_token: {s.last_detail}")
        # Upload ảnh gốc (nếu là chế độ sửa ảnh) TRƯỚC khi mint token.
        image_inputs = None
        if image_bytes:
            with s.lock:
                media_id = s.upload_image(project, image_bytes, access_token=access)
            if not media_id:
                raise RuntimeError(f"upload_image_failed: {s.last_detail}")
            image_inputs = [media_id]
            _api_log(f"[REQ] acc={acc.name} uploadImage OK media={media_id}")
        token = self.mgr.get_token(acc, project_id=project)
        if not token:
            raise RuntimeError("no_recaptcha_token")
        # access_token cho batchGenerateImages: ƯU TIÊN token lấy từ TRÌNH DUYỆT
        # (phiên sống của profile) vì acc.cookies trong file có thể đã cũ -> token
        # qua requests bị aisandbox từ chối 401.
        with s.lock:
            s.refresh_cookie(force=True)
            access = s._browser_token or s.get_access_token(force=True) or access
        body = self._build_body(prompt, model, aspect, seed, project, token,
                                image_inputs=image_inputs, input_type=input_type)
        url = f"{SANDBOX_BASE}/v1/projects/{project}/flowMedia:batchGenerateImages"

        # Mã model + seed thực sự GỬI LÊN (để biết rõ Flow nhận model nào)
        try:
            req0 = body["requests"][0]
            sent_model = req0.get("imageModelName")
            sent_seed = req0.get("seed")
            sent_ratio = req0.get("imageAspectRatio")
        except Exception:
            sent_model = model; sent_seed = seed; sent_ratio = aspect
        _api_log(f"[REQ] acc={acc.name} POST {url}")
        _api_log(f"[REQ] imageModelName={sent_model} | aspect={sent_ratio} | "
                 f"seed={sent_seed} | prompt={str(prompt)[:80]!r}")

        # Gọi tạo ảnh; nếu 401 (token hết hạn) thì làm mới access_token và thử lại
        # ngay 1 lần trước khi bỏ cuộc (tránh cooldown oan vì token cũ).
        r = None
        for attempt in range(2):
            r = s.session.post(url, data=json.dumps(body),
                               headers={"Authorization": f"Bearer {access}",
                                        "Content-Type": "text/plain;charset=UTF-8"}, timeout=120)
            _api_log(f"[RESP] acc={acc.name} HTTP {r.status_code} "
                     f"(model gửi={sent_model}, thử {attempt + 1})")
            if r.status_code != 401:
                break
            # 401 -> lấy lại access_token TƯƠI từ trình duyệt rồi thử lại (lần cuối mới raise)
            new_tok = None
            try:
                info = self.mgr.get_project_via_browser(acc)
                if isinstance(info, dict):
                    new_tok = info.get("token")
            except Exception:
                new_tok = None
            with s.lock:
                access = new_tok or s.get_access_token(force=True)
            if access and new_tok:
                s._browser_token = new_tok
            if not access:
                raise RuntimeError(f"401_no_access_token: {s.last_detail}")
        if r.status_code == 401:
            _api_log(f"[RESP] acc={acc.name} 401 body={r.text[:300]}")
            raise RuntimeError("401_token_expired")
        if r.status_code == 403:
            _api_log(f"[RESP] acc={acc.name} 403 body={r.text[:300]}")
            raise PermissionError(f"403:{r.text[:400]}")
        if r.status_code != 200:
            _api_log(f"[RESP] acc={acc.name} HTTP {r.status_code} body={r.text[:300]}")
            raise RuntimeError(f"http_{r.status_code}:{r.text[:400]}")
        data = r.json()
        media = data.get("media", [])
        # Model mà server BÁO ĐÃ DÙNG (nếu có) -> so với model gửi lên
        used_model = None
        try:
            gi = media[0].get("image", {}).get("generatedImage", {}) if media else {}
            used_model = gi.get("modelKey") or gi.get("imageModelName") or gi.get("model")
        except Exception:
            pass
        _api_log(f"[RESP] acc={acc.name} OK media={len(media)} | "
                 f"model gửi={sent_model} | model server dùng={used_model}")
        if not media:
            raise RuntimeError("no_media")
        m0 = media[0]
        fife = m0.get("image", {}).get("generatedImage", {}).get("fifeUrl")
        if not fife:
            raise RuntimeError("no_fifeurl")
        img = requests.get(fife, timeout=120)
        img.raise_for_status()
        meta = {"media_id": m0.get("name"), "project_id": project, "acc_id": acc.id}
        return img.content, meta

    def generate_one(self, prompt, model, aspect, seed, image_bytes=None) -> bytes:
        """Tạo 1 ảnh. Mỗi acc tối đa 4 luồng; quá thì xoay sang acc khác.
        Nếu mọi acc đều đang đủ 4 luồng -> chờ tới khi có chỗ trống."""
        last = "no_account"
        tried = 0
        wait_deadline = time.time() + 300   # tối đa chờ 5 phút khi tất cả acc bận
        while tried < self.max_account_retries:
            acc = self.mgr.pick_account()
            if not acc:
                # Hết chỗ: nếu vẫn còn acc khỏe (chỉ là đang đủ 4 luồng) -> chờ rồi thử lại
                if self.mgr.healthy_accounts() and time.time() < wait_deadline:
                    time.sleep(0.5)
                    continue
                reasons = []
                for a in self.mgr.accounts:
                    if not a.enabled:
                        continue
                    if not a.logged_in:
                        reasons.append(f"{a.name}: chưa đăng nhập/cookie")
                    elif a.cooldown_until > time.time():
                        reasons.append(f"{a.name}: cooldown ({a.last_error[:60]})")
                detail = " | ".join(reasons) if reasons else "chưa có acc nào đăng nhập"
                if tried == 0:
                    raise RuntimeError(f"Không có acc sẵn sàng. {detail}")
                raise RuntimeError(f"Hết acc khỏe. Lỗi gần nhất: {last}")
            tried += 1
            try:
                data = self._gen_with_account(acc, prompt, model, aspect, seed, image_bytes)
                self.mgr.report_success(acc)
                return data
            except PermissionError as e:
                last = str(e)
                self.mgr.report_failure(acc, last, quota=True)
                time.sleep(0.3)
            except Exception as e:
                last = str(e)
                quota = ("Unauthorized" in last or "quota" in last.lower()
                         or "RESOURCE_EXHAUSTED" in last)
                self.mgr.report_failure(acc, last, quota=quota)
                time.sleep(0.2)
            finally:
                self.mgr.release_account(acc)
        raise RuntimeError(f"Hết lượt thử. Lỗi cuối: {last}")

    # ====================================================== SỬA ẢNH ĐÃ TẠO
    def _edit_with_account(self, acc: Account, project_id, media_id, prompt, model, aspect, seed):
        """batchGenerateImages với ảnh GỐC = media_id đã có trong project (BASE_IMAGE)."""
        s = self._sess(acc)
        # đảm bảo có token hợp lệ từ trình duyệt (phiên sống)
        if not s._browser_token:
            try:
                info = self.mgr.get_project_via_browser(acc)
                if isinstance(info, dict) and info.get("token"):
                    s._browser_token = info["token"]
            except Exception:
                pass
        with s.lock:
            access = s._browser_token or s.get_access_token()
        token = self.mgr.get_token(acc, project_id=project_id)
        if not token:
            raise RuntimeError("no_recaptcha_token")
        with s.lock:
            access = s._browser_token or s.get_access_token(force=True) or access
        if not access:
            raise RuntimeError(f"no_access_token: {s.last_detail}")
        body = self._build_body(prompt, model, aspect, seed, project_id, token,
                                image_inputs=[media_id],
                                input_type="IMAGE_INPUT_TYPE_BASE_IMAGE")
        url = f"{SANDBOX_BASE}/v1/projects/{project_id}/flowMedia:batchGenerateImages"
        _api_log(f"[REQ] acc={acc.name} EDIT base={media_id} prompt={str(prompt)[:60]!r}")
        r = None
        for attempt in range(2):
            r = s.session.post(url, data=json.dumps(body),
                               headers={"Authorization": f"Bearer {access}",
                                        "Content-Type": "text/plain;charset=UTF-8"}, timeout=120)
            if r.status_code != 401:
                break
            new_tok = None
            try:
                info = self.mgr.get_project_via_browser(acc)
                if isinstance(info, dict):
                    new_tok = info.get("token")
            except Exception:
                new_tok = None
            access = new_tok or access
            if new_tok:
                s._browser_token = new_tok
        if r.status_code == 401:
            raise RuntimeError("401_token_expired")
        if r.status_code == 403:
            raise PermissionError(f"403:{r.text[:300]}")
        if r.status_code != 200:
            raise RuntimeError(f"http_{r.status_code}:{r.text[:300]}")
        media = (r.json() or {}).get("media", [])
        if not media:
            raise RuntimeError("no_media")
        m0 = media[0]
        fife = m0.get("image", {}).get("generatedImage", {}).get("fifeUrl")
        if not fife:
            raise RuntimeError("no_fifeurl")
        img = requests.get(fife, timeout=120)
        img.raise_for_status()
        meta = {"media_id": m0.get("name"), "project_id": project_id, "acc_id": acc.id}
        return img.content, meta

    def edit_image(self, acc_id, project_id, media_id, prompt, model, aspect,
                   n=1, seed=None, retries=2, log=None, detailed=True):
        """Sửa 1 ảnh ĐÃ TẠO (media_id) trong ĐÚNG acc+project đã sinh ra nó."""
        acc = self.mgr.get(acc_id)
        if not acc:
            raise RuntimeError("Không tìm thấy tài khoản của ảnh này.")
        out, last = [], None
        try:
            acc.inflight += 1
        except Exception:
            pass
        try:
            for _k in range(max(1, n)):
                ok = False
                for attempt in range(max(1, retries + 1)):
                    try:
                        sd = seed if (seed is not None and n == 1) else None
                        res = self._edit_with_account(acc, project_id, media_id,
                                                      prompt, model, aspect, sd)
                        out.append(res)
                        ok = True
                        break
                    except Exception as e:
                        last = str(e)
                        if log:
                            try:
                                log(f"Sửa ảnh lỗi (thử {attempt + 1}): {last}")
                            except Exception:
                                pass
                        time.sleep(0.5)
                if not ok and not out:
                    raise RuntimeError(last or "edit_failed")
        finally:
            try:
                acc.inflight = max(0, acc.inflight - 1)
            except Exception:
                pass
        if detailed:
            return [{"bytes": b, **(m or {})} for (b, m) in out]
        return [b for (b, m) in out]

    def get_account_status(self, acc: Account) -> dict:
        """Trạng thái acc: email + còn đăng nhập + điểm/quota còn lại."""
        info = {"email": None, "expires": None, "credits": None,
                "logged_in": False, "error": None}
        # cần browser chạy (cổng debug) để đọc cookie qua CDP
        if acc.status != "ready":
            self.mgr.start_account(acc.id)
        cookie = self.mgr.get_cookie_header(acc)
        if not cookie:
            info["error"] = "Chưa lấy được cookie (acc chưa đăng nhập / chưa mở được)."
            return info
        s = self._sess(acc)
        s.session.headers["Cookie"] = cookie
        try:
            r = s.session.get(SESSION_URL, timeout=20)
            if r.status_code == 200:
                j = r.json() or {}
                u = j.get("user") or {}
                info["email"] = u.get("email") or u.get("name")
                info["expires"] = j.get("expires")
                tok = j.get("access_token")
                if tok:
                    s.access_token = tok
                    s.access_ts = time.time()
                    info["logged_in"] = True
        except Exception as e:
            info["error"] = f"session: {e}"
        # điểm/quota
        try:
            if s.access_token:
                cr = s.session.get(f"{SANDBOX_BASE}/v1/credits",
                                   headers={"Authorization": f"Bearer {s.access_token}"}, timeout=20)
                if cr.status_code == 200:
                    info["credits"] = cr.json()
                else:
                    info["credits"] = {"http": cr.status_code, "body": cr.text[:120]}
        except Exception as e:
            info["credits"] = {"error": str(e)}
        return info

    def generate(self, prompt, model, aspect, n=1, seed=None,
                 retries=3, fallback_models=None, log=None, image_bytes=None,
                 detailed=False) -> List[bytes]:
        """Tạo n ảnh SONG SONG qua nhiều acc.

        - retries: số lần thử lại RIÊNG cho từng ảnh bị lỗi (mặc định 3) để không
          bị "miss" ảnh nào. Ảnh nào lỗi sẽ được gom lại và tạo lại ở vòng kế.
        - fallback_models: danh sách model dự phòng. Khi retry sẽ xoay vòng qua
          các model này (vd: có 2 model thì lần lỗi sau thử model còn lại).
        - log: callback(str) tuỳ chọn để báo tiến độ retry.
        """
        def _say(msg):
            if log:
                try:
                    log(msg)
                except Exception:
                    pass

        # Danh sách model dùng để xoay vòng khi retry: [chính, *dự phòng]
        model_chain = [model] + [m for m in (fallback_models or [])
                                 if m and m != model]

        results = [None] * n
        pending = list(range(n))
        last_errors = []

        attempt = 0
        max_attempts = 1 + max(0, int(retries))   # lần đầu + số lần retry
        while pending and attempt < max_attempts:
            cur_model = model_chain[min(attempt, len(model_chain) - 1)]
            if attempt > 0:
                _say(f"Retry lần {attempt}/{retries}: tạo lại {len(pending)} ảnh "
                     f"bị lỗi (model: {cur_model}).")
            errors = []
            with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(pending)))) as ex:
                futs = {}
                for i in pending:
                    # Giữ seed cố định chỉ khi tạo đúng 1 ảnh duy nhất
                    sd = seed if (seed is not None and n == 1) else None
                    futs[ex.submit(self.generate_one, prompt, cur_model, aspect, sd,
                                   image_bytes)] = i
                for fut in as_completed(futs):
                    i = futs[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        errors.append(str(e))
            pending = [i for i in range(n) if results[i] is None]
            last_errors = errors or last_errors
            attempt += 1
            # Còn ảnh lỗi và vẫn còn lượt retry -> nghỉ (acc có thể đang cooldown)
            if pending and attempt < max_attempts:
                joined = " ".join(last_errors).lower()
                if any(k in joined for k in ("cooldown", "recaptcha", "401", "token_expired", "đăng nhập")):
                    backoff = 22.0   # đợi qua cooldown ngắn (15-20s) của lỗi auth/recaptcha
                else:
                    backoff = min(5.0 * attempt, 30.0)
                _say(f"Còn {len(pending)} ảnh lỗi, nghỉ {backoff:.0f}s rồi thử lại...")
                time.sleep(backoff)

        out = [r for r in results if r]   # mỗi r = (bytes, meta)
        if pending:
            _say(f"Vẫn còn {len(pending)} ảnh không tạo được sau {retries} lần thử. "
                 f"Lỗi gần nhất: {last_errors[0] if last_errors else 'n/a'}")
        if not out and last_errors:
            raise RuntimeError(last_errors[0])
        if detailed:
            return [{"bytes": b, **(m or {})} for (b, m) in out]
        return [b for (b, m) in out]
