---
name: report
description: Tổng hợp báo cáo tiến độ các project (đọc git log + PROJECT.md/TODO trong workspace) thành markdown. Dùng khi người dùng hỏi tuần này project đi đến đâu, cái gì trễ.
version: 1.0
---
# Skill: Báo cáo tiến độ project

Khi được yêu cầu tổng hợp tiến độ:

1. Với mỗi project đã đăng ký trong config, dùng `exec` chạy `git -C <path> log --oneline --since="1 week ago"` để lấy hoạt động tuần.
2. Đọc `PROJECT.md`, `TODO.md`, `CHANGELOG.md` (nếu có) bằng `read_file`.
3. Tổng hợp thành báo cáo markdown: mỗi project một mục — việc đã xong, đang làm, đang trễ (so hạn trong TODO), rủi ro.
4. Ghi kết quả vào `workspace/reports/YYYY-MM-DD.md` bằng `write_file`.

Giữ báo cáo ngắn gọn, ưu tiên việc trễ và blocker lên đầu.
