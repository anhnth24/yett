# Đóng gói yett.exe (Windows)

Tạo 1 file `yett.exe` chạy không cần cài Python. Double-click → mở giao diện chat web.

## Build (trên máy Windows)

```powershell
# 1. Cài Python 3.11+ (chỉ cần cho lúc build), rồi:
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
pip install pyinstaller

# 2. Đóng gói:
cd packaging
pyinstaller yett.spec

# 3. Kết quả: packaging\dist\yett.exe
```

## Phân phối & chạy

Đặt cạnh `yett.exe` (cùng thư mục) khi chạy:
```
yett.exe
config\harness.yaml      ← cấu hình (tạo bằng: yett.exe setup)
secrets\llm_key          ← API key (wizard tự tạo, quyền hạn theo Windows)
workspace\               ← memory, skill, subagent, báo cáo
state\                   ← traces/checkpoint (tự tạo)
```

Lần đầu: double-click `yett.exe` → nếu chưa có config sẽ **chạy wizard** hỏi provider/model/key,
rồi tự mở trình duyệt vào giao diện chat.

Dùng như CLI cũng được:
```
yett.exe setup
yett.exe chat "báo cáo tiến độ tuần này"
yett.exe serve --open
```

## Lưu ý
- **exec native**: khi chạy .exe không có Docker, tool `exec` chạy bằng shell Windows;
  cmdguard chặn lệnh xóa/phá hoại (`del`, `rd /s`, `Remove-Item`, `format`...) nhưng
  KHÔNG cô lập thật như container. Muốn cô lập mạnh → cài Docker Desktop, đặt `sandbox.backend: docker`.
- **Secret** nằm ở `secrets/` cạnh exe (không nhúng vào exe, không commit).
- **SmartScreen**: exe chưa ký số có thể bị Windows cảnh báo lần đầu — chọn "More info → Run anyway",
  hoặc ký số bằng chứng chỉ của bạn (khuyến nghị khi phát hành nội bộ).
- Build lại khi cập nhật code: `git pull && pip install -e . && pyinstaller yett.spec`.
