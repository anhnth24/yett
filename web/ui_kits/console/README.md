# UI kit — yett Console

Màn **Tổng quan** (bố cục quỹ đạo) của yett Console, compose từ component primitives của design system (`Button`, `Card`, `Badge`, `Tag`, `Tabs`, `ApprovalBanner`).

- `index.html` — bản interactive: duyệt/từ chối approval hoạt động, switcher bố cục là `Tabs` (chỉ demo state, không đổi layout).
- Nguồn: import từ Claude Design project `329c9948` (`ui_kits/console/index.html`).

## Chạy

Loader nạp các `.jsx` qua `fetch()`, nên phải phục vụ qua HTTP (mở trực tiếp `file://` sẽ bị chặn fetch):

```bash
# từ thư mục web/
python -m http.server 8000
# rồi mở http://localhost:8000/ui_kits/console/index.html
```

React + Babel tải từ unpkg CDN và font Inter từ Google Fonts — cần mạng lần đầu.

## Ghi chú

Ảnh planet (`assets/planet-*.svg`) là sphere vector thay cho `.png` raster trong design gốc — nhẹ, hợp theme. Thay lại bằng PNG nếu cần đúng art gốc.
