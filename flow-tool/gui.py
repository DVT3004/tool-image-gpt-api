# -*- coding: utf-8 -*-
"""
Tool vẽ ảnh bằng Google Labs Flow API (giao diện đồ hoạ) - Hỗ trợ Đa tài khoản & Đa luồng.
"""

import os
import re
import threading
import traceback
import pathlib
import time
import random
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import cookie_grabber as cg
from flow_accounts import AccountManager, Account
from flow_multi import MultiFlow

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

HERE = pathlib.Path(__file__).parent
COOKIE_FILE = HERE / "flow_cookie.txt"

ASPECTS = ["16:9", "9:16", "1:1"]
ASPECT_MAP = {
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
}
# Lựa chọn nhanh dạng x1..x4 cho Số ảnh / Số luồng
COUNT_CHOICES = ["x1", "x2", "x3", "x4"]
# Số luồng song song cho nhiều mức hơn (mỗi tài khoản tối đa 4 luồng -> nhiều acc thì tổng cao hơn)
WORKER_CHOICES = ["x1", "x2", "x3", "x4", "x6", "x8", "x12", "x16", "x24", "x32", "x48", "x60"]


def parse_count(s, default=1):
    """'x3' -> 3 ; '2' -> 2 ; lỗi -> default."""
    try:
        return max(1, int(str(s).lower().lstrip("x")))
    except Exception:
        return default

# Mã model ẢNH thật của Flow (lấy từ request thực tế):
#   GEM_PIX_2 = Nano Banana Pro | NARWHAL = Nano Banana 2
MODELS = ["GEM_PIX_2", "NARWHAL"]
MODELS_FILE = HERE / "models.json"


def is_video_model(name):
    """True nếu mã model là model VIDEO/âm thanh (không dùng để tạo ảnh)."""
    n = str(name).lower()
    return n.startswith("veo") or "video" in n or "audio" in n


# Tên hiển thị mặc định cho vài mã model đã biết.
DEFAULT_LABELS = {"GEM_PIX_2": "Nano Banana Pro", "NARWHAL": "Nano Banana 2"}


def load_model_entries():
    """Trả về list dict {'label','code'} cho các MODEL ẢNH (bỏ model video).

    models.json hỗ trợ 2 dạng phần tử:
      - chuỗi:  "NARWHAL"
      - object: {"label": "Nano Banana Pro", "code": "abra"}
    """
    import json
    entries = []
    seen = set()

    def _add(code, label=None):
        if not code or is_video_model(code) or code in seen:
            return
        seen.add(code)
        entries.append({"code": code, "label": label or DEFAULT_LABELS.get(code, code)})

    for code in MODELS:           # mặc định (NARWHAL)
        _add(code)
    try:
        if MODELS_FILE.exists():
            data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
            items = data.get("models") if isinstance(data, dict) else data
            for m in items or []:
                if isinstance(m, dict):
                    _add(m.get("code"), m.get("label"))
                else:
                    _add(m)
    except Exception:
        pass
    return entries


