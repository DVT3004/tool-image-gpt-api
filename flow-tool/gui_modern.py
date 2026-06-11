# -*- coding: utf-8 -*-
"""
Flow Multi-Account Image Tool - Giao diện hiện đại (CustomTkinter, dark theme).
Tái dùng backend AccountManager + MultiFlow của bản tkinter gốc (gui.py).
"""

import os
import re
import io
import time
import random
import threading
import pathlib

import customtkinter as ctk
from tkinter import messagebox, filedialog

from PIL import Image

from flow_accounts import AccountManager
from flow_multi import MultiFlow
import flow_multi
import cookie_grabber as cg

# Cấu hình/model loader dùng chung (ASPECTS, model map, parse_count...)
from flow_config import (ASPECTS, ASPECT_MAP, COUNT_CHOICES, WORKER_CHOICES, parse_count,
                         load_model_entries, save_model_entries)

HERE = pathlib.Path(__file__).parent
COOKIE_FILE = HERE / "flow_cookie.txt"
OUT_DIR = HERE / "output"
OUT_DIR.mkdir(exist_ok=True)

# ----- Bảng màu (theo mockup) -----
BG        = "#0f1117"   # nền tổng
PANEL     = "#171a23"   # khung panel
CARD      = "#1d212c"   # thẻ
CARD2     = "#232838"   # thẻ nhạt hơn / placeholder
STROKE    = "#2a2f3d"   # viền
ACCENT    = "#7c5cff"   # tím nhấn
ACCENT_HV = "#6a4ce0"
TEXT      = "#e6e8ef"
MUTED     = "#8b90a0"
GREEN     = "#39d98a"
YELLOW    = "#f5c451"
RED       = "#ff6b6b"
GRAY      = "#5b6070"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Flow Multi-Account Image Tool")
        # Kích thước tự vừa màn hình (trừ taskbar) -> không bị cắt phần dưới.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1480, sw - 40)
        h = min(900, sh - 70)
        self.geometry(f"{w}x{h}+{(sw - w)//2}+10")
        self.minsize(1120, 680)
        self.configure(fg_color=BG)

        # ----- backend -----
        self.mgr = AccountManager()
        try:
            self.mgr.autoload_cookie_files()
        except Exception:
            pass
        if not self.mgr.accounts and COOKIE_FILE.exists():
            c = COOKIE_FILE.read_text(encoding="utf-8").strip()
            if c:
                self.mgr.add_or_update_with_cookies("default", c)

        # ----- state -----
        self._busy = False
        self._stop = False
        self._closing = False
        self._manual_active = False      # đang chạy chế độ "log tay" hay không
        self._manual_acc_id = None
        self._edit_image_path = None     # ảnh nguồn cho chế độ Sửa ảnh / img2img
        self._poll_id = None
        self._imgrefs = []     # giữ tham chiếu CTkImage để không bị GC
        self._slots = []       # các ô ảnh trong lưới
        self._slot_idx = 0
        self._done = 0
        self._target = 0
        self.model_map = {}

        self._build_header()
        self._build_body()
        self.show_page("gen")
        self._poll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ============================================================ HEADER
    def _build_header(self):
        bar = ctk.CTkFrame(self, height=52, fg_color=PANEL, corner_radius=0)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        ctk.CTkLabel(bar, text="  ◆  Flow Multi-Account Image Tool",
                     font=ctk.CTkFont(size=17, weight="bold"),
                     text_color=TEXT).pack(side="left", padx=18)

        nav = ctk.CTkFrame(bar, fg_color="transparent")
        nav.pack(side="left", padx=24)
        self._nav_btns = {}
        for key, label, icon in [("gen", "Tạo ảnh", "🖼"), ("acc", "Tài khoản", "👤"),
                                 ("settings", "Cài đặt", "⚙"), ("stats", "Thống kê", "📊"),
                                 ("logs", "Nhật ký", "📄")]:
            b = ctk.CTkButton(nav, text=f"{icon}  {label}", width=110, height=34,
                              corner_radius=8, fg_color="transparent",
                              hover_color=CARD, text_color=MUTED,
                              font=ctk.CTkFont(size=13),
                              command=lambda k=key: self.show_page(k))
            b.pack(side="left", padx=3)
            self._nav_btns[key] = b

        ctk.CTkButton(bar, text="?", width=36, height=34, corner_radius=8,
                      fg_color="transparent", hover_color=CARD, text_color=MUTED,
                      command=lambda: self.show_page("logs")).pack(side="right", padx=(2, 12))
        ctk.CTkButton(bar, text="⚙", width=36, height=34, corner_radius=8,
                      fg_color="transparent", hover_color=CARD, text_color=MUTED,
                      command=lambda: self.show_page("settings")).pack(side="right", padx=2)

    def _set_active_nav(self, key):
        for k, b in self._nav_btns.items():
            if k == key:
                b.configure(fg_color=CARD, text_color=TEXT)
            else:
                b.configure(fg_color="transparent", text_color=MUTED)

    # ============================================================ BODY
    def _build_body(self):
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=12, pady=8)
        self.pages = {}
        self.pages["gen"] = self._build_gen_page()
        self.pages["acc"] = self._build_acc_page()
        self.pages["settings"] = self._build_settings_page()
        self.pages["stats"] = self._build_stats_page()
        self.pages["logs"] = self._build_logs_page()

    def show_page(self, key):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self._set_active_nav(key)

    # ------------------------------------------------------------ GEN PAGE
    def _build_gen_page(self):
        page = ctk.CTkFrame(self.body, fg_color="transparent")

        # ===== Cột trái: điều khiển =====
        left = ctk.CTkFrame(page, width=380, fg_color="transparent")
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        # ① Prompt
        c1 = ctk.CTkFrame(left, fg_color=PANEL, corner_radius=14)
        c1.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(c1, text="①  Mô tả ảnh (nhiều prompt cách nhau 1 dòng trống)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=16, pady=(12, 6))
        self.prompt_box = ctk.CTkTextbox(c1, height=120, corner_radius=10,
                                         fg_color=CARD, border_color=STROKE, border_width=1,
                                         font=ctk.CTkFont(size=13), text_color=TEXT)
        self.prompt_box.pack(fill="x", padx=16)
        ctk.CTkLabel(c1, text="Mẹo: mỗi prompt 1 đoạn, ngăn cách bằng 2 lần Enter (dòng trống).",
                     font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w", padx=16, pady=(6, 0))
        ctk.CTkButton(c1, text="🗑  Xóa prompt", width=120, height=28, corner_radius=8,
                      fg_color=CARD, hover_color=STROKE, text_color=MUTED,
                      command=lambda: self.prompt_box.delete("1.0", "end")).pack(anchor="e", padx=16, pady=10)

        # ② Tùy chọn
        c2 = ctk.CTkFrame(left, fg_color=PANEL, corner_radius=14)
        c2.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(c2, text="②  Tùy chọn ảnh", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=16, pady=(12, 6))
        grid = ctk.CTkFrame(c2, fg_color="transparent")
        grid.pack(fill="x", padx=16, pady=(0, 12))
        grid.columnconfigure(1, weight=1)

        entries = load_model_entries()
        self.model_map = {e["label"]: e["code"] for e in entries}
        labels = list(self.model_map.keys()) or ["NARWHAL"]

        def row(r, label, widget):
            ctk.CTkLabel(grid, text=label, font=ctk.CTkFont(size=12),
                         text_color=MUTED, anchor="w").grid(row=r, column=0, sticky="w", pady=5, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="ew", pady=5)

        self.model_var = ctk.StringVar(value=labels[0])
        row(0, "🧩  Model", ctk.CTkOptionMenu(grid, variable=self.model_var, values=labels,
                                              fg_color=CARD, button_color=ACCENT,
                                              button_hover_color=ACCENT_HV, corner_radius=8))
        self.aspect_var = ctk.StringVar(value="16:9")
        row(1, "🔲  Tỉ lệ khung", ctk.CTkOptionMenu(grid, variable=self.aspect_var, values=ASPECTS,
                                                    fg_color=CARD, button_color=ACCENT,
                                                    button_hover_color=ACCENT_HV, corner_radius=8))
        self.n_var = ctk.StringVar(value="x2")
        row(2, "🔢  Số ảnh mỗi prompt", ctk.CTkOptionMenu(grid, variable=self.n_var, values=COUNT_CHOICES,
                                                          fg_color=CARD, button_color=ACCENT,
                                                          button_hover_color=ACCENT_HV, corner_radius=8))
        self.workers_var = ctk.StringVar(value="x4")
        row(3, "⚡  Số luồng song song", ctk.CTkOptionMenu(grid, variable=self.workers_var, values=WORKER_CHOICES,
                                                          fg_color=CARD, button_color=ACCENT,
                                                          button_hover_color=ACCENT_HV, corner_radius=8))
        self.retries_var = ctk.StringVar(value="3")
        row(4, "🔁  Số lần thử lại mỗi ảnh", ctk.CTkOptionMenu(grid, variable=self.retries_var,
                                                              values=["0", "1", "2", "3", "5"],
                                                              fg_color=CARD, button_color=ACCENT,
                                                              button_hover_color=ACCENT_HV, corner_radius=8))
        self.seed_entry = ctk.CTkEntry(grid, placeholder_text="Để trống để random",
                                       fg_color=CARD, border_color=STROKE, corner_radius=8)
        row(5, "🎲  Seed (trống = ngẫu nhiên)", self.seed_entry)

        # Ô tick "Log tay": mở Chromium cho bạn tự thao tác + tool bắt request
        self.manual_var = ctk.BooleanVar(value=False)
        self.manual_chk = ctk.CTkCheckBox(
            c2, text="🖐  Log tay (mở Chromium, tự thao tác — tool bắt request)",
            variable=self.manual_var, onvalue=True, offvalue=False,
            font=ctk.CTkFont(size=12), text_color=TEXT,
            fg_color=ACCENT, hover_color=ACCENT_HV, command=self._on_manual_toggle)
        self.manual_chk.pack(anchor="w", padx=16, pady=(0, 12))

        # Ảnh ĐƯA VÀO để tạo ảnh mới dựa trên nó (img2img).
        ctk.CTkLabel(c2, text="🖼  Ảnh đưa vào (img2img)",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=16, pady=(0, 4))
        edit_box = ctk.CTkFrame(c2, fg_color=CARD, corner_radius=10)
        edit_box.pack(fill="x", padx=16, pady=(0, 12))
        # Khung thumbnail bên trái
        self.edit_thumb = ctk.CTkLabel(edit_box, text="—", width=64, height=64,
                                       fg_color=PANEL, corner_radius=8,
                                       text_color=MUTED, font=ctk.CTkFont(size=11))
        self.edit_thumb.pack(side="left", padx=8, pady=8)
        right_box = ctk.CTkFrame(edit_box, fg_color="transparent")
        right_box.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)
        self.edit_img_label = ctk.CTkLabel(right_box, text="Chưa chọn ảnh\n(để trống = tạo ảnh mới)",
                                           font=ctk.CTkFont(size=11), text_color=MUTED,
                                           anchor="w", justify="left", wraplength=210)
        self.edit_img_label.pack(anchor="w")
        btn_row = ctk.CTkFrame(right_box, fg_color="transparent")
        btn_row.pack(anchor="w", pady=(6, 0))
        ctk.CTkButton(btn_row, text="Chọn ảnh", height=28, width=88, corner_radius=8,
                      fg_color=ACCENT, hover_color=ACCENT_HV,
                      command=self._choose_edit_image).pack(side="left")
        ctk.CTkButton(btn_row, text="Xoá", height=28, width=52, corner_radius=8,
                      fg_color=PANEL, hover_color=RED, text_color=MUTED,
                      command=self._clear_edit_image).pack(side="left", padx=(6, 0))

        # Nút hành động
        act = ctk.CTkFrame(left, fg_color="transparent")
        act.pack(fill="x")
        self.gen_btn = ctk.CTkButton(act, text="▶   TẠO ẢNH", height=44, corner_radius=12,
                                     fg_color=ACCENT, hover_color=ACCENT_HV,
                                     font=ctk.CTkFont(size=15, weight="bold"),
                                     command=self.on_generate)
        self.gen_btn.pack(fill="x", pady=(0, 8))
        row2 = ctk.CTkFrame(act, fg_color="transparent"); row2.pack(fill="x")
        self.stop_btn = ctk.CTkButton(row2, text="⏹  DỪNG", height=40, corner_radius=10,
                                      fg_color=CARD, hover_color=RED, text_color=TEXT,
                                      state="disabled", command=self.on_stop)
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        row3 = ctk.CTkFrame(act, fg_color="transparent"); row3.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(row3, text="📁  Mở thư mục", height=38, corner_radius=10,
                      fg_color=CARD, hover_color=STROKE, text_color=TEXT,
                      command=self._open_output).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(row3, text="🧹  Xóa kết quả", height=38, corner_radius=10,
                      fg_color=CARD, hover_color=STROKE, text_color=TEXT,
                      command=self._clear_results).pack(side="left", expand=True, fill="x", padx=(4, 0))

        # ===== Cột phải: kết quả =====
        right = ctk.CTkFrame(page, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x")
        self.result_title = ctk.CTkLabel(head, text="Ảnh kết quả (0 / 0)",
                                         font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT)
        self.result_title.pack(side="left")
        self.pct_label = ctk.CTkLabel(head, text="0%", font=ctk.CTkFont(size=13, weight="bold"),
                                      text_color=ACCENT)
        self.pct_label.pack(side="right")
        self.progress = ctk.CTkProgressBar(head, height=10, corner_radius=6,
                                           progress_color=ACCENT, fg_color=CARD)
        self.progress.pack(side="right", fill="x", expand=True, padx=14)
        self.progress.set(0)

        self.grid_frame = ctk.CTkScrollableFrame(right, fg_color="transparent")
        self.grid_frame.pack(fill="both", expand=True, pady=(12, 0))
        for c in range(4):
            self.grid_frame.columnconfigure(c, weight=1)

        # Hàng trạng thái tài khoản
        self.acc_row = ctk.CTkFrame(right, height=72, fg_color=PANEL, corner_radius=12)
        self.acc_row.pack(fill="x", pady=(10, 0))
        self.acc_row.pack_propagate(False)
        self.acc_chip_wrap = ctk.CTkFrame(self.acc_row, fg_color="transparent")
        self.acc_chip_wrap.pack(side="left", fill="both", expand=True, padx=10, pady=8)
        self.total_label = ctk.CTkLabel(self.acc_row, text="Tổng tiến trình\n0 / 0 ảnh",
                                        font=ctk.CTkFont(size=12, weight="bold"), text_color=TEXT,
                                        justify="right")
        self.total_label.pack(side="right", padx=18)

        # Log mini dưới cùng
        logc = ctk.CTkFrame(right, height=140, fg_color=PANEL, corner_radius=12)
        logc.pack(fill="x", pady=(10, 0))
        logc.pack_propagate(False)
        lh = ctk.CTkFrame(logc, fg_color="transparent"); lh.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(lh, text="Nhật ký hệ thống", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkButton(lh, text="Xóa log", width=70, height=26, corner_radius=8,
                      fg_color=CARD, hover_color=STROKE, text_color=MUTED,
                      command=self._clear_log).pack(side="right", padx=(6, 0))
        ctk.CTkButton(lh, text="Log API", width=70, height=26, corner_radius=8,
                      fg_color=CARD, hover_color=STROKE, text_color=MUTED,
                      command=self._open_api_log).pack(side="right")
        ctk.CTkButton(lh, text="📄 Log tay", width=80, height=26, corner_radius=8,
                      fg_color=CARD, hover_color=STROKE, text_color=MUTED,
                      command=self._open_manual_log).pack(side="right", padx=(0, 6))
        self.log_box = ctk.CTkTextbox(logc, fg_color="#0c0e14", text_color="#cdd2e0",
                                      font=ctk.CTkFont(family="Consolas", size=12), corner_radius=8)
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)

        return page

    # ------------------------------------------------------------ ACC PAGE
    def _build_acc_page(self):
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        bar = ctk.CTkFrame(page, fg_color="transparent"); bar.pack(fill="x")
        ctk.CTkLabel(bar, text="Quản lý tài khoản", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=TEXT).pack(side="left", pady=(0, 8))
        for txt, cmd in [("↻ Làm mới", self._refresh_accounts),
                         ("📂 Nạp cookies/", self._reload_cookies_dir),
                         ("✔ Kiểm tra đăng nhập", self._check_login),
                         ("🧠 Lấy model (Flow)", self._sniff_models),
                         ("🗑 Xóa", self._delete_account)]:
            ctk.CTkButton(bar, text=txt, height=34, corner_radius=8, fg_color=CARD,
                          hover_color=ACCENT_HV, text_color=TEXT,
                          command=cmd).pack(side="right", padx=4)

        self.acc_list = ctk.CTkScrollableFrame(page, fg_color=PANEL, corner_radius=12)
        self.acc_list.pack(fill="both", expand=True, pady=10)
        self._acc_selected = None

        # Tên tài khoản (dùng chung cho cả nạp cookie & đăng nhập Gmail)
        topr = ctk.CTkFrame(page, fg_color="transparent"); topr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(topr, text="Tên tài khoản:", text_color=MUTED, width=110, anchor="w").pack(side="left")
        self.acc_name = ctk.CTkEntry(topr, fg_color=CARD, border_color=STROKE,
                                     placeholder_text="vd: acc1")
        self.acc_name.pack(side="left", fill="x", expand=True, padx=6)

        forms = ctk.CTkFrame(page, fg_color="transparent"); forms.pack(fill="x")

        # --- Card 1: nạp bằng cookie ---
        fc = ctk.CTkFrame(forms, fg_color=PANEL, corner_radius=12)
        fc.pack(side="left", fill="both", expand=True, padx=(0, 6))
        ctk.CTkLabel(fc, text="Nạp bằng Cookie", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=14, pady=(10, 6))
        r = ctk.CTkFrame(fc, fg_color="transparent"); r.pack(fill="x", padx=14)
        self.browser_var = ctk.StringVar(value="chrome")
        ctk.CTkOptionMenu(r, variable=self.browser_var, values=["chrome", "brave", "edge", "coccoc"],
                          width=110, fg_color=CARD, button_color=ACCENT).pack(side="left")
        ctk.CTkButton(r, text="Lấy cookie từ trình duyệt", height=30, corner_radius=8,
                      fg_color=CARD, hover_color=ACCENT_HV,
                      command=self._grab_cookie).pack(side="left", padx=6)
        self.cookie_box = ctk.CTkTextbox(fc, height=80, fg_color=CARD, corner_radius=8)
        self.cookie_box.pack(fill="x", padx=14, pady=6)
        ctk.CTkButton(fc, text="NẠP TÀI KHOẢN", height=36, corner_radius=10,
                      fg_color=ACCENT, hover_color=ACCENT_HV,
                      command=self._import_account).pack(fill="x", padx=14, pady=(0, 12))

        # --- Card 2: đăng nhập Gmail ---
        gc = ctk.CTkFrame(forms, fg_color=PANEL, corner_radius=12)
        gc.pack(side="left", fill="both", expand=True, padx=(6, 0))
        ctk.CTkLabel(gc, text="Đăng nhập Gmail", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=14, pady=(10, 6))
        self.gmail_email = ctk.CTkEntry(gc, fg_color=CARD, border_color=STROKE,
                                        placeholder_text="Email Gmail")
        self.gmail_email.pack(fill="x", padx=14, pady=3)
        self.gmail_pw = ctk.CTkEntry(gc, fg_color=CARD, border_color=STROKE, show="*",
                                     placeholder_text="Mật khẩu")
        self.gmail_pw.pack(fill="x", padx=14, pady=3)
        gr = ctk.CTkFrame(gc, fg_color="transparent"); gr.pack(fill="x", padx=14, pady=3)
        self.gmail_rec = ctk.CTkEntry(gr, fg_color=CARD, border_color=STROKE,
                                      placeholder_text="Email phụ (nếu có)")
        self.gmail_rec.pack(side="left", fill="x", expand=True)
        self.login_browser_var = ctk.StringVar(value="chrome")
        ctk.CTkOptionMenu(gr, variable=self.login_browser_var, values=["chrome", "brave", "edge", "coccoc"],
                          width=100, fg_color=CARD, button_color=ACCENT).pack(side="left", padx=(6, 0))
        br = ctk.CTkFrame(gc, fg_color="transparent"); br.pack(fill="x", padx=14, pady=(4, 12))
        ctk.CTkButton(br, text="TỰ ĐỘNG ĐĂNG NHẬP", height=36, corner_radius=10,
                      fg_color=ACCENT, hover_color=ACCENT_HV,
                      command=self._auto_login).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(br, text="Mở trình duyệt (tay)", height=36, corner_radius=10,
                      fg_color=CARD, hover_color=ACCENT_HV, text_color=TEXT,
                      command=self._manual_login).pack(side="left", fill="x", expand=True, padx=(4, 0))
        return page

    def _auto_login(self):
        name = self.acc_name.get().strip()
        email = self.gmail_email.get().strip()
        pw = self.gmail_pw.get().strip()
        rec = self.gmail_rec.get().strip()
        br = self.login_browser_var.get()
        if not name or not email or not pw:
            messagebox.showwarning("Thiếu thông tin", "Cần Tên tài khoản + Email + Mật khẩu.")
            return
        acc = self.mgr.get(name) or self.mgr.add_account(name=name, browser=br, mode="manual")
        if not acc:
            messagebox.showerror("Lỗi", f"Không tạo được tài khoản '{name}'.")
            return
        acc.browser = br; acc.email = email; self.mgr.save()
        self._refresh_accounts()
        self.log(f"Đang tự động đăng nhập {name}... (gặp 2FA/captcha hãy tự thao tác trong cửa sổ).")

        def run():
            ok = self.mgr.auto_login(acc.id, email, pw, rec or None)
            self.after(0, self._refresh_accounts)
            self.after(0, lambda: self.log(
                f"{name}: {'ĐĂNG NHẬP OK' if ok else 'đăng nhập thất bại'}",
                "SUCCESS" if ok else "WARNING"))
        threading.Thread(target=run, daemon=True).start()

    def _manual_login(self):
        name = self.acc_name.get().strip()
        if not name:
            messagebox.showwarning("Thiếu", "Nhập tên tài khoản trước.")
            return
        acc = self.mgr.get(name) or self.mgr.add_account(
            name=name, browser=self.login_browser_var.get(), mode="manual")
        res = self.mgr.login_account(acc.id)
        self._refresh_accounts()
        self.log(res.get("message") or res.get("error", ""),
                 "INFO" if res.get("ok") else "WARNING")
        if res.get("ok"):
            messagebox.showinfo("Đăng nhập tay",
                                "Đăng nhập Google + vào Flow trong cửa sổ vừa mở. Xong rồi bấm "
                                "'Kiểm tra đăng nhập' để lưu cookie.")

    def _build_settings_page(self):
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        box = ctk.CTkScrollableFrame(page, fg_color=PANEL, corner_radius=14)
        box.pack(fill="both", expand=True)
        ctk.CTkLabel(box, text="Cài đặt", font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=24, pady=(20, 8))

        def section(title):
            ctk.CTkLabel(box, text=title, font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=ACCENT).pack(anchor="w", padx=24, pady=(14, 4))

        # --- Giao diện ---
        section("Giao diện")
        r = ctk.CTkFrame(box, fg_color="transparent"); r.pack(fill="x", padx=24, pady=4)
        ctk.CTkLabel(r, text="Chế độ màu:", text_color=MUTED, width=140, anchor="w").pack(side="left")
        self.theme_var = ctk.StringVar(value="Dark")
        ctk.CTkOptionMenu(r, variable=self.theme_var, values=["Dark", "Light", "System"],
                          width=160, fg_color=CARD, button_color=ACCENT,
                          command=lambda m: ctk.set_appearance_mode(m.lower())).pack(side="left")

        # --- Chạy ngầm ---
        section("Chạy ngầm")
        r = ctk.CTkFrame(box, fg_color="transparent"); r.pack(fill="x", padx=24, pady=4)
        self.hide_switch = ctk.CTkSwitch(
            r, text="Ẩn cửa sổ trình duyệt khi tạo ảnh (mở ngoài màn hình, không nhảy lên)",
            command=self._toggle_hide, progress_color=ACCENT)
        if getattr(self.mgr, "hide_browser", True):
            self.hide_switch.select()
        self.hide_switch.pack(side="left")

        # --- Ghi log API ---
        section("Nhật ký request API")
        r = ctk.CTkFrame(box, fg_color="transparent"); r.pack(fill="x", padx=24, pady=4)
        self.apilog_switch = ctk.CTkSwitch(r, text="Ghi log chi tiết request tạo ảnh (api_requests.log)",
                                           command=self._toggle_api_log, progress_color=ACCENT)
        if getattr(flow_multi, "API_LOG", True):
            self.apilog_switch.select()
        self.apilog_switch.pack(side="left")

        # --- Thư mục & tệp ---
        section("Thư mục & tệp")
        r = ctk.CTkFrame(box, fg_color="transparent"); r.pack(fill="x", padx=24, pady=4)
        for txt, path in [("📁 Ảnh đã tạo", OUT_DIR), ("🍪 Cookies", HERE / "cookies"),
                          ("🧩 models.json", HERE / "models.json"), ("📄 flow.log", HERE / "flow.log")]:
            ctk.CTkButton(r, text=txt, height=34, corner_radius=8, fg_color=CARD,
                          hover_color=ACCENT_HV, text_color=TEXT,
                          command=lambda p=path: self._open_path(p)).pack(side="left", padx=(0, 8))

        # --- Thông tin ---
        section("Thông tin")
        info = (f"• Tối đa luồng / tài khoản: 4\n"
                f"• Mỗi tài khoản mở trình duyệt riêng (profile riêng) để mint token.\n"
                f"• Model ảnh: Nano Banana Pro (GEM_PIX_2), Nano Banana 2 (NARWHAL).\n"
                f"• Thư mục làm việc: {HERE}")
        ctk.CTkLabel(box, text=info, font=ctk.CTkFont(size=12), text_color=MUTED,
                     justify="left").pack(anchor="w", padx=24, pady=(4, 20))
        return page

    def _toggle_api_log(self):
        flow_multi.API_LOG = bool(self.apilog_switch.get())
        self.log(f"Ghi log API: {'BẬT' if flow_multi.API_LOG else 'TẮT'}")

    def _toggle_hide(self):
        self.mgr.hide_browser = bool(self.hide_switch.get())
        self.log(f"Chạy ngầm (ẩn trình duyệt): {'BẬT' if self.mgr.hide_browser else 'TẮT'}. "
                 f"Có hiệu lực ở lần mở trình duyệt mint kế tiếp.")

    def _open_path(self, p):
        p = pathlib.Path(p)
        try:
            if not p.exists():
                if p.suffix:
                    p.write_text("", encoding="utf-8")
                else:
                    p.mkdir(parents=True, exist_ok=True)
            os.startfile(str(p))
        except Exception as e:
            messagebox.showinfo("Đường dẫn", f"{p}\n{e}")

    def _build_stats_page(self):
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        box = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=14)
        box.pack(fill="both", expand=True)
        ctk.CTkLabel(box, text="Thống kê", font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=24, pady=(24, 12))
        self.stats_label = ctk.CTkLabel(box, text="", font=ctk.CTkFont(size=14),
                                        text_color=TEXT, justify="left")
        self.stats_label.pack(anchor="w", padx=24)
        return page

    def _build_logs_page(self):
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        box = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=14)
        box.pack(fill="both", expand=True)
        ctk.CTkLabel(box, text="Nhật ký hệ thống", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=20, pady=(16, 8))
        self.full_log = ctk.CTkTextbox(box, fg_color="#0c0e14", text_color="#cdd2e0",
                                       font=ctk.CTkFont(family="Consolas", size=12), corner_radius=8)
        self.full_log.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        return page

    # ============================================================ LOG
    def log(self, msg, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}\n"
        for box in (getattr(self, "log_box", None), getattr(self, "full_log", None)):
            if box is not None:
                try:
                    box.insert("end", line); box.see("end")
                except Exception:
                    pass

    def _clear_log(self):
        try:
            self.log_box.delete("1.0", "end")
        except Exception:
            pass

    def _open_api_log(self):
        f = HERE / "api_requests.log"
        if not f.exists():
            f.write_text("(Chưa có request nào.)\n", encoding="utf-8")
        try:
            os.startfile(str(f))
        except Exception:
            messagebox.showinfo("Log API", str(f))

    def _open_manual_log(self):
        f = HERE / "manual_capture.log"
        if not f.exists():
            messagebox.showinfo("Log tay",
                                "Chưa có file. Hãy bật 'Log tay', mở Chromium và thao tác đã nhé.\n"
                                + str(f))
            return
        try:
            os.startfile(str(f))
        except Exception:
            messagebox.showinfo("Log tay", str(f))

    def _open_output(self):
        try:
            os.startfile(str(OUT_DIR))
        except Exception:
            messagebox.showinfo("Thư mục", str(OUT_DIR))

    # ============================================================ RESULTS GRID
    def _clear_results(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self._imgrefs.clear()
        self._slots.clear()
        self._slot_idx = 0
        self._done = 0
        self._target = 0
        self.progress.set(0)
        self.pct_label.configure(text="0%")
        self.result_title.configure(text="Ảnh kết quả (0 / 0)")
        self.total_label.configure(text="Tổng tiến trình\n0 / 0 ảnh")

    # Kích thước thẻ ảnh theo tỉ lệ khung (đều nhau trong 1 lượt)
    _BOX = {"16:9": (244, 138), "9:16": (150, 266), "1:1": (200, 200)}

    @staticmethod
    def _cover(im, W, H):
        """Phóng + cắt giữa để ảnh phủ kín khung WxH (không méo, không viền thừa)."""
        im = im.convert("RGB")
        w, h = im.size
        scale = max(W / w, H / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        im = im.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - W) // 2, (nh - H) // 2
        return im.crop((left, top, left + W, top + H))

    def _build_slots(self, target):
        """Thêm 'target' ô placeholder mới, NỐI TIẾP ảnh các lượt trước (không xóa)."""
        self._box = self._BOX.get(self.aspect_var.get(), (244, 138))
        bw, bh = self._box
        cols = 4
        start = len(self._slots)
        # ảnh mới sẽ lấp vào các ô mới, không đụng vào ô cũ
        self._slot_idx = start
        for i in range(target):
            idx = start + i
            cell = ctk.CTkFrame(self.grid_frame, fg_color=CARD2, corner_radius=12,
                                width=bw + 12, height=bh + 12)
            cell.grid(row=idx // cols, column=idx % cols, padx=7, pady=7)
            cell.grid_propagate(False)
            lbl = ctk.CTkLabel(cell, text="⌛", text_color=MUTED, font=ctk.CTkFont(size=20))
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            self._slots.append({"cell": cell, "label": lbl})
        self._target += target
        self.result_title.configure(text=f"Ảnh kết quả ({self._done} / {self._target})")
        self.total_label.configure(text=f"Tổng tiến trình\n{self._done} / {self._target} ảnh")

    def _fill_slot(self, path, aspect_text, meta=None):
        if self._slot_idx >= len(self._slots):
            return
        slot = self._slots[self._slot_idx]
        slot["meta"] = meta
        slot["path"] = path
        self._slot_idx += 1
        bw, bh = getattr(self, "_box", (244, 138))
        try:
            im = Image.open(path)
            thumb = self._cover(im, bw, bh)
            cimg = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=(bw, bh))
            self._imgrefs.append(cimg)
            lbl = slot["label"]
            lbl.configure(image=cimg, text="", cursor="hand2")
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            lbl.bind("<Button-1>", lambda e, p=path, m=meta: self._click_image(p, m))
            # badge tỉ lệ (góc dưới trái)
            badge = ctk.CTkLabel(slot["cell"], text=f" {aspect_text} ", fg_color="#000000",
                                 text_color="#ffffff", corner_radius=6,
                                 font=ctk.CTkFont(size=10, weight="bold"))
            badge.place(relx=0.03, rely=0.97, anchor="sw")
            badge.bind("<Button-1>", lambda e, p=path, m=meta: self._click_image(p, m))
            # dấu tick (góc trên trái)
            chk = ctk.CTkLabel(slot["cell"], text="✓", fg_color=GREEN, text_color="#06231a",
                               corner_radius=10, width=20, height=20,
                               font=ctk.CTkFont(size=12, weight="bold"))
            chk.place(relx=0.03, rely=0.03, anchor="nw")
            # nút ✎ Sửa (góc dưới phải) nếu ảnh có media_id -> sửa & tạo lại
            if meta and meta.get("media_id"):
                ed = ctk.CTkLabel(slot["cell"], text=" ✎ Sửa ", fg_color=ACCENT,
                                  text_color="#ffffff", corner_radius=6, cursor="hand2",
                                  font=ctk.CTkFont(size=10, weight="bold"))
                ed.place(relx=0.97, rely=0.97, anchor="se")
                ed.bind("<Button-1>", lambda e, p=path, m=meta: self._open_edit_dialog(m, p))
        except Exception as e:
            slot["label"].configure(text=f"Lỗi\n{e}", text_color=RED, font=ctk.CTkFont(size=10))

        self._done += 1
        pct = (self._done / self._target) if self._target else 0
        self.progress.set(pct)
        self.pct_label.configure(text=f"{int(pct*100)}%")
        self.result_title.configure(text=f"Ảnh kết quả ({self._done} / {self._target})")
        self.total_label.configure(text=f"Tổng tiến trình\n{self._done} / {self._target} ảnh")

    def _click_image(self, path, meta=None):
        """Bấm ảnh: nếu là ảnh đã tạo (có media_id) -> mở cửa sổ Sửa; không thì xem lớn."""
        if meta and meta.get("media_id"):
            self._open_edit_dialog(meta, path)
        else:
            self._open_preview(path)

    def _open_edit_dialog(self, meta, path):
        """Sửa prompt trực tiếp trên ảnh ĐÃ TẠO rồi tạo lại (BASE_IMAGE)."""
        if self._busy:
            messagebox.showinfo("Đang bận", "Đợi lượt tạo hiện tại xong đã nhé.")
            return
        top = ctk.CTkToplevel(self)
        top.title("Sửa ảnh — " + pathlib.Path(path).name)
        top.configure(fg_color=BG)
        try:
            im = Image.open(path)
            w, h = im.size
            scale = min(520 / w, 520 / h, 1.0)
            cimg = ctk.CTkImage(light_image=im, dark_image=im,
                                size=(max(1, int(w * scale)), max(1, int(h * scale))))
            self._imgrefs.append(cimg)
            ctk.CTkLabel(top, image=cimg, text="").pack(padx=12, pady=12)
        except Exception:
            pass
        ctk.CTkLabel(top, text="Mô tả phần cần sửa (vd: đổi chữ, đổi màu, thêm icon...):",
                     text_color=TEXT, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=12)
        box = ctk.CTkTextbox(top, height=70, width=520, fg_color=CARD,
                             border_color=STROKE, border_width=1, text_color=TEXT)
        box.pack(padx=12, pady=(4, 8))
        status = ctk.CTkLabel(top, text="", text_color=MUTED, font=ctk.CTkFont(size=11))
        status.pack(anchor="w", padx=12)
        bar = ctk.CTkFrame(top, fg_color="transparent")
        bar.pack(pady=(0, 12))

        def _run():
            prompt = box.get("1.0", "end").strip()
            if not prompt:
                status.configure(text="Hãy nhập mô tả cần sửa.", text_color=RED)
                return
            model = self.model_map.get(self.model_var.get(), self.model_var.get()) or "NARWHAL"
            aspect_text = self.aspect_var.get()
            aspect = ASPECT_MAP.get(aspect_text, "IMAGE_ASPECT_RATIO_LANDSCAPE")
            edit_btn.configure(state="disabled", text="⏳ Đang sửa...")
            status.configure(text="Đang gửi yêu cầu sửa...", text_color=MUTED)
            self._busy = True

            def _job():
                multi = MultiFlow(self.mgr, max_workers=1)
                try:
                    res = multi.edit_image(meta.get("acc_id"), meta.get("project_id"),
                                           meta.get("media_id"), prompt, model, aspect,
                                           n=1, retries=2, log=self._safe_log, detailed=True)
                except Exception as e:
                    self.after(0, lambda e=str(e): _fail(e))
                    return
                self.after(0, lambda: _ok(res, aspect_text))

            def _ok(res, aspect_text):
                self._busy = False
                edit_btn.configure(state="normal", text="✎  Sửa ảnh")
                if not res:
                    status.configure(text="Không sửa được (không có ảnh trả về).", text_color=RED)
                    return
                for d in res:
                    b = d.get("bytes")
                    if not b:
                        continue
                    ext = "png"
                    if b[:3] == b"\xff\xd8\xff":
                        ext = "jpg"
                    elif b[:4] == b"RIFF":
                        ext = "webp"
                    fn = OUT_DIR / f"edit_{int(time.time())}_{random.randint(1000,9999)}.{ext}"
                    try:
                        fn.write_bytes(b)
                    except Exception:
                        continue
                    m = {"media_id": d.get("media_id"), "project_id": d.get("project_id"),
                         "acc_id": d.get("acc_id")}
                    if self._slot_idx >= len(self._slots):
                        self._build_slots(1)
                    self._fill_slot(str(fn), aspect_text, m)
                status.configure(text="Đã sửa xong! Ảnh mới đã thêm vào lưới.", text_color=GREEN)
                self.log("Sửa ảnh: xong, đã thêm ảnh mới vào kết quả.", "SUCCESS")

            def _fail(msg):
                self._busy = False
                edit_btn.configure(state="normal", text="✎  Sửa ảnh")
                status.configure(text=f"Lỗi: {msg}", text_color=RED)
                self.log(f"Sửa ảnh lỗi: {msg}", "ERROR")

            threading.Thread(target=_job, daemon=True).start()

        edit_btn = ctk.CTkButton(bar, text="✎  Sửa ảnh", fg_color=ACCENT,
                                 hover_color=ACCENT_HV, command=_run)
        edit_btn.pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Mở ảnh gốc", fg_color=CARD, hover_color=ACCENT_HV,
                      command=lambda: self._open_path(path)).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Đóng", fg_color=CARD, hover_color=RED,
                      command=top.destroy).pack(side="left", padx=4)
        top.after(120, top.lift)
        top.after(150, top.focus_force)

    def _open_preview(self, path):
        """Cửa sổ xem ảnh lớn khi bấm vào thumbnail."""
        try:
            im = Image.open(path)
            w, h = im.size
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            mw, mh = int(sw * 0.82), int(sh * 0.82)
            scale = min(mw / w, mh / h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))

            top = ctk.CTkToplevel(self)
            top.title(pathlib.Path(path).name)
            top.configure(fg_color=BG)
            cimg = ctk.CTkImage(light_image=im, dark_image=im, size=(nw, nh))
            self._imgrefs.append(cimg)
            ctk.CTkLabel(top, image=cimg, text="").pack(padx=10, pady=10)
            bar = ctk.CTkFrame(top, fg_color="transparent"); bar.pack(pady=(0, 10))
            ctk.CTkLabel(bar, text=pathlib.Path(path).name, text_color=MUTED).pack(side="left", padx=10)
            ctk.CTkButton(bar, text="Mở bằng ứng dụng ngoài", fg_color=CARD, hover_color=ACCENT_HV,
                          command=lambda: self._open_path(path)).pack(side="left", padx=4)
            ctk.CTkButton(bar, text="Đóng", fg_color=ACCENT, hover_color=ACCENT_HV,
                          command=top.destroy).pack(side="left", padx=4)
            top.after(120, top.lift)
            top.after(150, top.focus_force)
        except Exception:
            try:
                os.startfile(path)
            except Exception:
                pass

    # ============================================================ ACCOUNT CHIPS
    def _render_chips(self):
        for w in self.acc_chip_wrap.winfo_children():
            w.destroy()
        states = self.mgr.states()
        if not states:
            ctk.CTkLabel(self.acc_chip_wrap, text="Chưa có tài khoản — vào tab Tài khoản để thêm.",
                         text_color=MUTED).pack(side="left", padx=8)
            return
        color = {"ready": GREEN, "cooldown": YELLOW, "login_needed": GRAY}
        for st in states[:6]:
            dot = color.get(st.get("status", ""), GRAY)
            chip = ctk.CTkFrame(self.acc_chip_wrap, fg_color=CARD, corner_radius=10)
            chip.pack(side="left", padx=4, fill="y")
            top = ctk.CTkFrame(chip, fg_color="transparent"); top.pack(anchor="w", padx=10, pady=(7, 0))
            ctk.CTkLabel(top, text="●", text_color=dot, font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkLabel(top, text=f" {st['name']}", text_color=TEXT,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            ctk.CTkLabel(chip, text=f"{st.get('uses', 0)} lượt · {st.get('inflight',0)}/4 luồng",
                         text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(1, 6))

    # ============================================================ ACCOUNTS TAB
    def _refresh_accounts(self):
        for w in self.acc_list.winfo_children():
            w.destroy()
        states = self.mgr.states()
        if not states:
            ctk.CTkLabel(self.acc_list, text="Chưa có tài khoản. Nạp cookie bên dưới.",
                         text_color=MUTED).pack(anchor="w", padx=12, pady=12)
            return
        color = {"ready": GREEN, "cooldown": YELLOW, "login_needed": GRAY}
        for st in states:
            row = ctk.CTkFrame(self.acc_list, fg_color=CARD, corner_radius=10)
            row.pack(fill="x", padx=8, pady=4)
            dot = color.get(st.get("status", ""), GRAY)
            ctk.CTkLabel(row, text="●", text_color=dot, width=20).pack(side="left", padx=(12, 4), pady=10)
            ctk.CTkLabel(row, text=st["name"], text_color=TEXT, width=120, anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
            info = (f"{'🔑 đăng nhập' if st.get('logged_in') else '❌ chưa đăng nhập'}"
                    f"   ·   {st.get('uses',0)} lượt   ·   lỗi {st.get('failures',0)}"
                    f"   ·   {st.get('status','')}")
            ctk.CTkLabel(row, text=info, text_color=MUTED, anchor="w").pack(side="left", padx=10)
            ctk.CTkButton(row, text="Chọn", width=60, height=28, corner_radius=8,
                          fg_color=PANEL, hover_color=ACCENT_HV,
                          command=lambda i=st["id"]: self._select_account(i)).pack(side="right", padx=10)

    def _select_account(self, acc_id):
        self._acc_selected = acc_id
        acc = self.mgr.get(acc_id)
        if acc:
            self.acc_name.delete(0, "end"); self.acc_name.insert(0, acc.name)
        self.log(f"Đã chọn tài khoản: {acc_id}")

    def _reload_cookies_dir(self):
        try:
            n = self.mgr.autoload_cookie_files()
            self._refresh_accounts()
            self.log(f"Đã nạp {n} file cookie từ thư mục cookies/.", "SUCCESS")
        except Exception as e:
            self.log(f"Lỗi nạp cookies: {e}", "WARNING")

    def _grab_cookie(self):
        try:
            ck = cg.grab_cookies(self.browser_var.get())
            self.cookie_box.delete("1.0", "end"); self.cookie_box.insert("1.0", ck)
            self.log(f"Đã lấy {len(ck)} ký tự cookie từ {self.browser_var.get()}.", "SUCCESS")
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không lấy được cookie: {e}")

    def _import_account(self):
        name = self.acc_name.get().strip()
        raw = self.cookie_box.get("1.0", "end").strip()
        if not name or not raw:
            messagebox.showwarning("Thiếu", "Cần nhập tên tài khoản và cookie.")
            return
        res = self.mgr.add_or_update_with_cookies(name, raw)
        self._refresh_accounts()
        if res.get("ok"):
            self.log(f"Đã nạp {res.get('count')} cookie cho '{name}'.", "SUCCESS")
            self.cookie_box.delete("1.0", "end")
        else:
            self.log(f"Lỗi nạp: {res.get('error')}", "WARNING")

    def _check_login(self):
        if not self._acc_selected:
            messagebox.showwarning("Chọn", "Hãy bấm 'Chọn' ở 1 tài khoản trước.")
            return
        acc_id = self._acc_selected
        self.log(f"Đang kiểm tra đăng nhập {acc_id}...")

        def run():
            ok = self.mgr.check_login(acc_id)
            self.after(0, self._refresh_accounts)
            self.after(0, lambda: self.log(
                f"{acc_id}: {'ĐĂNG NHẬP OK' if ok else 'thất bại'}",
                "SUCCESS" if ok else "WARNING"))
        threading.Thread(target=run, daemon=True).start()

    def _sniff_models(self):
        if not self._acc_selected:
            messagebox.showwarning("Chọn", "Hãy chọn 1 tài khoản đã đăng nhập.")
            return
        acc_id = self._acc_selected
        self.log("Đang tự dò model ảnh từ Flow...")

        def run():
            models = self.mgr.sniff_models(acc_id, max_wait=240)
            def done():
                if models:
                    entries = load_model_entries()
                    existing = {e["code"] for e in entries}
                    for m in models:
                        code = m.get("code") if isinstance(m, dict) else m
                        label = (m.get("label") if isinstance(m, dict) else m) or code
                        if code and code not in existing:
                            entries.append({"code": code, "label": label}); existing.add(code)
                    save_model_entries(entries)
                    self.model_map = {e["label"]: e["code"] for e in entries}
                    self.model_var.configure(values=list(self.model_map.keys()))
                    self.log("Model ảnh: " + ", ".join(f"{e['label']}({e['code']})" for e in entries),
                             "SUCCESS")
                else:
                    self.log("Chưa lấy được model ảnh. Xem sniff_requests.log.", "WARNING")
            self.after(0, done)
        threading.Thread(target=run, daemon=True).start()

    def _delete_account(self):
        if not self._acc_selected:
            return
        if messagebox.askyesno("Xóa", f"Xóa tài khoản {self._acc_selected}?"):
            self.mgr.delete_account(self._acc_selected)
            self._acc_selected = None
            self._refresh_accounts()

    # ----- SỬA ẢNH / img2img: chọn ảnh nguồn -----
    def _choose_edit_image(self):
        path = filedialog.askopenfilename(
            title="Chọn ảnh nguồn để sửa",
            filetypes=[("Ảnh", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Tất cả", "*.*")])
        if not path:
            return
        self._edit_image_path = path
        name = pathlib.Path(path).name
        # Thumbnail preview
        try:
            im = Image.open(path).convert("RGB")
            thumb = self._cover(im, 64, 64)
            cimg = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=(64, 64))
            self._imgrefs.append(cimg)
            self.edit_thumb.configure(image=cimg, text="")
        except Exception:
            self.edit_thumb.configure(text="?", image=None)
        short = name if len(name) <= 28 else name[:25] + "..."
        self.edit_img_label.configure(text=f"{short}\n→ bấm để tạo ảnh mới dựa trên ảnh này",
                                      text_color=GREEN)
        self.gen_btn.configure(text="🖼   IMG2IMG")

    def _clear_edit_image(self):
        self._edit_image_path = None
        self.edit_thumb.configure(image=None, text="—")
        self.edit_img_label.configure(text="Chưa chọn ảnh\n(để trống = tạo ảnh mới)",
                                      text_color=MUTED)
        self._refresh_gen_btn_text()

    def _refresh_gen_btn_text(self):
        """Đặt lại nhãn nút TẠO ẢNH theo chế độ đang bật."""
        if self.manual_var.get():
            self.gen_btn.configure(text="🖐  MỞ CHROMIUM (LOG TAY)")
        elif self._edit_image_path:
            self.gen_btn.configure(text="🖼   IMG2IMG")
        else:
            self.gen_btn.configure(text="▶   TẠO ẢNH")

    # ============================================================ GENERATE
    def on_generate(self):
        if self._busy:
            return
        # Chế độ "log tay": không tạo ảnh tự động, chỉ mở Chromium + bắt request.
        if self.manual_var.get():
            self._start_manual_capture()
            return
        healthy = self.mgr.healthy_accounts()
        if not healthy:
            messagebox.showwarning("Không có tài khoản",
                                   "Chưa có tài khoản sẵn sàng. Vào tab Tài khoản để nạp/đăng nhập.")
            return
        raw = self.prompt_box.get("1.0", "end").strip()
        prompts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
        if not prompts:
            messagebox.showwarning("Thiếu prompt", "Vui lòng nhập mô tả ảnh.")
            return

        n = parse_count(self.n_var.get(), 1)
        workers = parse_count(self.workers_var.get(), 1)
        try:
            retries = max(0, int(self.retries_var.get()))
        except Exception:
            retries = 3
        seed_raw = self.seed_entry.get().strip()
        seed = int(seed_raw) if seed_raw.lstrip("-").isdigit() else None
        # Đọc model/tỉ lệ NGAY Ở LUỒNG CHÍNH (không đọc StringVar trong thread -> tránh crash)
        model = self.model_map.get(self.model_var.get(), self.model_var.get()) or "NARWHAL"
        aspect_text = self.aspect_var.get()

        # Chế độ SỬA ẢNH: đọc bytes ảnh nguồn (nếu đã chọn) ngay ở luồng chính.
        edit_bytes = None
        if self._edit_image_path:
            try:
                edit_bytes = pathlib.Path(self._edit_image_path).read_bytes()
            except Exception as e:
                messagebox.showerror("Lỗi ảnh", f"Không đọc được ảnh nguồn:\n{e}")
                return

        self._busy = True
        self._stop = False
        self.gen_btn.configure(state="disabled", text="⏳  Đang tạo...")
        self.stop_btn.configure(state="normal")

        self._build_slots(len(prompts) * n)
        mode_txt = "img2img" if edit_bytes else "tạo ảnh"
        self.log(f"Bắt đầu {mode_txt} với {len(prompts)} prompt × {n} ảnh, "
                 f"{workers} luồng, retry {retries}.", "INFO")
        threading.Thread(target=self._worker,
                         args=(prompts, n, workers, seed, retries, model, aspect_text,
                               edit_bytes),
                         daemon=True).start()

    def on_stop(self):
        if self._manual_active:
            self._stop_manual_capture()
            return
        if self._busy:
            self._stop = True
            self.stop_btn.configure(state="disabled")
            self.log("Đã yêu cầu DỪNG.", "WARNING")

    # ----- LOG TAY: mở Chromium cho user thao tác + bắt request -----
    def _on_manual_toggle(self):
        """Đổi nhãn nút khi bật/tắt ô 'Log tay'."""
        if self._busy or self._manual_active:
            return
        self._refresh_gen_btn_text()

    def _start_manual_capture(self):
        if self._manual_active:
            return
        # Chọn acc: ưu tiên acc khỏe, không thì acc đầu tiên (vẫn mở để bạn đăng nhập tay).
        healthy = self.mgr.healthy_accounts()
        acc = healthy[0] if healthy else (self.mgr.accounts[0] if self.mgr.accounts else None)
        if acc is None:
            messagebox.showwarning("Chưa có tài khoản",
                                   "Vào tab Tài khoản để thêm 1 tài khoản trước đã.")
            return
        self._manual_acc_id = acc.id
        self._manual_active = True
        self.gen_btn.configure(state="disabled", text="🖐  Đang mở Chromium...")
        self.stop_btn.configure(state="normal")
        self.manual_chk.configure(state="disabled")
        self.log(f"LOG TAY: đang mở Chromium cho [{acc.name}]...", "INFO")

        def _job():
            res = self.mgr.start_manual_capture(acc.id, log=self._safe_log, timeout=180)
            self.after(0, lambda: self._on_manual_started(res))

        threading.Thread(target=_job, daemon=True).start()

    def _on_manual_started(self, res):
        if res.get("ok"):
            self.gen_btn.configure(text="🖐  ĐANG LOG TAY (bấm DỪNG để kết thúc)")
            self.log("LOG TAY: Chromium đã mở. Hãy tự đăng nhập / tạo ảnh trong cửa sổ.",
                     "SUCCESS")
            self.log(f"Mọi request đang được ghi vào: {res.get('file')}", "INFO")
        else:
            self._manual_active = False
            self._manual_acc_id = None
            self.gen_btn.configure(state="normal", text="🖐  MỞ CHROMIUM (LOG TAY)")
            self.stop_btn.configure(state="disabled")
            self.manual_chk.configure(state="normal")
            self.log(f"LOG TAY lỗi: {res.get('error')}", "ERROR")

    def _stop_manual_capture(self):
        aid = self._manual_acc_id
        self.stop_btn.configure(state="disabled")
        self.log("LOG TAY: đang dừng & đóng phiên bắt request...", "WARNING")

        def _job():
            try:
                self.mgr.stop_manual_capture(aid)
            except Exception:
                pass
            self.after(0, self._on_manual_stopped)

        threading.Thread(target=_job, daemon=True).start()

    def _on_manual_stopped(self):
        self._manual_active = False
        self._manual_acc_id = None
        self.gen_btn.configure(state="normal",
                               text="🖐  MỞ CHROMIUM (LOG TAY)" if self.manual_var.get()
                               else "▶   TẠO ẢNH")
        self.manual_chk.configure(state="normal")
        self.log("LOG TAY: đã dừng. File request đã lưu (manual_capture.log).", "SUCCESS")

    def _worker(self, prompts, n, workers, seed, retries, model, aspect_text,
                image_bytes=None):
        try:
            multi = MultiFlow(self.mgr, max_workers=workers)
            aspect = ASPECT_MAP.get(aspect_text, "IMAGE_ASPECT_RATIO_LANDSCAPE")
            total = len(prompts)
            for idx, prompt in enumerate(prompts, 1):
                if self._stop:
                    break
                self.after(0, lambda i=idx: self.log(f"Account pool đang tạo ảnh (prompt {i}/{total})..."))
                try:
                    imgs = multi.generate(prompt=prompt, model=model, aspect=aspect,
                                          n=n, seed=seed, retries=retries, log=self._safe_log,
                                          image_bytes=image_bytes, detailed=True)
                except Exception as e:
                    self.after(0, lambda e=str(e), i=idx:
                               self.log(f"Prompt {i} lỗi: {e}", "WARNING"))
                    continue
                for j, d in enumerate(imgs):
                    b = d.get("bytes")
                    if not b:
                        continue
                    meta = {"media_id": d.get("media_id"), "project_id": d.get("project_id"),
                            "acc_id": d.get("acc_id")}
                    ext = "png"
                    if b[:3] == b"\xff\xd8\xff":
                        ext = "jpg"
                    elif b[:4] == b"RIFF":
                        ext = "webp"
                    fn = OUT_DIR / f"flow_{int(time.time())}_{idx}_{j}_{random.randint(1000,9999)}.{ext}"
                    try:
                        fn.write_bytes(b)
                    except Exception:
                        continue
                    self.after(0, lambda p=str(fn), m=meta: self._fill_slot(p, aspect_text, m))
                self.after(0, lambda i=idx, c=len(imgs):
                           self.log(f"Prompt {i}/{total}: xong {c} ảnh.", "SUCCESS"))
        finally:
            self._busy = False
            self.after(0, self._on_done)

    def _safe_log(self, msg):
        try:
            self.after(0, lambda: self.log(str(msg)))
        except Exception:
            pass

    def _on_done(self):
        self.stop_btn.configure(state="disabled")
        self.gen_btn.configure(state="normal")
        self._refresh_gen_btn_text()
        self.log(f"Hoàn tất. Đã tạo {self._done}/{self._target} ảnh.", "SUCCESS")

    # ============================================================ POLL
    def _poll(self):
        if getattr(self, "_closing", False):
            return
        try:
            self._render_chips()
            if self.pages.get("stats") is not None and self.stats_label.winfo_exists():
                sts = self.mgr.states()
                ready = sum(1 for s in sts if s.get("status") == "ready")
                self.stats_label.configure(
                    text=f"Tổng tài khoản: {len(sts)}\nSẵn sàng: {ready}\n"
                         f"Ảnh đã tạo (phiên này): {self._done}")
        except Exception:
            pass
        if not getattr(self, "_closing", False):
            try:
                self._poll_id = self.after(1500, self._poll)
            except Exception:
                pass

    def _on_close(self):
        # Đóng AN TOÀN: dừng poll + thread trước, rồi mới hủy cửa sổ -> tránh Tcl crash.
        self._closing = True
        self._stop = True
        try:
            if getattr(self, "_poll_id", None):
                self.after_cancel(self._poll_id)
        except Exception:
            pass
        # Đóng trình duyệt/loop nền trong 1 thread có giới hạn thời gian (không để treo).
        done = threading.Event()

        def _shutdown():
            try:
                self.mgr.stop_all()
            except Exception:
                pass
            done.set()
        threading.Thread(target=_shutdown, daemon=True).start()
        done.wait(timeout=6)
        try:
            self._imgrefs.clear()
        except Exception:
            pass
        try:
            self.quit()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


def main():
    app = App()
    app.after(200, app._refresh_accounts)
    app.mainloop()


if __name__ == "__main__":
    main()
