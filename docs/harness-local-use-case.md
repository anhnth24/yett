# Use-case Local — Trợ lý DevOps cá nhân (mục tiêu dogfood của v0.2)

> **Trạng thái:** Draft để review · 04/07/2026
> **Vai trò tài liệu:** đặc tả use-case ĐẦU TIÊN của harness — chạy local trên máy của người dùng, quản lý các project cá nhân, báo cáo tiến độ, code, deploy UAT, điều tra lỗi, kiểm tra dữ liệu DB. Use-case này là **kịch bản dogfood chính thức của RG-2** trong `harness-execution-plan.md`, đồng thời là khách hàng số 0 của sản phẩm.
> **Nguyên tắc không đổi:** dù chạy local cho một người, mọi tool call vẫn xuyên Policy Gate fail-closed — use-case này chính là nơi kiểm chứng mô hình security với hệ thống thật (server UAT, DB thật).

---

## 1. Năm kịch bản

| # | Kịch bản | Ví dụ lệnh người dùng |
|---|---|---|
| S1 | **Quản lý project + báo cáo tiến độ** | "tổng hợp tuần này các project đi đến đâu, cái gì trễ" — agent đọc workspace từng project (PROJECT.md, git log, TODO), sinh báo cáo markdown; cron gửi báo cáo sáng thứ 2 |
| S2 | **Code** | như hiện tại: đọc/sửa code, chạy test trong sandbox Docker local |
| S3 | **Deploy UAT** | "deploy project X lên UAT" — agent bật VPN (FortiClient/OpenVPN) nếu cần → SSH vào server → chạy deploy script. **Luôn qua approval** |
| S4 | **Điều tra lỗi trên server → đề xuất fix** | "UAT đang lỗi 500, xem log rồi đề xuất fix" — agent SSH đọc log (read-only), phân tích, đề xuất patch; việc *sửa* là S2 + *deploy lại* là S3 |
| S5 | **Query DB kiểm tra dữ liệu** | "kiểm tra đơn hàng #123 trong DB UAT sao trạng thái sai" — agent lấy connection string từ config (hoặc người dùng cài qua lệnh), chạy SELECT, đối chiếu. **Tuyệt đối không ALTER/DELETE/UPDATE nếu không được phép tường minh** |

## 2. Tool mới cần thêm

| Tool | Kịch bản | Mô tả | Phase |
|---|---|---|---|
| `ssh_exec` | S3, S4 | Chạy lệnh trên server qua SSH backend (vendor `ssh.py` từ Hermes — bổ sung vào vendor intake P0.2.1) | v0.2 |
| `vpn` (connect/disconnect/status) | S3, S4, S5 | Bọc CLI của OpenVPN và Fortinet client. **[Inference — verify khi implement]** với FortiClient nên đánh giá `openfortivpn` (OSS client cho Fortinet SSL VPN) vì FortiClient chính hãng hạn chế CLI tùy nền tảng. Credentials từ secret store, **không bao giờ đi vào context của model** | v0.2 |
| `db_query` | S5 | Query DB qua connection profile; xem §4 — tool có hardline rule riêng | v0.2 |
| `db_config` | S5 | Cài đặt/liệt kê connection profile (connection string lưu vào secret store, model chỉ thấy tên profile) | v0.2 |
| `log_read` | S4 | Đường tắt an toàn của ssh_exec: tail/grep các đường dẫn log khai báo trước theo host profile | v0.2 |
| `report` (skill, không phải tool) | S1 | Skill tổng hợp báo cáo từ workspace các project + git log; chạy tay hoặc qua cron | v0.2 |

S1, S2 không cần tool mới — chạy trên nền workspace files + skills + scheduler + sandbox đã có trong plan.

## 3. Mô hình an toàn cho remote ops (SSH/VPN)

**Host profile trong config** — SSH chỉ đến host khai báo trước, mỗi host gắn mức quyền:

```yaml
remote:
  hosts:
    uat-app-1:
      address: 10.x.x.x
      auth: keyfile:~/.ssh/uat_key
      vpn_required: fortivpn-office        # tự bật VPN profile này trước khi SSH
      tier: uat                            # uat | restricted
      log_paths: [/var/log/app/*.log, journalctl:app.service]
      deploy_script: /opt/deploy/run.sh    # lệnh deploy DUY NHẤT được phép
```

