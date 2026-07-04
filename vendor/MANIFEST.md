# Vendor Manifest

Ghi lại mọi code vendor từ dự án ngoài + license + commit SHA nguồn (spec P0.2, AG-3).
Quy tắc: chỉ vendor từ repo MIT; giữ nguyên copyright notice; ghi SHA để theo dõi upstream.

| Thư mục | Nguồn | License | Commit SHA | Ngày | Ghi chú |
|---|---|---|---|---|---|
| `hermes_environments/` | NousResearch/hermes-agent — `tools/environments/{base,local,docker,ssh,file_sync}.py` | MIT | _(điền khi intake P0.2.1)_ | — | Sandbox backend. Chỉ giữ base/local/docker/ssh/file_sync; bỏ singularity/modal/daytona |
| `hermes_state/` | NousResearch/hermes-agent — schema từ `hermes_state.py` | MIT | _(điền khi intake P0.2.2)_ | — | Chỉ schema SQLite FTS5 + trigram + migrations |

> ⚠️ **KHÔNG vendor từ GoClaw** (CC BY-NC 4.0) — chỉ học pattern từ docs qua clean-room.
> Định kỳ (hàng quý): diff upstream các file trên + theo dõi security advisory (đặc biệt Hermes issues #41/#7071 cho RPC).
