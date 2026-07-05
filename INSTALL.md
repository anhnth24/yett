# Cài đặt yett — hướng dẫn từng bước

Dành cho **Windows + WSL2 + Docker Desktop** (môi trường chính). Linux native tương tự (bỏ phần WSL2).

---

## Bước 0 — Chuẩn bị Windows (một lần)

1. **Bật WSL2** (PowerShell admin):
   ```powershell
   wsl --install -d Ubuntu
   ```
   Khởi động lại, mở "Ubuntu" từ Start menu, tạo user.

2. **Cài Docker Desktop** cho Windows → Settings → Resources → WSL integration → bật cho distro Ubuntu.
   Kiểm trong Ubuntu (WSL2):
   ```bash
   docker run --rm hello-world
   ```

---

## Bước 1 — Lấy mã & cài (trong WSL2 / Ubuntu)

```bash
# Python 3.11+
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

git clone <repo-url> && cd go-learn
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Kiểm nhanh (không cần API key):
```bash
yett demo          # chạy một turn agent offline
pytest -q          # 174 test
```

---

## Bước 2 — Cài đặt bằng wizard (khuyến nghị)

```bash
yett setup
```

Wizard hỏi từng bước:

| Bước | Nội dung |
|---|---|
| 1/5 | **Chọn provider** — GLM, MiniMax, DeepSeek, Gemini, OpenAI, Anthropic, Grok, Qwen, Mistral (kèm giá tham khảo) |
| 2/5 | **Chọn model** — gợi ý model + bản rẻ hơn |
| 3/5 | **Dán API key** — lưu vào `secrets/llm_key` quyền `600`, KHÔNG vào config/git |
| 4/5 | **Thêm project** — đường dẫn WSL2 (vd `/mnt/d/work/duan`), thêm nhiều cái |
| 5/5 | **Ngưỡng cảnh báo chi phí/tháng** (tuỳ chọn) |

Kết quả: `config/harness.yaml` (không chứa secret) + `secrets/llm_key` (600).

**Lấy API key ở đâu:**
- GLM: https://z.ai (quốc tế) hoặc https://open.bigmodel.cn (TQ)
- MiniMax: https://platform.minimax.io
- DeepSeek: https://platform.deepseek.com
- Gemini: https://ai.google.dev
- OpenAI: https://platform.openai.com
- Anthropic: https://console.anthropic.com

---

## Bước 2 (thay thế) — Cài đặt thủ công

```bash
cp config/harness.example.yaml config/harness.yaml
$EDITOR config/harness.yaml     # sửa provider, model, projects, egress
mkdir -p secrets && printf '%s' "<API-KEY>" > secrets/llm_key && chmod 600 secrets/llm_key
```
Đảm bảo `secret_backend: file` trong config (wizard đặt sẵn).

---

## Bước 3 — Dùng

```bash
yett chat "xin chào, giới thiệu về bạn"
yett chat "báo cáo tiến độ tuần này của các project"
yett traces list --state state          # xem lịch sử turn
yett traces get <trace-id> --state state
yett usage --by provider --state state  # chi phí theo provider
```

Đổi model/provider bất cứ lúc nào: sửa `config/harness.yaml` (hoặc chạy lại `yett setup`), không đụng code.

---

## Lưu ý WSL2 (quan trọng)

- **Hiệu năng file:** project trên `/mnt/c` `/mnt/d` (ổ Windows/NTFS) chạy git/exec **chậm**.
  Nếu chậm, clone project hay dùng vào ext4 của WSL (`~/work/...`) và trỏ `projects.path` vào đó.
- **VPN (FortiClient):** nếu chạy trên Windows host, kiểm tra SSH từ WSL2 có qua tunnel không;
  nếu không, cài `openfortivpn` trong WSL2. (Tính năng SSH/VPN ở giai đoạn nối backend — xem README.)
- **Docker:** phải bật WSL integration trong Docker Desktop. Không có Docker → đặt tạm
  `sandbox.backend: local` trong config (kém cô lập hơn, chỉ dùng dev).

---

## Backup / Nâng cấp

```bash
bash deploy/backup.sh ./backups          # sao lưu state + workspace + config (KHÔNG gồm secret)
git pull && pip install -e .             # nâng cấp; dữ liệu giữ nguyên
```

Chi tiết vận hành: [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).
