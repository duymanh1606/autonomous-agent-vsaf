/**
 * Nhận dữ liệu từ backend (integrations/sheet_export.py) qua POST JSON và
 * ghi/update vào sheet "Khách hàng" (cấu trúc do setupKhachHangSheet() dựng),
 * khớp dòng theo "Mã hội thoại" (cột M, ẩn = conversation_id nội bộ). Cột A
 * hiện tên shop (workspace) đang chăm sóc khách, gửi kèm mọi payload.
 *
 * File này CHỈ chứa doPost() và phần hỗ trợ - dán thêm vào cùng project Apps
 * Script đã có setupKhachHangSheet()/onEdit() (setup_khach_hang_sheet.gs),
 * không thay thế file đó.
 *
 * Triển khai:
 *   1. Trong project Apps Script gắn với spreadsheet: + > Tập lệnh, dán file
 *      này. Ô chọn hàm trên thanh công cụ phải hiện "doPost" - nếu không,
 *      file chưa nằm trong project và Web App sẽ báo "Không tìm thấy hàm
 *      tập lệnh: doPost".
 *   2. Cài đặt dự án (bánh răng) > Thuộc tính tập lệnh > Thêm: tên
 *      WEBHOOK_TOKEN, giá trị = GSHEET_WEBHOOK_TOKEN trong .env.
 *   3. Triển khai > Lần triển khai mới > Ứng dụng web (Thực thi: Tôi; Truy
 *      cập: Bất kỳ ai) > copy URL /exec vào GSHEET_WEBHOOK_URL trong .env.
 *   4. Sau MỖI lần sửa code: Quản lý các lần triển khai > bút chì > Phiên
 *      bản: Phiên bản mới > Triển khai. URL /exec phục vụ phiên bản đã
 *      đóng băng, không phải code đang mở trong editor.
 *   Kiểm tra nhanh: POST với token sai phải trả về
 *   {"ok":false,"error":"invalid token"}. Chạy webhook_receiver.test.js để
 *   test logic offline.
 *
 * Payload gửi lên có 2 dạng, phân biệt bằng field "type":
 *   Mọi payload: shop, conversation_id.
 *   - "customer": conversation_id, id_tiktok, customer_id, customer_name,
 *                 phone, chat_address, stage, intent, product_name,
 *                 updated_at  (product_name đã ghép size/số lượng, vd
 *                 "Áo Polo – size L – x2")
 *   - "order":    conversation_id, id_tiktok, customer_name, order_status,
 *                 order_items, order_total, shipping_address, updated_at
 *
 * Cột E "Địa chỉ giao hàng" (trước đây là Email): địa chỉ trên đơn hàng
 * (shipping_address) luôn ghi đè; địa chỉ khách nhắn trong chat
 * (chat_address) chỉ được ghi khi ô còn TRỐNG. Dữ liệu "customer" được gửi
 * lại sau mỗi lần agent quét inbox - nếu nó được ghi đè thì địa chỉ trong
 * chat sẽ đè lên địa chỉ trên đơn.
 *
 * Quy tắc ghi: một trường rỗng/null trong payload nghĩa là "chưa biết ở lần
 * cập nhật này" - KHÔNG ghi đè lên giá trị đã có sẵn trên sheet. Nếu ghi đè
 * vô điều kiện, một sự kiện chat tới sau (chưa scrape được tên khách) sẽ xoá
 * mất tên đã có từ một đơn hàng trước đó, và ngược lại.
 */

const SHEET_NAME = "Khách hàng";
// Script Property WEBHOOK_TOKEN (Cài đặt dự án > Thuộc tính tập lệnh) phải
// khớp GSHEET_WEBHOOK_TOKEN trong .env của backend. Không hardcode ở đây để
// token không lọt vào git.
const WEBHOOK_TOKEN = PropertiesService.getScriptProperties().getProperty("WEBHOOK_TOKEN");

const COL = {
  SHOP: 1,
  ID_TIKTOK: 2,
  CUSTOMER_NAME: 3,
  PHONE: 4,
  SHIPPING_ADDRESS: 5,
  STAGE: 6,
  INTENT: 7,
  PRODUCT_INTEREST: 8,
  ORDER_STATUS: 9,
  ORDER_ITEMS: 10,
  ORDER_TOTAL: 11,
  UPDATED_AT: 12,
  // Khoá khớp dòng. Trước đây là cột A; sheet cũ phải chuyển (xem
  // docs/google-sheet-export.md) nếu không mỗi khách sẽ bị tạo dòng mới.
  CONVERSATION_ID: 13,
};

