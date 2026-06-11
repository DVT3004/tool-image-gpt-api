# -*- coding: utf-8 -*-
"""Hằng số & helper dùng chung cho Flow tool (tách ra để app gọn, không phụ thuộc GUI cũ)."""

import json
import pathlib

HERE = pathlib.Path(__file__).parent
MODELS_FILE = HERE / "models.json"

ASPECTS = ["16:9", "9:16", "1:1"]
ASPECT_MAP = {
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
}
# Lựa chọn nhanh dạng x1..x4 cho Số ảnh / Số luồng
COUNT_CHOICES = ["x1", "x2", "x3", "x4"]
# Số luồng song song (mỗi acc tối đa 4 luồng -> nhiều acc thì tổng cao hơn)
WORKER_CHOICES = ["x1", "x2", "x3", "x4", "x6", "x8", "x12", "x16", "x24", "x32", "x48", "x60"]

# Mã model ẢNH thật của Flow: GEM_PIX_2 = Nano Banana Pro | NARWHAL = Nano Banana 2
MODELS = ["GEM_PIX_2", "NARWHAL"]
DEFAULT_LABELS = {"GEM_PIX_2": "Nano Banana Pro", "NARWHAL": "Nano Banana 2"}


def parse_count(s, default=1):
    """'x3' -> 3 ; '2' -> 2 ; lỗi -> default."""
    try:
        return max(1, int(str(s).lower().lstrip("x")))
    except Exception:
        return default


def is_video_model(name):
    """True nếu mã model là model VIDEO/âm thanh (không dùng để tạo ảnh)."""
    n = str(name).lower()
    return n.startswith("veo") or "video" in n or "audio" in n


def load_model_entries():
    """Trả list dict {'label','code'} cho các MODEL ẢNH (bỏ model video)."""
    entries, seen = [], set()

    def _add(code, label=None):
        if not code or is_video_model(code) or code in seen:
            return
        seen.add(code)
        entries.append({"code": code, "label": label or DEFAULT_LABELS.get(code, code)})

    for code in MODELS:
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
    out, seen = [], set()
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
