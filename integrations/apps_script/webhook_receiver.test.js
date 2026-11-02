// Mocks the Apps Script runtime (SpreadsheetApp, LockService, ContentService)
// so doPost() runs under plain Node with the payloads sheet_export.py sends.
// Run: node integrations/apps_script/webhook_receiver.test.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "webhook_receiver.gs"), "utf8");

function makeSheet() {
  // rows[0] is the header row (index 1 in 1-based sheet coordinates); data starts at row 2.
  const rows = [new Array(13).fill("")];
  return {
    getLastRow() { return rows.length; },
    getRange(row, col, numRows, numCols) {
      numRows = numRows || 1;
      numCols = numCols || 1;
      while (rows.length < row + numRows - 1) rows.push(new Array(13).fill(""));
      return {
        getValues() {
          const out = [];
          for (let r = 0; r < numRows; r++) {
            const line = [];
            for (let c = 0; c < numCols; c++) line.push(rows[row - 1 + r][col - 1 + c]);
            out.push(line);
          }
          return out;
        },
        getValue() { return rows[row - 1][col - 1]; },
        setValue(v) { rows[row - 1][col - 1] = v; },
        setNumberFormat() { /* no-op */ },
      };
    },
    _rows: rows,
  };
}

function makeSandbox(sheet, scriptToken) {
  return {
    PropertiesService: {
      getScriptProperties: () => ({ getProperty: (k) => (k === "WEBHOOK_TOKEN" ? scriptToken : null) }),
    },
    SpreadsheetApp: { getActiveSpreadsheet: () => ({ getSheetByName: () => sheet }) },
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
    ContentService: {
      MimeType: { JSON: "JSON" },
      createTextOutput: (s) => ({ _text: s, setMimeType() { return this; } }),
    },
  };
}

function runDoPost(sheet, payload, token, scriptToken = TOKEN) {
  const sandbox = makeSandbox(sheet, scriptToken);
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  const body = { token, ...payload };
  const result = sandbox.doPost({ postData: { contents: JSON.stringify(body) } });
  return JSON.parse(result._text);
}

const TOKEN = "test-token";
let failures = 0;
function check(name, fn) {
  try { fn(); console.log("PASS:", name); }
  catch (e) { failures++; console.log("FAIL:", name, "-", e.message); }
}

check("rejects invalid token", () => {
  const sheet = makeSheet();
  const res = runDoPost(sheet, { conversation_id: "c1", type: "customer" }, "wrong-token");
  assert.strictEqual(res.ok, false);
  assert.match(res.error, /token/);
});

check("rejects everything when WEBHOOK_TOKEN script property is unset", () => {
  const sheet = makeSheet();
  const res = runDoPost(sheet, { conversation_id: "c1", type: "customer" }, TOKEN, null);
  assert.strictEqual(res.ok, false);
  assert.match(res.error, /WEBHOOK_TOKEN/);
  assert.strictEqual(sheet.getLastRow(), 1);
});

check("rejects missing conversation_id", () => {
  const sheet = makeSheet();
  const res = runDoPost(sheet, { type: "customer" }, TOKEN);
  assert.strictEqual(res.ok, false);
  assert.match(res.error, /conversation_id/);
});

check("creates a new row for a new conversation_id (customer payload)", () => {
  const sheet = makeSheet();
  const res = runDoPost(sheet, {
    type: "customer", conversation_id: "c1", id_tiktok: "t1",
    customer_name: "Nguyen Van A", phone: "0900000000", email: "old@x.test",
    stage: "asking_price", intent: "buy", product_name: "Ao thun",
    updated_at: "2026-01-01T00:00:00Z",
  }, TOKEN);
  assert.strictEqual(res.ok, true);
  const row = sheet._rows[1]; // row 2 (1-based) -> index 1
  assert.strictEqual(row[12], "c1");           // conversation_id (column M)
  assert.strictEqual(row[1], "t1");            // id_tiktok
  assert.strictEqual(row[2], "Nguyen Van A");  // customer_name
  assert.strictEqual(row[3], "0900000000");    // phone
  assert.strictEqual(row[4], "");              // shipping address: never from a customer payload
  assert.strictEqual(row[5], "Báo giá");       // stage label mapped
  assert.strictEqual(row[6], "buy");           // intent
  assert.strictEqual(row[7], "Ao thun");       // product_name
});