// CustomerState.stage (models/customer_state.py) -> nhãn dropdown cột F.
// Dropdown chỉ có 5 lựa chọn trong khi backend có 11 stage - đây là một cách
// gộp hợp lý, chỉnh lại nếu muốn ánh xạ khác.
const STAGE_LABELS = {
  new: "Mới tiếp cận",
  browsing: "Mới tiếp cận",
  asking_product: "Đang tư vấn",
  asking_price: "Báo giá",
  checking_inventory: "Đang tư vấn",
  interested: "Đang tư vấn",
  ordering: "Đã chốt",
  after_sales: "Đã chốt",
  complaint: "Thất bại",
  human_required: "Đang tư vấn",
  closed: "Đã chốt",
};

// dashboard_api/orders.py: VALID_STATUSES -> nhãn dropdown cột I. Dropdown
// chỉ có 4 lựa chọn trong khi backend có 6 trạng thái - "packing" gộp vào
// "Chờ xử lý", "returned" gộp vào "Đã hủy"; chỉnh lại nếu muốn tách riêng.
const ORDER_STATUS_LABELS = {
  pending: "Chờ xử lý",
  packing: "Chờ xử lý",
  shipping: "Đang giao",
  done: "Hoàn thành",
  returned: "Đã hủy",
  cancelled: "Đã hủy",
};

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (!WEBHOOK_TOKEN) {
      return jsonResponse_({ ok: false, error: "WEBHOOK_TOKEN script property not set" });
    }
    if (body.token !== WEBHOOK_TOKEN) {
      return jsonResponse_({ ok: false, error: "invalid token" });
    }
    const conversationId = body.conversation_id;
    if (!conversationId) {
      return jsonResponse_({ ok: false, error: "missing conversation_id" });
    }

    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
    if (!sheet) {
      return jsonResponse_({ ok: false, error: "sheet not found: " + SHEET_NAME });
    }

    // Nhiều request có thể tới gần như đồng thời (một chat vừa nhắn vừa lên
    // đơn) - khoá lại trong lúc tìm/ghi dòng để hai request không cùng tạo
    // hai dòng mới cho cùng một mã hội thoại.
    const lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      const row = findOrCreateRow_(sheet, conversationId);
      applyPayload_(sheet, row, body);
    } finally {
      lock.releaseLock();
    }

    return jsonResponse_({ ok: true });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err) });
  }
}

function findOrCreateRow_(sheet, conversationId) {
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const ids = sheet.getRange(2, COL.CONVERSATION_ID, lastRow - 1, 1).getValues();
    for (let i = 0; i < ids.length; i++) {
      if (String(ids[i][0]) === String(conversationId)) {
        return i + 2; // +2: bù hàng tiêu đề + getValues() 0-index
      }
    }
  }
  const newRow = lastRow + 1;
  sheet.getRange(newRow, COL.CONVERSATION_ID).setValue(conversationId);
  return newRow;
}

function setIfPresent_(sheet, row, col, value) {
  if (value === undefined || value === null || value === "") return;
  sheet.getRange(row, col).setValue(value);
}

// Chỉ ghi khi ô đang trống - cho dữ liệu "dự phòng" không được đè lên dữ
// liệu chính thức (địa chỉ chat so với địa chỉ trên đơn).
function setIfEmpty_(sheet, row, col, value) {
  if (value === undefined || value === null || value === "") return;
  const cell = sheet.getRange(row, col);
  if (String(cell.getValue()).trim() !== "") return;
  cell.setValue(value);
}

function applyPayload_(sheet, row, body) {
  setIfPresent_(sheet, row, COL.SHOP, body.shop);
  setIfPresent_(sheet, row, COL.ID_TIKTOK, body.id_tiktok);
  setIfPresent_(sheet, row, COL.CUSTOMER_NAME, body.customer_name);

  if (body.type === "customer") {
    setIfPresent_(sheet, row, COL.PHONE, body.phone);
    setIfEmpty_(sheet, row, COL.SHIPPING_ADDRESS, body.chat_address);
    setIfPresent_(sheet, row, COL.STAGE, STAGE_LABELS[body.stage] || body.stage);
    setIfPresent_(sheet, row, COL.INTENT, body.intent);
    setIfPresent_(sheet, row, COL.PRODUCT_INTEREST, body.product_name);
  } else if (body.type === "order") {
    setIfPresent_(
      sheet, row, COL.ORDER_STATUS,
      ORDER_STATUS_LABELS[body.order_status] || body.order_status,
    );
    setIfPresent_(sheet, row, COL.ORDER_ITEMS, body.order_items);
    setIfPresent_(sheet, row, COL.ORDER_TOTAL, body.order_total);
    setIfPresent_(sheet, row, COL.SHIPPING_ADDRESS, body.shipping_address);
  }

  const updatedCell = sheet.getRange(row, COL.UPDATED_AT);
  updatedCell.setValue(body.updated_at ? new Date(body.updated_at) : new Date());
  updatedCell.setNumberFormat("dd/mm/yyyy hh:mm");
}

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