def save_model_entries(entries):
    """Lưu list dict {'label','code'} (bỏ model video) vào models.json."""
    import json
    out = []
    seen = set()
    for e in entries:
        code = e.get("code") if isinstance(e, dict) else e
        label = e.get("label") if isinstance(e, dict) else e
        if not code or is_video_model(code) or code in seen:
            continue
        seen.add(code)
        out.append({"label": label or code, "code": code})
    try:
        MODELS_FILE.write_text(json.dumps({"models": out}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


def load_models():
    """Tương thích cũ: chỉ trả về danh sách MÃ model ảnh."""
    return [e["code"] for e in load_model_entries()]


class FlowGUI:
    def __init__(self, root):
        self.root = root
        root.title("Flow Multi-Account Image Tool")
        root.geometry("1180x820")
        root.minsize(960, 640)
        
        self.mgr = AccountManager()
        self.mgr.autoload_cookie_files()
        
        # Fallback từ cookie cũ
        if not self.mgr.accounts and COOKIE_FILE.exists():
            c = COOKIE_FILE.read_text(encoding="utf-8").strip()
            if c:
                self.mgr.add_or_update_with_cookies("default", c)
                
        self._busy = False
        self._stop = False
        self._images = []
        self._grid_count = 0
        self._total = 0
        
        self._build_ui()
        self.refresh_account_list()
        self._poll_status()
        
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        
        # Create Tab Control
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, **pad)
        
        # Create Tab Frames
        self.tab_gen = ttk.Frame(self.notebook)
        self.tab_acc = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_gen, text="🎨 Tạo ảnh song song")
        self.notebook.add(self.tab_acc, text="🔑 Quản lý tài khoản")
        
        self._build_gen_tab()
        self._build_acc_tab()
        
        # Log frame ở dưới cùng
        log_frame = ttk.LabelFrame(self.root, text="Nhật ký hệ thống")
        log_frame.pack(fill="x", side="bottom", **pad)
        self.log = tk.Text(log_frame, height=6, bg="#111", fg="#0f0", wrap="word")
        self.log.pack(fill="x", **pad)
        
    def _log(self, msg):
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.root.update_idletasks()
        
    def _build_gen_tab(self):
        pad = {"padx": 8, "pady": 5}

        # Bố cục 2 cột: trái = bảng điều khiển, phải = ảnh kết quả
        outer = ttk.Frame(self.tab_gen)
        outer.pack(fill="both", expand=True, **pad)

        left = ttk.Frame(outer, width=390)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        # ===== CỘT TRÁI: ĐIỀU KHIỂN =====
        # --- ① Mô tả ảnh (hỗ trợ NHIỀU prompt) ---
        pf = ttk.LabelFrame(left, text="① Mô tả ảnh (nhiều prompt cách nhau 1 dòng trống)")
        pf.pack(fill="x", **pad)
        self.prompt_text = tk.Text(pf, height=8, wrap="word", font=("Segoe UI", 10))
        self.prompt_text.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(pf, text="Mẹo: mỗi prompt 1 đoạn, ngăn cách bằng 2 lần Enter (dòng trống).",
                  foreground="#888", font=("Segoe UI", 8)).pack(anchor="w", padx=6)
        ttk.Button(pf, text="Xóa prompt",
                   command=lambda: self.prompt_text.delete("1.0", "end")).pack(anchor="e", padx=6, pady=(0, 6))

        # --- ② Tùy chọn ảnh ---
        of = ttk.LabelFrame(left, text="② Tùy chọn ảnh")
        of.pack(fill="x", **pad)
        grid = ttk.Frame(of); grid.pack(fill="x", padx=6, pady=6)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Model:").grid(row=0, column=0, sticky="w", pady=3)
        entries = load_model_entries()
        self.model_map = {e["label"]: e["code"] for e in entries}   # nhãn -> mã thật
        labels = list(self.model_map.keys())
        self.model_var = tk.StringVar(value=labels[0] if labels else "NARWHAL")
        self.model_combo = ttk.Combobox(grid, textvariable=self.model_var,
                                        values=labels, state="readonly")
        self.model_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(grid, text="Tỉ lệ khung:").grid(row=1, column=0, sticky="w", pady=3)
        self.aspect_var = tk.StringVar(value="16:9")
        ttk.Combobox(grid, textvariable=self.aspect_var, values=ASPECTS,
                     state="readonly").grid(row=1, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(grid, text="Số ảnh mỗi prompt:").grid(row=2, column=0, sticky="w", pady=3)
        self.n_var = tk.StringVar(value="x2")
        ttk.Combobox(grid, textvariable=self.n_var, values=COUNT_CHOICES,
                     state="readonly").grid(row=2, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(grid, text="Số luồng song song:").grid(row=3, column=0, sticky="w", pady=3)
        self.workers_var = tk.StringVar(value="x4")
        ttk.Combobox(grid, textvariable=self.workers_var, values=COUNT_CHOICES,
                     state="readonly").grid(row=3, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(grid, text="Số lần thử lại mỗi ảnh:").grid(row=4, column=0, sticky="w", pady=3)
        self.retries_var = tk.IntVar(value=3)
        ttk.Spinbox(grid, from_=0, to=10, textvariable=self.retries_var).grid(row=4, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(grid, text="Seed (trống = ngẫu nhiên):").grid(row=5, column=0, sticky="w", pady=3)
        self.seed_var = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.seed_var).grid(row=5, column=1, sticky="ew", padx=4, pady=3)

        # --- Nút hành động ---
        btnf = ttk.Frame(left); btnf.pack(fill="x", **pad)
        self.gen_btn = ttk.Button(btnf, text="▶  TẠO ẢNH", command=self.on_generate)
        self.gen_btn.pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=4)
        self.stop_btn = ttk.Button(btnf, text="■  DỪNG", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(4, 0), ipady=4)

        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(left, textvariable=self.status_var, foreground="#0a7",
                  wraplength=370, justify="left").pack(anchor="w", padx=8, pady=(2, 0))

        # ===== CỘT PHẢI: KẾT QUẢ =====
        head = ttk.Frame(right); head.pack(fill="x")
        ttk.Label(head, text="Ảnh kết quả", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(head, text="Mở thư mục", command=self._open_output_dir).pack(side="right", padx=4)
        ttk.Button(head, text="Log API", command=self._open_api_log).pack(side="right", padx=4)
        ttk.Button(head, text="Xóa kết quả", command=self._clear_results).pack(side="right", padx=4)

        imgf = ttk.Frame(right); imgf.pack(fill="both", expand=True, pady=(6, 0))
        self.canvas = tk.Canvas(imgf, bg="#222", highlightthickness=0)
        v_scroll = ttk.Scrollbar(imgf, orient="vertical", command=self.canvas.yview)
        self.img_frame = ttk.Frame(self.canvas)

        self.canvas.create_window((0, 0), window=self.img_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=v_scroll.set)

        v_scroll.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)

        self.img_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # Cuộn chuột chỉ khi con trỏ ở vùng ảnh
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _open_output_dir(self):
        out_dir = HERE / "output"
        out_dir.mkdir(exist_ok=True)
        try:
            os.startfile(str(out_dir))
        except Exception:
            messagebox.showinfo("Thư mục output", str(out_dir))

    def _open_api_log(self):
        log_file = HERE / "api_requests.log"
        if not log_file.exists():
            log_file.write_text("(Chưa có request nào. Hãy tạo ảnh trước.)\n", encoding="utf-8")
        try:
            os.startfile(str(log_file))
        except Exception:
            messagebox.showinfo("Log API", str(log_file))

    def _open_image(self, path):
        try:
            os.startfile(path)
        except Exception:
            pass

    def _clear_results(self):
        for w in self.img_frame.winfo_children():
            w.destroy()
        self._images.clear()
        self._grid_count = 0
        self.canvas.config(scrollregion=self.canvas.bbox("all"))
        
    def _build_acc_tab(self):
        pad = {"padx": 6, "pady": 4}
        
        # Chia cột trái (Danh sách acc) và cột phải (Form nạp/thêm)
        main_frame = ttk.Frame(self.tab_acc)
        main_frame.pack(fill="both", expand=True, **pad)
        
        left_frame = ttk.LabelFrame(main_frame, text="Danh sách tài khoản")
        left_frame.pack(side="left", fill="both", expand=True, **pad)
        
        right_frame = ttk.LabelFrame(main_frame, text="Nạp / Cập nhật tài khoản", width=350)
        right_frame.pack(side="right", fill="both", **pad)
        right_frame.pack_propagate(False) # Prevent frame from shrinking

        
        # --- Cột trái: Treeview hiển thị acc ---
        columns = ("Name", "Status", "Flows", "Token", "Uses", "Failures", "LoggedIn", "Cooldown")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="headings")
        self.tree.heading("Name", text="Tên")
        self.tree.heading("Status", text="Trạng thái")
        self.tree.heading("Flows", text="Luồng")
        self.tree.heading("Token", text="Token còn")
        self.tree.heading("Uses", text="Lượt dùng")
        self.tree.heading("Failures", text="Lượt lỗi")
        self.tree.heading("LoggedIn", text="Đã đăng nhập")
        self.tree.heading("Cooldown", text="Chờ (s)")
        
        self.tree.column("Name", width=110)
        self.tree.column("Status", width=100)
        self.tree.column("Flows", width=60, anchor="center")
        self.tree.column("Token", width=80, anchor="center")
        self.tree.column("Uses", width=70, anchor="center")
        self.tree.column("Failures", width=60, anchor="center")
        self.tree.column("LoggedIn", width=90, anchor="center")
        self.tree.column("Cooldown", width=70, anchor="center")
        
        self.tree.pack(fill="both", expand=True, **pad)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        
        # Nút điều khiển danh sách
        btn_row = ttk.Frame(left_frame); btn_row.pack(fill="x", **pad)
        ttk.Button(btn_row, text="Làm mới list", command=self.refresh_account_list).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Nạp từ cookies/", command=self.on_reload_cookies_dir).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Kiểm tra đăng nhập", command=self.on_check_selected_login).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Lấy tên model (Flow)", command=self.on_sniff_models).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Xoá tài khoản", command=self.on_delete_selected_account).pack(side="right", padx=4)
        
        # --- Cột phải: Form nhập ---
        f_row1 = ttk.Frame(right_frame); f_row1.pack(fill="x", **pad)
        ttk.Label(f_row1, text="Tên tài khoản:").pack(side="left")
        self.acc_name_var = tk.StringVar()
        ttk.Entry(f_row1, textvariable=self.acc_name_var).pack(side="left", fill="x", expand=True, padx=4)
        
        # Thiết lập tab cho nạp cookie và tự động đăng nhập
        self.acc_import_notebook = ttk.Notebook(right_frame)
        self.acc_import_notebook.pack(fill="both", expand=True, **pad)
        
        self.tab_cookie_input = ttk.Frame(self.acc_import_notebook)
        self.tab_gmail_login = ttk.Frame(self.acc_import_notebook)
        
        self.acc_import_notebook.add(self.tab_cookie_input, text="Nhập Cookie")
        self.acc_import_notebook.add(self.tab_gmail_login, text="Login Gmail")
        
        # --- Tab Nhập Cookie ---
        f_row2 = ttk.Frame(self.tab_cookie_input); f_row2.pack(fill="x", **pad)
        ttk.Label(f_row2, text="Tự lấy từ:").pack(side="left")
        self.browser_var = tk.StringVar(value="brave")
        ttk.Combobox(f_row2, textvariable=self.browser_var, values=["brave", "chrome", "edge"], width=10, state="readonly").pack(side="left", padx=4)
        ttk.Button(f_row2, text="Lấy cookie", command=self.on_grab_cookie_for_form).pack(side="left", padx=4)
        
        ttk.Label(self.tab_cookie_input, text="Dán cookie (Header Cookie / JSON / Netscape):").pack(anchor="w", **pad)
        self.cookie_text = tk.Text(self.tab_cookie_input, height=12, wrap="word")
        self.cookie_text.pack(fill="both", expand=True, **pad)
        
        ttk.Button(self.tab_cookie_input, text="NẠP TÀI KHOẢN", command=self.on_import_account_form).pack(fill="x", padx=6, pady=10)
        
        # --- Tab Đăng nhập Gmail ---
        g_row1 = ttk.Frame(self.tab_gmail_login); g_row1.pack(fill="x", **pad)
        ttk.Label(g_row1, text="Gmail Email:", width=12, anchor="w").pack(side="left")
        self.gmail_email_var = tk.StringVar()
        ttk.Entry(g_row1, textvariable=self.gmail_email_var).pack(side="left", fill="x", expand=True, padx=4)
        
        g_row2 = ttk.Frame(self.tab_gmail_login); g_row2.pack(fill="x", **pad)
        ttk.Label(g_row2, text="Mật khẩu:", width=12, anchor="w").pack(side="left")
        self.gmail_password_var = tk.StringVar()
        ttk.Entry(g_row2, textvariable=self.gmail_password_var, show="*").pack(side="left", fill="x", expand=True, padx=4)
        
        g_row3 = ttk.Frame(self.tab_gmail_login); g_row3.pack(fill="x", **pad)
        ttk.Label(g_row3, text="Email phụ:", width=12, anchor="w").pack(side="left")
        self.gmail_recovery_var = tk.StringVar()
        ttk.Entry(g_row3, textvariable=self.gmail_recovery_var).pack(side="left", fill="x", expand=True, padx=4)
        
        g_row4 = ttk.Frame(self.tab_gmail_login); g_row4.pack(fill="x", **pad)
        ttk.Label(g_row4, text="Trình duyệt:", width=12, anchor="w").pack(side="left")
        self.login_browser_var = tk.StringVar(value="chrome")
        ttk.Combobox(g_row4, textvariable=self.login_browser_var, values=["chrome", "brave", "edge"], width=10, state="readonly").pack(side="left", padx=4)
        
        ttk.Button(self.tab_gmail_login, text="TỰ ĐỘNG ĐĂNG NHẬP", command=self.on_auto_login).pack(fill="x", padx=6, pady=15)
        
    def refresh_account_list(self):
        states = self.mgr.states()
        alive_ids = {st["id"] for st in states}

        # Xoá những dòng không còn tồn tại
        for row in self.tree.get_children():
            if row not in alive_ids:
                self.tree.delete(row)

        # Cập nhật TẠI CHỖ (không xoá dòng đang chọn -> không mất selection)
        for st in states:
            logged = "🔑 OK" if st["logged_in"] else "❌"
            tl = st.get("token_left", 0)
            token_txt = f"{tl // 60}:{tl % 60:02d}" if tl > 0 else "-"
            flows = f"{st.get('inflight', 0)}/4"
            values = (st["name"], st["status"], flows, token_txt,
                      st["uses"], st["failures"], logged, st["cooldown"])
            if self.tree.exists(st["id"]):
                self.tree.item(st["id"], values=values)
            else:
                self.tree.insert("", "end", iid=st["id"], values=values)
            
    def on_reload_cookies_dir(self):
        n = self.mgr.autoload_cookie_files()
        self.refresh_account_list()
        self._log(f"Đã nạp {n} file cookie từ thư mục cookies/.")
        messagebox.showinfo("Nạp cookies", f"Đã nạp thành công {n} tài khoản từ cookies/.")
        
    def on_grab_cookie_for_form(self):
        browser = self.browser_var.get()
        try:
            self._log(f"Đang lấy cookie từ {browser}...")
            cookie = cg.grab_cookies(browser)
            self.cookie_text.delete("1.0", "end")
            self.cookie_text.insert("1.0", cookie)
            self._log(f"Đã lấy {len(cookie)} ký tự cookie.")
        except Exception as e:
            messagebox.showerror("Lỗi lấy cookie", f"Không lấy được cookie: {e}")
            
    def on_import_account_form(self):
        name = self.acc_name_var.get().strip()
        raw = self.cookie_text.get("1.0", "end").strip()
        if not name:
            messagebox.showwarning("Thiếu tên", "Vui lòng nhập tên tài khoản.")
            return
        if not raw:
            messagebox.showwarning("Thiếu cookie", "Vui lòng dán hoặc lấy cookie trước.")
            return
            
        res = self.mgr.add_or_update_with_cookies(name, raw)
        self.refresh_account_list()
        if res.get("ok"):
            flag = "Thành công" if res.get("has_session") else "Thiếu session-token"
            self._log(f"Đã nạp {res.get('count')} cookie cho '{name}'. Session: {res.get('has_session')}")
            messagebox.showinfo("Nạp tài khoản", f"{flag}! Đã nạp {res.get('count')} cookie.")
            self.cookie_text.delete("1.0", "end")
            self.acc_name_var.set("")
        else:
            messagebox.showerror("Lỗi nạp", f"Lỗi: {res.get('error')}")
            
    def on_delete_selected_account(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Chọn tài khoản", "Vui lòng chọn tài khoản cần xóa trong bảng.")
            return
        acc_id = sel[0]
        if messagebox.askyesno("Xóa tài khoản", f"Bạn có chắc muốn xóa tài khoản ID: {acc_id}?"):
            self.mgr.delete_account(acc_id)
            self.refresh_account_list()
            self._log(f"Đã xóa tài khoản {acc_id}.")
            
    def on_check_selected_login(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Chọn tài khoản", "Vui lòng chọn tài khoản cần kiểm tra.")
            return
        acc_id = sel[0]
        self._log(f"Đang kiểm tra đăng nhập cho {acc_id}...")
        
        def run():
            ok = self.mgr.check_login(acc_id)
            self.root.after(0, self.refresh_account_list)
            if ok:
                self.root.after(0, lambda: messagebox.showinfo("Kiểm tra", f"Tài khoản {acc_id} ĐĂNG NHẬP THÀNH CÔNG!"))
                self._log(f"Tài khoản {acc_id} Đăng nhập OK.")
            else:
                self.root.after(0, lambda: messagebox.showerror("Kiểm tra", f"Tài khoản {acc_id} kiểm tra thất bại. Hãy cập nhật cookie mới!"))
                self._log(f"Tài khoản {acc_id} lỗi đăng nhập.")
        
        threading.Thread(target=run, daemon=True).start()
    def on_sniff_models(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Chọn tài khoản", "Vui lòng chọn tài khoản đã đăng nhập.")
            return
        acc_id = sel[0]
        messagebox.showinfo(
            "Lấy tên model",
            "Cửa sổ Flow sẽ mở và tool TỰ ĐỘNG đọc danh sách model — "
            "bạn KHÔNG cần tạo ảnh.\n\n"
            "Cứ để cửa sổ đó tự nạp, tool sẽ tự dừng khi lấy xong và thêm "
            "model vào danh sách. Mã model cũng được ghi vào flow.log / sniff_requests.log.")
        self._log(f"Đang dò tên model thật từ Flow cho {acc_id}...")

        def run():
            models = self.mgr.sniff_models(acc_id, max_wait=240)

            def done():
                if models:
                    # Gộp model mới (dạng {code,label}) vào danh sách hiện có và lưu lại
                    entries = load_model_entries()
                    existing = {e["code"] for e in entries}
                    for m in models:
                        code = m.get("code") if isinstance(m, dict) else m
                        label = (m.get("label") if isinstance(m, dict) else m) or code
                        if code and code not in existing:
                            entries.append({"code": code, "label": label})
                            existing.add(code)
                    save_model_entries(entries)

                    # Dựng lại map + combo theo nhãn
                    self.model_map = {e["label"]: e["code"] for e in entries}
                    labels = list(self.model_map.keys())
                    self.model_combo["values"] = labels
                    if labels:
                        self.model_var.set(labels[0])

                    names = [f"{e['label']} ({e['code']})" for e in entries]
                    self._log("Đã cập nhật model ảnh: " + ", ".join(names))
                    messagebox.showinfo(
                        "Lấy tên model",
                        "Model ảnh đã lấy được:\n" + "\n".join(names) +
                        "\n\nDropdown hiện tên, khi tạo ảnh sẽ gửi đúng mã.")
                else:
                    self._log("Chưa bắt được model ảnh nào. Xem flow.log / sniff_requests.log.")
                    messagebox.showwarning(
                        "Lấy tên model",
                        "Chưa lấy được model ảnh. Hãy chắc chắn cửa sổ Flow đã đăng nhập "
                        "và mở được tab Hình ảnh. Chi tiết trong sniff_requests.log.")
            self.root.after(0, done)

        threading.Thread(target=run, daemon=True).start()


        
    def on_tree_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        acc_id = sel[0]
        acc = self.mgr.get(acc_id)
        if acc:
            self.acc_name_var.set(acc.name)
            self.gmail_email_var.set(acc.email)

    def on_auto_login(self):
        name = self.acc_name_var.get().strip()
        email = self.gmail_email_var.get().strip()
        password = self.gmail_password_var.get().strip()
        recovery = self.gmail_recovery_var.get().strip()
        browser = self.login_browser_var.get()
        
        if not name:
            messagebox.showwarning("Thiếu tên", "Vui lòng nhập tên tài khoản.")
            return
        if not email or not password:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng điền đầy đủ Email và Mật khẩu Gmail.")
            return
            
        acc = self.mgr.get(name)
        if not acc:
            acc = self.mgr.add_account(name=name, browser=browser, mode="manual")
        if not acc:
            messagebox.showerror("Lỗi", f"Không tạo được tài khoản '{name}'.")
            return

        acc.browser = browser
        acc.email = email
        self.mgr.save()
        
        self.refresh_account_list()
        self._log(f"Bắt đầu tự động đăng nhập cho {name}... "
                  f"(Nếu gặp 2FA/captcha, hãy tự thao tác trong cửa sổ trình duyệt vừa mở)")
        
        def run():
            ok = self.mgr.auto_login(acc.id, email, password, recovery or None)
            self.root.after(0, self.refresh_account_list)
            if ok:
                self.root.after(0, lambda: messagebox.showinfo("Auto Login", f"Tài khoản {name} ĐĂNG NHẬP THÀNH CÔNG!"))
                self._log(f"Tài khoản {name} Auto Login thành công.")
            else:
                self.root.after(0, lambda: messagebox.showerror("Auto Login", f"Đăng nhập thất bại. Xem logs hoặc kiểm tra lại thông tin!"))
                self._log(f"Tài khoản {name} Auto Login thất bại.")
                
        threading.Thread(target=run, daemon=True).start()

    def _poll_status(self):
        try:
            self.refresh_account_list()
        except Exception:
            pass
        self.root.after(1500, self._poll_status)
        
    def on_generate(self):
        if self._busy:
            return
        prompt = self.prompt_text.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("Thiếu prompt", "Vui lòng nhập mô tả ảnh.")
            return

        healthy = self.mgr.healthy_accounts()
        if not healthy:
            total = len(self.mgr.accounts)
            logged = sum(1 for a in self.mgr.accounts if a.logged_in)
            if total == 0:
                msg = "Chưa có tài khoản nào. Hãy nạp/đăng nhập ở tab Tài khoản."
            elif logged == 0:
                msg = ("Có tài khoản nhưng chưa lấy được phiên Flow "
                       "(thiếu cookie __Secure-next-auth.session-token).\n"
                       "Hãy đăng nhập lại (Login Gmail) hoặc bấm 'Kiểm tra đăng nhập' "
                       "sau khi đã vào được Flow trong trình duyệt.")
            else:
                msg = "Tài khoản đang trong thời gian chờ (cooldown). Thử lại sau."
            messagebox.showwarning("Không có tài khoản", msg)
            return

        n = parse_count(self.n_var.get(), 1)
        workers = parse_count(self.workers_var.get(), 1)
        retries = max(0, int(self.retries_var.get()))
        seed = self.seed_var.get().strip()
        seed = int(seed) if seed.lstrip("-").isdigit() else None

        # Tách NHIỀU prompt: ngăn cách bằng 1 dòng trống (>=2 lần Enter)
        raw = self.prompt_text.get("1.0", "end").strip()
        prompts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
        if not prompts:
            messagebox.showwarning("Thiếu prompt", "Vui lòng nhập mô tả ảnh.")
            return

        self._busy = True
        self._stop = False
        self.gen_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        self._log(f"Bắt đầu vẽ {len(prompts)} prompt × {n} ảnh, retry tối đa {retries} lần/ảnh, "
                  f"{workers} luồng.")
        threading.Thread(target=self._generate_worker,
                         args=(prompts, n, workers, seed, retries), daemon=True).start()

    def on_stop(self):
        if self._busy:
            self._stop = True
            self.status_var.set("Đang dừng...")
            self._log("Đã yêu cầu DỪNG. Sẽ dừng sau prompt hiện tại.")
            self.stop_btn.config(state="disabled")

    def _generate_worker(self, prompts, n, workers, seed, retries=3):
        try:
            multi = MultiFlow(self.mgr, max_workers=workers)
            out_dir = HERE / "output"
            out_dir.mkdir(exist_ok=True)
            model = self.model_map.get(self.model_var.get(), self.model_var.get()) or "NARWHAL"
            aspect = ASPECT_MAP[self.aspect_var.get()]

            total_prompts = len(prompts)
            for idx, prompt in enumerate(prompts, 1):
                if self._stop:
                    break
                self.root.after(0, lambda i=idx, t=total_prompts, p=prompt:
                                self.status_var.set(f"Prompt {i}/{t}: {p[:40]}..."))
                try:
                    images_bytes = multi.generate(
                        prompt=prompt,
                        model=model,
                        aspect=aspect,
                        n=n,
                        seed=seed,
                        retries=retries,
                        log=self._log,
                    )
                except Exception as e:
                    err = str(e)
                    self._log(f"[LỖI prompt {idx}/{total_prompts}] {err}")
                    continue

                paths = []
                for i, img_bytes in enumerate(images_bytes):
                    ext = "png"
                    if HAS_PIL:
                        try:
                            import io
                            fmt = Image.open(io.BytesIO(img_bytes)).format
                            ext = {"JPEG": "jpg", "WEBP": "webp", "PNG": "png"}.get(fmt, (fmt or "png").lower())
                        except Exception:
                            pass
                    fname = out_dir / f"flow_{int(time.time())}_{idx}_{i}_{random.randint(1000, 9999)}.{ext}"
                    fname.write_bytes(img_bytes)
                    paths.append(str(fname))

                self._total += len(paths)
                self._log(f"Prompt {idx}/{total_prompts}: vẽ thành công {len(paths)} ảnh "
                          f"(tổng {self._total}).")
                self.root.after(0, lambda p=list(paths): self._show_images(p, append=True))
        finally:
            self._busy = False
            self.root.after(0, self._on_worker_done)

    def _on_worker_done(self):
        self.gen_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(f"Hoàn tất. Tổng đã tạo trong phiên: {self._total} ảnh.")
            
    def _show_images(self, paths, append=False):
        if not append:
            for w in self.img_frame.winfo_children():
                w.destroy()
            self._images.clear()
            self._grid_count = 0

        if not paths and self._grid_count == 0:
            ttk.Label(self.img_frame, text="Không có ảnh nào được trả về.").grid(row=0, column=0, padx=10, pady=10)
            self.img_frame.update_idletasks()
            self.canvas.config(scrollregion=self.canvas.bbox("all"))
            return

        max_cols = 4
        for p in paths:
            try:
                if HAS_PIL:
                    # Pillow đọc được PNG/JPEG/WebP... và tự resize
                    im = Image.open(p)
                    im.thumbnail((240, 240))
                    img = ImageTk.PhotoImage(im)
                else:
                    img = tk.PhotoImage(file=p)
                    factor = max(1, img.width() // 220)
                    if factor > 1:
                        img = img.subsample(factor, factor)
                self._images.append(img)

                idx = self._grid_count
                cell = ttk.Frame(self.img_frame)
                cell.grid(row=idx // max_cols, column=idx % max_cols, padx=6, pady=6)

                lbl = tk.Label(cell, image=img, bg="#111", cursor="hand2")
                lbl.pack()
                lbl.bind("<Button-1>", lambda e, path=p: self._open_image(path))
                ttk.Label(cell, text=pathlib.Path(p).name, font=("Segoe UI", 8)).pack()
                self._grid_count += 1
            except Exception as e:
                self._log(f"Lỗi hiển thị {p}: {e}")

        if self._grid_count == 0:
            hint = ("Đã lưu ảnh vào thư mục output/ nhưng không hiển thị được."
                    + ("" if HAS_PIL else " Cài Pillow để xem mọi định dạng: pip install pillow"))
            ttk.Label(self.img_frame, text=hint, wraplength=600).grid(row=0, column=0, padx=10, pady=10)

        self.img_frame.update_idletasks()
        self.canvas.config(scrollregion=self.canvas.bbox("all"))
        
    def on_close(self):
        try:
            self.mgr.stop_all()
        except Exception:
            pass
        self.root.destroy()


def main_legacy():
    """Giao diện cũ (tkinter/ttk) - giữ làm dự phòng."""
    root = tk.Tk()
    app = FlowGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


def main():
    """Mặc định mở GIAO DIỆN MỚI (CustomTkinter). Nếu lỗi -> quay về bản cũ."""
    try:
        from gui_modern import main as modern_main
        modern_main()
    except Exception as e:
        print(f"[gui] Không mở được giao diện mới ({e}); dùng bản cũ.")
        main_legacy()


if __name__ == "__main__":
    main()
