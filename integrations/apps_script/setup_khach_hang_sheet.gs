/**
 * ============================================================
 * QUẢN LÝ KHÁCH HÀNG - GOOGLE SHEETS
 * Giao diện Light / Modern CRM
 * ============================================================
 *
 * Dựng sheet "Khách hàng" (13 cột, dropdown, conditional formatting) mà
 * webhook_receiver.gs ghi dữ liệu vào. Cột A là tên shop (workspace) đang
 * chăm sóc khách; cột M "Mã hội thoại" là khoá để webhook tìm đúng dòng -
 * được ẩn đi, đừng xoá hay sửa tay. Chạy setupKhachHangSheet() một lần
 * từ editor; onEdit() là simple trigger, tự chạy khi người dùng sửa tay.
 */

function setupKhachHangSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName("Khách hàng");

  if (!sheet) {
    sheet = ss.insertSheet("Khách hàng");
  }

  // ==========================================================
  // 1. CẤU TRÚC 13 CỘT (M = mã hội thoại, ẩn)
  // ==========================================================

  const headers = [
    "Shop",
    "ID Tiktok",
    "Khách hàng",
    "SĐT",
    "Địa chỉ giao hàng",
    "Giai đoạn",
    "Ý định",
    "Sp quan tâm",
    "Trạng thái đơn",
    "Sản phẩm đơn",
    "Tổng tiền",
    "Cập nhập lúc",
    "Mã hội thoại"
  ];

  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);

  // ==========================================================
  // 2. HEADER - SÁNG, HIỆN ĐẠI
  // ==========================================================

  const headerRange = sheet.getRange(1, 1, 1, headers.length);

  headerRange
    .setBackground("#2563EB")
    .setFontColor("#FFFFFF")
    .setFontWeight("bold")
    .setFontSize(10)
    .setHorizontalAlignment("center")
    .setVerticalAlignment("middle")
    .setWrap(true);

  sheet.setRowHeight(1, 42);
  sheet.setFrozenRows(1);

  // ==========================================================
  // 3. DỮ LIỆU MẪU
  // ==========================================================

  const sampleData = [
    [
      "Shop Thời Trang A",
      "@nguyenvan_a",
      "Nguyễn Văn A",
      "0901234567",
      "12 Lê Lợi, Q.1, TP.HCM",
      "Đã chốt",
      "Mua hàng",
      "Áo thun Polo",
      "Hoàn thành",
      "Áo thun Polo Regular Fit (L)",
      250000,
      "2026-09-15 08:30:00",
      "HT-00101"
    ],
    [
      "Shop Thời Trang A",
      "@tran_thi_b",
      "Trần Thị B",
      "0912345678",
      "45 Trần Phú, Hà Đông, Hà Nội",
      "Đang tư vấn",
      "Hỏi giá",
      "Đầm voan hoa",
      "Chờ xử lý",
      "Đầm voan hoa tay lỡ (M)",
      380000,
      "2026-09-15 09:15:00",
      "HT-00102"
    ],
    [
      "Shop Thời Trang B",
      "@lehoang_c",
      "Lê Hoàng C",
      "0983456789",
      "8 Nguyễn Văn Linh, Hải Châu, Đà Nẵng",
      "Báo giá",
      "Tư vấn size",
      "Quần Jean Slim",
      "Chờ xử lý",
      "Quần Jean Slimfit đen (32)",
      420000,
      "2026-09-15 10:00:00",
      "HT-00103"
    ],
    [
      "Shop Thời Trang A",
      "@pham_minh_d",
      "Phạm Minh D",
      "0974567890",
      "102 Lý Thường Kiệt, Q.10, TP.HCM",
      "Mới tiếp cận",
      "Tìm hiểu sản phẩm",
      "Áo khoác gió",
      "Chờ xử lý",
      "Áo khoác gió thể thao",
      320000,
      "2026-09-15 11:20:00",
      "HT-00104"
    ],
    [
      "Shop Thời Trang B",
      "@vu_thu_e",
      "Vũ Thu E",
      "0935678901",
      "27 Hùng Vương, TP. Huế",
      "Đã chốt",
      "Mua hàng",
      "Chân váy chữ A",
      "Đang giao",
      "Chân váy chữ A caro (S)",
      290000,
      "2026-09-15 13:45:00",
      "HT-00105"
    ],
    [
      "Shop Thời Trang B",
      "@dang_khoa_f",
      "Đặng Tuấn Khôi",
      "0946789012",
      "3 Phan Chu Trinh, Hoàn Kiếm, Hà Nội",
      "Thất bại",
      "Hủy đơn",
      "Giày sneaker",
      "Đã hủy",
      "Giày sneaker trắng (41)",
      550000,
      "2026-09-15 14:10:00",
      "HT-00106"
    ]
  ];

  sheet
    .getRange(2, 1, sampleData.length, headers.length)
    .setValues(sampleData);

  // ==========================================================
  // 4. FORMAT DỮ LIỆU
  // ==========================================================

  const numRows = 50;

  const dataRange = sheet.getRange(
    2,
    1,
    numRows,
    headers.length
  );

  dataRange
    .setVerticalAlignment("middle")
    .setFontSize(10)
    .setFontColor("#263238");

  // Shop
  sheet
    .getRange(2, 1, numRows, 1)
    .setHorizontalAlignment("left");

  // Mã hội thoại (khoá của webhook, ẩn)
  sheet
    .getRange(2, 13, numRows, 1)
    .setNumberFormat("@")
    .setHorizontalAlignment("center");

  // TikTok ID
  sheet
    .getRange(2, 2, numRows, 1)
    .setHorizontalAlignment("center");

  // Khách hàng
  sheet
    .getRange(2, 3, numRows, 1)
    .setHorizontalAlignment("left")
    .setFontWeight("bold");

  // SĐT
  sheet
    .getRange(2, 4, numRows, 1)
    .setNumberFormat("@")
    .setHorizontalAlignment("center");

  // Địa chỉ giao hàng
  sheet
    .getRange(2, 5, numRows, 1)
    .setHorizontalAlignment("left")
    .setWrap(true);

  // Giai đoạn
  sheet
    .getRange(2, 6, numRows, 1)
    .setHorizontalAlignment("center");

  // Ý định
  sheet
    .getRange(2, 7, numRows, 1)
    .setHorizontalAlignment("left");

  // Sản phẩm quan tâm
  sheet
    .getRange(2, 8, numRows, 1)
    .setHorizontalAlignment("left");

  // Trạng thái đơn
  sheet
    .getRange(2, 9, numRows, 1)
    .setHorizontalAlignment("center");

  // Sản phẩm đơn
  sheet
    .getRange(2, 10, numRows, 1)
    .setHorizontalAlignment("left");

  // Tổng tiền
  sheet
    .getRange(2, 11, numRows, 1)
    .setNumberFormat('#,##0 "₫"')
    .setHorizontalAlignment("right");

  // Thời gian
  sheet
    .getRange(2, 12, numRows, 1)
    .setNumberFormat("yyyy-mm-dd hh:mm:ss")
    .setHorizontalAlignment("center");

  // ==========================================================
  // 5. DROPDOWN - GIAI ĐOẠN
  // ==========================================================

  const giaiDoanRule = SpreadsheetApp
    .newDataValidation()
    .requireValueInList(
      [
        "Mới tiếp cận",
        "Đang tư vấn",
        "Báo giá",
        "Đã chốt",
        "Thất bại"
      ],
      true
    )
    .setAllowInvalid(true)
    .build();

  sheet
    .getRange(2, 6, numRows, 1)
    .setDataValidation(giaiDoanRule);

  // ==========================================================
  // 6. DROPDOWN - TRẠNG THÁI ĐƠN
  // ==========================================================

  const trangThaiRule = SpreadsheetApp
    .newDataValidation()
    .requireValueInList(
      [
        "Chờ xử lý",
        "Đang giao",
        "Hoàn thành",
        "Đã hủy"
      ],
      true
    )
    .setAllowInvalid(true)
    .build();

  sheet
    .getRange(2, 9, numRows, 1)
    .setDataValidation(trangThaiRule);

  // ==========================================================
  // 7. CONDITIONAL FORMATTING - MÀU PASTEL
  // ==========================================================

  const rules = [];

  // ----------------------------------------------------------
  // GIAI ĐOẠN
  // ----------------------------------------------------------

  // Đã chốt - xanh pastel
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Đã chốt")
      .setBackground("#E8F5E9")
      .setFontColor("#2E7D32")
      .setBold(true)
      .setRanges([sheet.getRange("F2:F50")])
      .build()
  );

  // Đang tư vấn - vàng pastel
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Đang tư vấn")
      .setBackground("#FFF8E1")
      .setFontColor("#8D6E00")
      .setBold(true)
      .setRanges([sheet.getRange("F2:F50")])
      .build()
  );

  // Báo giá - tím pastel
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Báo giá")
      .setBackground("#F3E5F5")
      .setFontColor("#6A1B9A")
      .setBold(true)
      .setRanges([sheet.getRange("F2:F50")])
      .build()
  );

  // Mới tiếp cận - xanh cyan pastel
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Mới tiếp cận")
      .setBackground("#E0F7FA")
      .setFontColor("#00695C")
      .setBold(true)
      .setRanges([sheet.getRange("F2:F50")])
      .build()
  );

  // Thất bại - đỏ pastel
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Thất bại")
      .setBackground("#FFEBEE")
      .setFontColor("#C62828")
      .setBold(true)
      .setRanges([sheet.getRange("F2:F50")])
      .build()
  );

  // ----------------------------------------------------------
  // TRẠNG THÁI ĐƠN
  // ----------------------------------------------------------

  // Hoàn thành
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Hoàn thành")
      .setBackground("#E8F5E9")
      .setFontColor("#2E7D32")
      .setBold(true)
      .setRanges([sheet.getRange("I2:I50")])
      .build()
  );

  // Đang giao
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Đang giao")
      .setBackground("#E3F2FD")
      .setFontColor("#1565C0")
      .setBold(true)
      .setRanges([sheet.getRange("I2:I50")])
      .build()
  );

  // Chờ xử lý
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Chờ xử lý")
      .setBackground("#FFF8E1")
      .setFontColor("#8D6E00")
      .setBold(true)
      .setRanges([sheet.getRange("I2:I50")])
      .build()
  );

  // Đã hủy
  rules.push(
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("Đã hủy")
      .setBackground("#FFEBEE")
      .setFontColor("#C62828")
      .setBold(true)
      .setRanges([sheet.getRange("I2:I50")])
      .build()
  );

  sheet.setConditionalFormatRules(rules);

  // ==========================================================
  // 8. BANDING - CHỈ ÁP DỤNG CHO PHẦN DỮ LIỆU
  // Không áp dụng lên header để tránh ghi đè màu xanh
  // ==========================================================

  const fullTable = sheet.getRange(
    1,
    1,
    numRows + 1,
    headers.length
  );

  // Xóa banding cũ
  fullTable
    .getBandings()
    .forEach(banding => banding.remove());

  // Chỉ banding từ dòng 2 trở đi
  const bodyRange = sheet.getRange(
    2,
    1,
    numRows,
    headers.length
  );

  bodyRange.applyRowBanding(
    SpreadsheetApp.BandingTheme.LIGHT_GREY,
    false,
    false
  );

  // ==========================================================
  // 9. KÍCH THƯỚC CỘT
  // ==========================================================

  const columnWidths = [
    150, // Shop
    130, // TikTok
    160, // Khách hàng
    120, // SĐT
    260, // Địa chỉ giao hàng
    140, // Giai đoạn
    160, // Ý định
    150, // SP quan tâm
    140, // Trạng thái
    230, // Sản phẩm đơn
    120, // Tổng tiền
    175, // Cập nhập lúc
    110  // Mã hội thoại
  ];

  columnWidths.forEach((width, index) => {
    sheet.setColumnWidth(index + 1, width);
  });

  // Mã hội thoại chỉ để webhook khớp dòng - người xem không cần thấy.
  sheet.hideColumns(13);

  // ==========================================================
  // 10. BORDER NHẸ
  // ==========================================================

  dataRange.setBorder(
    true,
    true,
    true,
    true,
    true,
    true,
    "#E6EAF0",
    SpreadsheetApp.BorderStyle.SOLID
  );

  // Header border
  headerRange.setBorder(
    false,
    false,
    true,
    false,
    false,
    false,
    "#B8C7DC",
    SpreadsheetApp.BorderStyle.SOLID_MEDIUM
  );

  SpreadsheetApp.getUi().alert(
    "Hoàn tất! Bảng Khách hàng đã được thiết lập với giao diện sáng và hiện đại."
  );
}


/**
 * ============================================================
 * TỰ ĐỘNG CẬP NHẬT THỜI GIAN
 * ============================================================
 *
 * Khi chỉnh sửa A:K → tự động cập nhật cột L.
 * Chỉ chạy khi người dùng sửa tay - ghi từ doPost() (webhook_receiver.gs)
 * không kích hoạt simple trigger này, doPost tự ghi cột L.
 */
function onEdit(e) {

  if (!e || !e.range) return;

  const range = e.range;
  const sheet = range.getSheet();

  if (sheet.getName() !== "Khách hàng") return;

  const row = range.getRow();
  const col = range.getColumn();

  // Bỏ qua header
  if (row <= 1) return;

  // Chỉ xử lý A:K
  if (col >= 1 && col <= 11) {

    const timeCell = sheet.getRange(row, 12);

    const now = new Date();

    const formattedDate = Utilities.formatDate(
      now,
      "Asia/Ho_Chi_Minh",
      "yyyy-MM-dd HH:mm:ss"
    );

    timeCell.setValue(formattedDate);
  }
}
