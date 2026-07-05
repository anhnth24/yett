# Cài đặt yett — hướng dẫn từng bước

Chọn 1 trong 4 cách cài (khuyến nghị theo thứ tự):

| Cách | Hợp với | Ghi chú |
|---|---|---|
| **A. Host trong Docker Desktop** | Windows/macOS/Linux | Sạch nhất — yett chạy trong Linux container, mọi tính năng đúng thiết kế. Xem [§A](#a--host-trong-docker-desktop-khuyến-nghị) |
| **B. WSL2** (Windows) | Windows muốn chạy trực tiếp | Môi trường Linux đầy đủ. Xem [§B](#b--windows--wsl2) |
| **C. macOS native** | máy Mac | POSIX sẵn — chạy thẳng, không cần gì thêm. Xem [§C](#c--macos-native) |
| **D. Windows native (PowerShell)** | Windows không muốn WSL/Docker | Chạy được core; exec cần lưu ý. Xem [§D](#d--windows-native-powershell) |

> **Bạn dùng Windows + host trên Docker Desktop → đọc §A.** Đó là lựa chọn tốt nhất.

---

## A — Host trong Docker Desktop (khuyến nghị)

yett chạy **trong container Linux** nên không dính vấn đề shell/quyền file của Windows —
`sh`, hardline cmdguard, quyền `600` đều hoạt động đúng.

```bash
# 1. Cài Docker Desktop (Windows/macOS), mở lên.
# 2. Lấy mã + cấu hình (chạy ở PowerShell hoặc Terminal, KHÔNG cần WSL shell):
git clone <repo-url>
cd go-learn
copy config\harness.example.yaml config\harness.yaml     # Windows;  macOS/Linux: cp
#   sửa provider/model; với projects dùng ĐƯỜNG DẪN TRONG CONTAINER (xem dưới)

# 3. Truyền API key: tạo file .env cạnh deploy/docker-compose.yml
echo YETT_SECRET_LLM_KEY=sk-your-key > deploy\.env

# 4. Build + chạy tương tác:
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml run --rm yett chat "xin chào"
```

**Mount project của bạn** để agent làm việc trên nó — sửa `deploy/docker-compose.yml`:
```yaml
    volumes:
      - "D:/work/duan:/projects/duan"    # ổ Windows → đường dẫn trong container
```
rồi trong `config/harness.yaml`:
```yaml
projects:
  duan: { path: /projects/duan }         # dùng đường dẫn CONTAINER, không phải D:/...
```

### Giới hạn khi host trong Docker (đọc kỹ)

1. **exec sandbox — dùng `sandbox.backend: local`** trong config. Vì sao: khi yett đã ở
   trong container, muốn tạo container-con (backend `docker`) phải mount `docker.sock` của
   host — điều này **cho container quyền điều khiển Docker daemon của máy bạn** (rủi ro bảo mật).
   Thay vào đó để `backend: local`: lệnh chạy **ngay trong container yett**, mà container này
   ĐÃ là ranh giới cô lập với máy Windows. Hardline cmdguard vẫn chặn lệnh nguy hiểm.
2. **Chat tương tác** phải qua `docker compose run --rm yett chat "..."` (cần `-it`, đã bật
   `stdin_open/tty`). Hoặc v0.3 thêm Telegram để giao việc từ xa, không cần terminal.
3. **File project mount từ ổ Windows** vào container vẫn **chậm I/O** (giống hạn chế WSL `/mnt`).
   Project hay dùng nên copy vào volume `yett-workspace` hoặc ext4.
4. **VPN/SSH ra ngoài**: SSH client có trong image. VPN (openfortivpn) trong container cần
   quyền `NET_ADMIN` + `/dev/net/tun` — cấu hình thêm khi tính năng VPN được nối (chưa có ở bản này).
5. **Dữ liệu**: state/workspace/secrets nằm trong Docker **named volume** (giữ qua restart).
   Backup: `docker run --rm -v yett-state:/s -v $PWD:/b busybox tar czf /b/state.tgz -C /s .`
6. **Cập nhật**: `git pull` rồi `docker compose build` lại; volume dữ liệu giữ nguyên.

---

## B — Windows + WSL2

Dành cho Windows muốn chạy trực tiếp trong Linux (không đóng container).

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
pytest -q          # 176 test
```

---

## C — macOS native

macOS là Unix (có `sh`, quyền file POSIX) → chạy thẳng, không cần Docker/WSL:

```bash
brew install python@3.11 git         # nếu chưa có
git clone <repo-url> && cd go-learn
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
yett demo
```
Muốn tool `exec` chạy cô lập hơn: cài Docker Desktop for Mac và đặt `sandbox.backend: docker`.
Không có Docker → `sandbox.backend: local` (chạy bằng shell máy, cmdguard vẫn chặn lệnh nguy hiểm).

---

## D — Windows native (PowerShell, không WSL/Docker)

Chạy được **core** (chat, memory, skills, traces, cost, query DB) bằng Python thuần:

```powershell
# Cài Python 3.11+ từ python.org (nhớ tick "Add to PATH")
git clone <repo-url>
cd go-learn
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e .
yett demo
```

**Giới hạn Windows native:**
- Tool `exec` với `sandbox.backend: local`: yett tự dùng `cmd /c` khi không có `sh`.
  cmdguard **đã chặn cả lệnh xóa Windows** (`del`, `rd /s`, `Remove-Item`, `format`, `diskpart`)
  nên hardline vẫn phủ. Muốn cô lập mạnh hơn → cài Docker Desktop, đặt `backend: docker`.
- Quyền `600` cho `secrets/llm_key` không enforce chuẩn trên NTFS (ACL khác POSIX) — key vẫn
  nằm ngoài config/git, nhưng nếu cần chặt hãy dùng cách §A (Docker) hoặc DPAPI/keyring sau.
- Tính năng SSH/VPN (khi nối) hợp Linux hơn — Windows native nên tập trung vào trợ lý + DB.

---

## Cấu hình provider (mọi cách) — wizard

```bash
yett setup
```

Wizard hỏi từng bước:

| Bước | Nội dung |
|---|---|
| 1/5 | **Chọn provider** — GLM, MiniMax, DeepSeek, Gemini, OpenAI, Anthropic, Grok, Qwen, Mistral (kèm giá tham khảo) |
| 2/5 | **Chọn model** — gợi ý model + bản rẻ hơn |
| 3/5 | **Dán API key** — lưu vào `secrets/llm_key` quyền `600`, KHÔNG vào config/git |
| 4/5 | **Thêm project** — đường dẫn (WSL2 `/mnt/d/...`, container `/projects/...`, Win `D:\...`, mac `/Users/...`) |
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

### Cấu hình thủ công (thay cho wizard)

```bash
cp config/harness.example.yaml config/harness.yaml
$EDITOR config/harness.yaml     # sửa provider, model, projects, egress
mkdir -p secrets && printf '%s' "<API-KEY>" > secrets/llm_key && chmod 600 secrets/llm_key
```
Đảm bảo `secret_backend: file` trong config (wizard đặt sẵn).

---

## Dùng

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