**Phân lớp lệnh SSH qua Policy Gate:**

| Lớp | Ví dụ | Quyết định |
|---|---|---|
| **Hardline: xóa file OS** | `rm`, `rmdir`, `unlink`, `shred`, `find … -delete` / `-exec rm`, `truncate -s 0`, `dd of=`, `mv`/`cp` đè lên file có sẵn, redirect ghi đè (`>`, `tee`) ra ngoài thư mục tạm | **DENY TUYỆT ĐỐI — không có đường approval.** Áp cho MỌI lệnh qua `ssh_exec`/`log_read` và mọi phiên có VPN đang bật |
| Read-only quan sát | `tail/grep/cat` trên `log_paths`, `systemctl status`, `docker ps/logs`, `df -h` | Auto-allow (allowlist theo host profile) |
| Deploy | đúng `deploy_script` khai báo, đúng tham số dạng khai báo | **Approval từng lần** — hiện nguyên văn lệnh + host cho người dùng duyệt |
| Mọi thứ khác | sửa file trên server, restart tùy ý, lệnh không khớp lớp nào | **DENY mặc định** (muốn mở → thêm vào allowlist config, không mở runtime) |

**Chi tiết hardline xóa-file-remote:**
- Không có ngoại lệ runtime: kể cả người dùng gõ "cứ rm đi" trong chat, Gate vẫn chặn — vì lệnh xóa hợp lệ duy nhất trên server là thứ nằm **bên trong `deploy_script`** do chính người dùng viết và khai báo trước trong config (script là code được review, không phải lệnh agent tự ghép).
- Chặn theo **phân loại lệnh sau khi parse**, không chỉ match chuỗi: bộ test né tránh bắt buộc gồm `xargs rm`, `busybox rm`, `$(echo rm)`, alias/function bọc rm, `bash -c "rm …"`, `find -delete`, wildcard expansion — tất cả phải bị chặn; parse không được → DENY (fail-closed, nhất quán với SQL classifier §4).
- Phạm vi "remote": mọi lệnh chạy qua `ssh_exec`/`log_read`, và mọi lệnh local phát sinh trong lúc VPN đang bật mà đích là filesystem mount/share từ xa **[Inference — nhận diện mount từ xa chốt khi design chi tiết]**.
- Xóa file **local trong workspace/sandbox** không thuộc rule này (đã có Policy Gate + container cô lập quản); rule này bảo vệ server.

- Host không có trong config → deny (không có chế độ "ssh đại").
- `tier: restricted` (nếu sau này thêm host prod): chỉ lớp read-only, không deploy, không approval override. **[Inference]** — phạm vi hiện tại là UAT, prod chưa nằm trong use-case.
- VPN: bật/tắt là tool call bình thường (có span + audit); sai/hết hạn credentials → lỗi trả về agent, không bao giờ hỏi model điền password.

## 4. Hardline rule cho DB — "tuyệt đối không ALTER/DELETE nếu không được phép"

Yêu cầu này map thẳng vào Policy Gate, thực thi **phòng thủ 4 lớp, fail-closed** — không lớp nào một mình được tin:

| Lớp | Cơ chế | Ghi chú |
|---|---|---|
| L1 — Quyền ở DB | Connection profile khuyến nghị dùng **DB user read-only** do DBA cấp | Mạnh nhất, nhưng không phụ thuộc — vì user có thể chỉ có sẵn account thường |
| L2 — Session read-only | Mở session ở chế độ read-only theo từng loại DB (vd `SET TRANSACTION READ ONLY`, `default_transaction_read_only`, `ApplicationIntent=ReadOnly` **[Inference — mapping cụ thể từng DBMS chốt khi implement]**) | Áp dụng mặc định cho mọi profile |
| L3 — SQL classifier trong Policy Gate | Parse câu lệnh (sqlglot **[Inference — chọn lib khi implement]**): chỉ cho qua `SELECT` / `SHOW` / `EXPLAIN` / `DESCRIBE`. Multi-statement (`;`), CTE chứa DML, `SELECT ... INTO`, stored procedure gọi write → **DENY**. **Parse không được → DENY** (fail-closed, không đoán) | Đây là hardline: `ALTER`, `DROP`, `TRUNCATE`, `DELETE`, `UPDATE`, `INSERT`, `CREATE`, `GRANT` nằm trong deny-list không override được bằng config thường |
| L4 — Approval tường minh cho write | Muốn cho phép một câu write cụ thể: người dùng duyệt **từng câu lệnh** qua approval flow — hiển thị nguyên văn SQL + profile + bảng bị ảnh hưởng; approve chỉ có hiệu lực cho đúng câu đó, một lần. Không tồn tại chế độ "cho phép write cả phiên" | `ALTER`/`DROP`/`TRUNCATE` ngay cả khi approve vẫn yêu cầu gõ lại tên bảng để xác nhận **[Inference — cơ chế xác nhận chốt khi design chi tiết]** |

