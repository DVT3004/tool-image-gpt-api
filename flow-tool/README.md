# Flow Multi-Account Image Tool

App desktop tạo/sửa ảnh qua Google Labs **Flow** (Nano Banana), hỗ trợ nhiều tài khoản,
chạy song song và tự lấy token reCAPTCHA + access_token qua phiên trình duyệt đã đăng nhập.

## Chạy app

```bash
pip install customtkinter pillow requests playwright
playwright install chromium
python gui_modern.py        # hoặc bấm run_gui_modern.bat
```

## Tính năng

- **Tạo ảnh**: nhập prompt (mỗi prompt cách nhau 1 dòng trống), chọn model / tỉ lệ /
  số ảnh / số luồng / seed.
- **Image-to-image**: chọn 1 ảnh từ máy ("Ảnh đưa vào") → tạo ảnh mới dựa trên nó
  (dùng `IMAGE_INPUT_TYPE_REFERENCE`).
- **Sửa ảnh đã tạo**: bấm nút **✎ Sửa** trên ảnh kết quả → nhập mô tả → tạo lại từ chính
  ảnh đó (dùng `IMAGE_INPUT_TYPE_BASE_IMAGE`).
- **Log tay**: mở Chromium cho bạn tự thao tác, tool ghi lại mọi request (vào
  `manual_capture.log`) để phục vụ phát triển thêm.
- **Đa tài khoản / đa luồng**: mỗi acc tối đa 4 luồng, tự xoay vòng acc khi lỗi/quota.

## Cơ chế xác thực

- Project + `access_token` được lấy **trong phiên trình duyệt đã đăng nhập** của tài khoản
  (fetch ngay trong trang `labs.google`) → tránh lỗi 401 của các API tRPC.
- Token reCAPTCHA mint qua chính trình duyệt đó.
- `batchGenerateImages` / `uploadImage` gọi bằng `requests` với Bearer token hợp lệ.

## Các tệp

| Tệp | Vai trò |
|-----|---------|
| `gui_modern.py` | App desktop (CustomTkinter) |
| `flow_accounts.py` | Quản lý tài khoản, cookie, mint token, lấy project/token qua trình duyệt |
| `flow_multi.py` | Tạo/sửa ảnh đa tài khoản, đa luồng |
| `flow_config.py` | Hằng số & helper (model, tỉ lệ khung...) |
| `cookie_grabber.py` / `cookie_import.py` | Lấy / nạp cookie |
| `flow_log.py` | Ghi log |
| `models.json` | Danh sách model ảnh |

> `accounts.json`, `login_data/`, `mint_data/`, `cookies/` và các `*.log` chứa cookie/token
> nên **không commit** (đã có trong `.gitignore`).
