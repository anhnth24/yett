# Đóng gói `yett.exe` (Windows) / binary (macOS, Linux)

Tạo 1 file chạy được **không cần cài Python**. Double-click → nếu chưa có config thì chạy
wizard `setup`, sau đó mở web UI trong trình duyệt.

## Yêu cầu
- Build cùng OS với máy đích (PyInstaller không cross-compile): build trên Windows ra `.exe`.
- Python 3.11 + đã `pip install -e .` trong repo + `pip install pyinstaller==6.11.1`.

## Build
Dùng spec có sẵn (khuyến nghị — đã cấu hình hidden imports + bundle skills):
```bash
pip install pyinstaller==6.11.1
pyinstaller packaging/yett.spec
# Kết quả: dist/yett.exe  (Windows)  |  dist/yett  (macOS/Linux)
```

## Dùng
1. Copy file ở `dist/` vào một thư mục riêng cho yett.
2. Đặt cạnh nó thư mục `config/` (có `harness.yaml`). Chưa có? Double-click sẽ tự chạy wizard.
3. Đặt API key: file `secrets/llm_key` cạnh exe, hoặc biến môi trường `YETT_SECRET_LLM_KEY`.
4. **Double-click `yett.exe`** → console nhỏ mở + trình duyệt bật giao diện chat.
   Hoặc lệnh: `yett.exe chat "..."`, `yett.exe setup`, `yett.exe serve --port 9000`.

## Ghi chú
- `yett.spec` đã gom `yett`, `pydantic`, `sqlglot`, `yaml` (hidden imports) + bundle `skills/`.
- exe tìm `config/`, `skills/`, `secrets/`, `state/` theo thư mục làm việc — đặt exe trong thư mục riêng.
- Cỡ ~30–50MB (gồm Python runtime + deps) — bình thường với onefile.
- Muốn ẩn cửa sổ console: sửa `console=False` trong `yett.spec` (mất log; chỉ khi đã ổn định).
- Secret KHÔNG nhúng vào exe — luôn nằm ngoài ở `secrets/` hoặc env.