**Bổ sung:**
- Mọi query (kể cả bị deny) ghi audit: profile, SQL nguyên văn, verdict, người approve nếu có.
- Kết quả query đi qua Result Filters như mọi tool result (redact cột nhạy cảm theo config **[Inference]**).
- Connection string nằm trong secret store; model chỉ nhìn thấy tên profile — không bao giờ thấy password trong context.
- Test bắt buộc (vào AG-suite): bộ case né tránh — SQL obfuscated (comment chèn giữa `AL/**/TER`), multi-statement, DML trong CTE, lowercase/unicode — tất cả phải bị chặn; đây là tiêu chí RG-2 mới (RG2-8).

## 5. Thay đổi tương ứng trong plan

| Thay đổi | Ở đâu |
|---|---|
| Vendor intake thêm `ssh.py` | P0.2.1 (`harness-execution-plan.md`) |
| Thêm WP2.6 (Remote Ops: ssh_exec + vpn + log_read + host profile) và WP2.7 (DB: db_query + db_config + SQL classifier 4 lớp) | Phase 2 — kéo dài Phase 2 ~6→8 tuần **[Inference]** |
| RG-2 dogfood định nghĩa lại = chính 5 kịch bản S1–S5 chạy thật trên project/server/DB của người dùng | RG2-1 |
| Thêm RG2-8 (bộ test né tránh SQL classifier xanh) và AG-6 (DB hardline suite thường trực) | RG-2, §1 execution plan |
| Thêm RG2-9 (bộ test né tránh hardline xóa-file-remote xanh) và AG-7 (remote-deletion hardline suite thường trực) | RG-2, §1 execution plan |
| Kiến trúc: sandbox vendor giữ thêm `ssh.py`; Tool Runtime thêm nhóm remote-ops/db | `harness-architecture-design.md` §3.6–3.7 |

**Điều KHÔNG đổi:** kiến trúc, thứ tự phase, các gate khác. Use-case local chạy trên đúng core đã thiết kế — khác biệt duy nhất là bộ tool + config + skill, đúng như kỳ vọng của thiết kế (mọi năng lực mới = tool qua Gate, không đục core).

## 6. Trải nghiệm mục tiêu (định nghĩa "chạy được" cho từng kịch bản)

- **S1:** `harness chat` → "báo cáo tuần" → nhận markdown tổng hợp N project trong <2 phút; cron thứ 2 tự sinh báo cáo vào `workspace/reports/`.
- **S3:** "deploy X lên UAT" → agent: bật VPN (nếu chưa) → hỏi approval với nguyên văn lệnh + host → chạy → báo kết quả kèm log đuôi → tắt VPN nếu do nó bật. Toàn bộ có trace.
- **S4:** "xem lỗi UAT" → agent đọc log qua lớp read-only (không cần approval) → khoanh vùng nguyên nhân → đề xuất patch dạng diff. Người dùng duyệt thì agent sửa (S2) và hỏi deploy (S3).
- **S5:** "check dữ liệu đơn #123" → agent chọn profile, chạy SELECT, trình bày kết quả. Nếu agent thử UPDATE (kể cả do được yêu cầu mơ hồ) → Gate chặn, agent báo lại "cần bạn approve tường minh câu lệnh này" kèm nguyên văn SQL.

---

*Tài liệu này là đặc tả use-case; design chi tiết của WP2.6/WP2.7 viết khi bắt đầu Phase 2. Các thay đổi plan ở §5 đã được phản ánh vào `harness-execution-plan.md`.*