check("second call with same conversation_id updates same row, doesn't create a new one", () => {
  const sheet = makeSheet();
  runDoPost(sheet, { type: "customer", conversation_id: "c1", customer_name: "A" }, TOKEN);
  runDoPost(sheet, { type: "customer", conversation_id: "c1", phone: "0911111111" }, TOKEN);
  assert.strictEqual(sheet.getLastRow(), 2); // header + 1 data row only
  const row = sheet._rows[1];
  assert.strictEqual(row[2], "A");            // name preserved from first call
  assert.strictEqual(row[3], "0911111111");   // phone added by second call
});

check("blank/null fields never overwrite an existing value", () => {
  const sheet = makeSheet();
  runDoPost(sheet, { type: "customer", conversation_id: "c1", customer_name: "A", phone: "090" }, TOKEN);
  runDoPost(sheet, { type: "customer", conversation_id: "c1", customer_name: "", phone: null }, TOKEN);
  const row = sheet._rows[1];
  assert.strictEqual(row[2], "A");
  assert.strictEqual(row[3], "090");
});

check("order payload maps order_status label and fills order columns", () => {
  const sheet = makeSheet();
  const res = runDoPost(sheet, {
    type: "order", conversation_id: "c2", id_tiktok: "t2", customer_name: "B",
    order_status: "packing", order_items: "2x Quan", order_total: 500000,
    shipping_address: "12 Lê Lợi, Q1",
    updated_at: "2026-01-02T00:00:00Z",
  }, TOKEN);
  assert.strictEqual(res.ok, true);
  const row = sheet._rows[1];
  assert.strictEqual(row[4], "12 Lê Lợi, Q1"); // column E: shipping address
  assert.strictEqual(row[8], "Chờ xử lý");   // packing -> "Chờ xử lý"
  assert.strictEqual(row[9], "2x Quan");
  assert.strictEqual(row[10], 500000);
});

check("customer then order for same conversation_id merge into one row", () => {
  const sheet = makeSheet();
  runDoPost(sheet, { type: "customer", conversation_id: "c3", customer_name: "C", stage: "interested" }, TOKEN);
  runDoPost(sheet, { type: "order", conversation_id: "c3", order_status: "done", order_total: 100 }, TOKEN);
  assert.strictEqual(sheet.getLastRow(), 2);
  const row = sheet._rows[1];
  assert.strictEqual(row[2], "C");
  assert.strictEqual(row[5], "Đang tư vấn"); // interested
  assert.strictEqual(row[8], "Hoàn thành");  // done
});

check("chat address fills an empty address cell", () => {
  const sheet = makeSheet();
  runDoPost(sheet, { type: "customer", conversation_id: "c4", chat_address: "5 Hai Bà Trưng, Hà Nội" }, TOKEN);
  assert.strictEqual(sheet._rows[1][4], "5 Hai Bà Trưng, Hà Nội");
});

check("order address overwrites a chat address, and chat never overwrites it back", () => {
  const sheet = makeSheet();
  runDoPost(sheet, { type: "customer", conversation_id: "c5", chat_address: "địa chỉ chat" }, TOKEN);
  runDoPost(sheet, { type: "order", conversation_id: "c5", shipping_address: "12 Lê Lợi" }, TOKEN);
  assert.strictEqual(sheet._rows[1][4], "12 Lê Lợi");
  // The customer payload is re-sent on every inbox scan.
  runDoPost(sheet, { type: "customer", conversation_id: "c5", chat_address: "địa chỉ chat mới" }, TOKEN);
  assert.strictEqual(sheet._rows[1][4], "12 Lê Lợi");
});

check("shop name goes to column A, the row is still found by conversation id", () => {
  const sheet = makeSheet();
  runDoPost(sheet, { type: "customer", conversation_id: "c6", shop: "Shop A", customer_name: "X" }, TOKEN);
  runDoPost(sheet, { type: "order", conversation_id: "c6", shop: "Shop A", order_total: 1 }, TOKEN);
  assert.strictEqual(sheet.getLastRow(), 2);
  assert.strictEqual(sheet._rows[1][0], "Shop A");
  assert.strictEqual(sheet._rows[1][12], "c6");
});

check("missing sheet is reported as an error, not a crash", () => {
  const sandbox = makeSandbox(null, TOKEN);
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  const body = { token: TOKEN, conversation_id: "c1", type: "customer" };
  const res = JSON.parse(sandbox.doPost({ postData: { contents: JSON.stringify(body) } })._text);
  assert.strictEqual(res.ok, false);
  assert.match(res.error, /sheet not found/);
});

console.log(failures === 0 ? "\nALL PASSED" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
