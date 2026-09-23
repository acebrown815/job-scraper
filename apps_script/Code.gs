/**
 * job-scraper web app endpoint.
 *
 * A small, token-protected read/write API over one sheet tab. The Python side
 * (job_scraper/sheets.py) does the merging; this script only reads and writes
 * row ranges, so every call is safe to retry.
 *
 * Setup: see "Google Sheet setup" in the project README.
 *   Script Properties:
 *     TOKEN     (required) shared secret, must match [sheets].token in config.toml
 *     SHEET_ID  (optional) spreadsheet id; omit when the script is bound to the sheet
 *
 * All requests are POST with a JSON body: {token, action, sheet, ...params}
 *   read      {offset, limit}      -> {rows, total_rows}
 *   write     {start_row, values}  -> {written}
 *   finalize  {total_rows}         -> {rows}   trims rows below total_rows, freezes header
 */

function doPost(e) {
  var result;
  var lock = LockService.getScriptLock();
  try {
    var req = JSON.parse(e.postData.contents);
    var token = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!token || req.token !== token) throw new Error('unauthorized');

    lock.waitLock(30000);
    var sheet = getSheet_(req.sheet || 'Jobs');
    switch (req.action) {
      case 'read':
        result = read_(sheet, req.offset || 0, req.limit || 5000);
        break;
      case 'write':
        result = write_(sheet, req.start_row, req.values || []);
        break;
      case 'finalize':
        result = finalize_(sheet, req.total_rows);
        break;
      default:
        throw new Error('unknown action: ' + req.action);
    }
    result.ok = true;
  } catch (err) {
    result = { ok: false, error: String((err && err.message) || err) };
  } finally {
    lock.releaseLock();
  }
  return json_(result);
}

/** Health check: open the /exec URL in a browser. */
function doGet() {
  return json_({ ok: true, service: 'job-scraper' });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function getSheet_(name) {
  var id = PropertiesService.getScriptProperties().getProperty('SHEET_ID');
  var ss = id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

function read_(sheet, offset, limit) {
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (offset >= lastRow || lastCol === 0) return { rows: [], total_rows: lastRow };

  var n = Math.min(limit, lastRow - offset);
  var tz = sheet.getParent().getSpreadsheetTimeZone();
  var rows = sheet.getRange(offset + 1, 1, n, lastCol).getValues().map(function (row) {
    return row.map(function (v) {
      // Date cells go back as yyyy-MM-dd so Python can compare them as strings.
      return v instanceof Date ? Utilities.formatDate(v, tz, 'yyyy-MM-dd') : v;
    });
  });
  return { rows: rows, total_rows: lastRow };
}

function write_(sheet, startRow, values) {
  if (!values.length) return { written: 0 };
  var width = values[0].length;
  var needRows = startRow + values.length - 1;

  if (sheet.getMaxRows() < needRows) {
    sheet.insertRowsAfter(sheet.getMaxRows(), needRows - sheet.getMaxRows());
  }
  if (sheet.getMaxColumns() < width) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  }
  sheet.getRange(startRow, 1, values.length, width).setValues(values);
  return { written: values.length };
}

function finalize_(sheet, totalRows) {
  // Keep at least one row below the frozen header; Sheets refuses to delete all of them.
  var keep = Math.max(totalRows, 2);
  var maxRows = sheet.getMaxRows();
  if (maxRows > keep) sheet.deleteRows(keep + 1, maxRows - keep);
  if (totalRows < keep) {
    sheet.getRange(totalRows + 1, 1, keep - totalRows, sheet.getMaxColumns()).clearContent();
  }
  sheet.setFrozenRows(1);
  return { rows: totalRows };
}
