# yett — Runbook cài đặt (WSL2 + Docker Desktop)

## Yêu cầu
- Windows + WSL2 (Ubuntu) + Docker Desktop (bật WSL integration cho distro)
- Python 3.11+ trong WSL2
- API key MiniMax hoặc GLM

## Cài (trong WSL2)
```bash
git clone <repo> && cd go-learn
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Config
cp config/harness.example.yaml config/harness.yaml
#   sửa: provider (GLM/MiniMax), projects (đường dẫn /mnt/... của bạn), egress allowlist
$EDITOR config/harness.yaml

# Secret (KHÔNG để trong file config)
export YETT_SECRET_LLM_KEY="<api-key-cua-ban>"

# Kiểm tra
yett demo                      # turn offline, không cần key
yett chat "xin chào"           # gọi provider thật (cần key)
```

## Lệnh thường dùng
```bash
yett chat "báo cáo tiến độ tuần này"
yett traces list --state state
yett traces get <trace-id> --state state
yett usage --by provider --state state
```

## Lưu ý WSL2 (từ P0.5.1)
- **Hiệu năng file:** project nằm trên `/mnt/c` hoặc `/mnt/d` (NTFS) sẽ chậm với git/exec.
  Nếu chậm, cân nhắc clone project hay dùng vào ext4 của WSL (`~/work/...`) và trỏ `projects.path` vào đó.
- **VPN:** khai `remote.vpn_profiles` (`openfortivpn`/`openvpn`), đặt secret, rồi
  chạy `yett vpn connect <profile>` ở foreground (giữ terminal mở; `Ctrl+C` ngắt process
  được sở hữu) hoặc để SSH tự pre-connect khi `host.vpn_required` khớp. Không dùng một lần
  CLI khác để nhận nuôi/status/kill PID tunnel. Runtime VPN chỉ hỗ trợ POSIX/WSL2.
  Nếu FortiClient chạy trên Windows host, kiểm tra traffic SSH từ WSL2 có đi qua tunnel
  không; nếu không, dùng `openfortivpn` trong WSL2.
- **Docker:** bật "WSL integration" trong Docker Desktop Settings cho distro đang dùng.

## Backup / Restore
```bash
bash deploy/backup.sh   ./backups           # tạo backup state + workspace + config
bash deploy/restore.sh  ./backups/<file>    # khôi phục
```

## Nâng cấp
```bash
git pull && pip install -e .    # dữ liệu (state/, workspace/) giữ nguyên; chạy pytest -q để kiểm
```
