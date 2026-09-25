/**
 * job-scraper web app endpoint.
 *
 * A small, token-protected API over one sheet tab for incremental sync. The Python
 * side (job_scraper/sheets.py) decides what changes; this script applies it. Rows are
 * addressed by their `key` column, never by position, so manual edits (sorting,
 * inserting, deleting rows) between calls are safe, and every call is safe to retry.
 *
 * Setup: see "Google Sheet setup" in the project README.
 *   Script Properties:
 *     TOKEN     (required) shared secret, must match [sheets].token in config.toml
 *     SHEET_ID  (optional) spreadsheet id; omit when the script is bound to the sheet
 *
 * All requests are POST with a JSON body: {token, action, sheet, ...params}
 *   read_columns  {columns, offset, limit} -> {header, total_rows, columns: {name: [values]}}
 *   set_header    {header}                 -> {}       writes row 1, freezes it
 *   insert_rows   {values}                 -> {inserted}  below the header; rows whose
 *                                                        key already exists are skipped
 *   delete_keys   {keys}                   -> {deleted}
 *   trim          {max_rows}               -> {deleted}   drops data rows past max_rows
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
      case 'read_columns':
        result = readColumns_(sheet, req.columns || [], req.offset || 0, req.limit || 5000);
        break;
      case 'set_header':
        result = setHeader_(sheet, req.header);
        break;
      case 'insert_rows':
        result = insertRows_(sheet, req.values || []);
        break;
      case 'delete_keys':
        result = deleteKeys_(sheet, req.keys || []);
        break;
      case 'trim':
        result = trim_(sheet, req.max_rows);
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

function header_(sheet) {
  var lastCol = sheet.getLastColumn();
  return lastCol ? sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String) : [];
}

function colIndex_(header, name) {
  var i = header.indexOf(name);
  if (i < 0) throw new Error('column not found: ' + name);
  return i;
}

/** Values of one column for all data rows (row 2 .. last row). */
function columnValues_(sheet, col) {
  var n = sheet.getLastRow() - 1;
  if (n <= 0) return [];
  return sheet.getRange(2, col + 1, n, 1).getValues().map(function (r) { return String(r[0]); });
}

function formatCell_(v, tz) {
  // Date cells go back as yyyy-MM-dd so Python can compare them as strings.
  return v instanceof Date ? Utilities.formatDate(v, tz, 'yyyy-MM-dd') : v;
}

function readColumns_(sheet, names, offset, limit) {
  var header = header_(sheet);
  var dataRows = Math.max(sheet.getLastRow() - 1, 0);
  var out = { header: header, total_rows: dataRows, columns: {} };
  var n = Math.min(limit, dataRows - offset);
  var tz = sheet.getParent().getSpreadsheetTimeZone();
  names.forEach(function (name) {
    var col = header.indexOf(name);
    if (col < 0 || n <= 0) {
      out.columns[name] = [];
      return;
    }
    out.columns[name] = sheet.getRange(offset + 2, col + 1, n, 1).getValues()
      .map(function (r) { return formatCell_(r[0], tz); });
  });
  return out;
}

function setHeader_(sheet, header) {
  if (sheet.getMaxColumns() < header.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), header.length - sheet.getMaxColumns());
  }
  sheet.getRange(1, 1, 1, header.length).setValues([header]);
  sheet.setFrozenRows(1);
  return {};
}

function insertRows_(sheet, values) {
  var keyCol = colIndex_(header_(sheet), 'key');
  var existing = {};
  columnValues_(sheet, keyCol).forEach(function (k) { existing[k] = true; });
  // A retried request finds its rows already present and inserts nothing.
  var fresh = values.filter(function (row) {
    var k = String(row[keyCol]).replace(/^'/, '');
    if (existing[k]) return false;
    existing[k] = true;
    return true;
  });
  if (!fresh.length) return { inserted: 0 };

  var width = fresh[0].length;
  if (sheet.getMaxColumns() < width) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  }
  sheet.insertRowsAfter(1, fresh.length);
  sheet.getRange(2, 1, fresh.length, width).setValues(fresh);
  return { inserted: fresh.length };
}

function deleteKeys_(sheet, keys) {
  var keyCol = colIndex_(header_(sheet), 'key');
  var wanted = {};
  keys.forEach(function (k) { wanted[k] = true; });
  var rows = [];  // 1-based sheet row numbers, ascending
  columnValues_(sheet, keyCol).forEach(function (k, i) {
    if (wanted[k]) rows.push(i + 2);
  });
  deleteRowNumbers_(sheet, rows);
  return { deleted: rows.length };
}

function trim_(sheet, maxRows) {
  var dataRows = sheet.getLastRow() - 1;
  if (!maxRows || dataRows <= maxRows) return { deleted: 0 };
  // New rows go in at the top, so the bottom rows are the oldest.
  sheet.deleteRows(maxRows + 2, dataRows - maxRows);
  return { deleted: dataRows - maxRows };
}

/** Delete rows bottom-up, one call per contiguous run. */
function deleteRowNumbers_(sheet, rows) {
  var i = rows.length - 1;
  while (i >= 0) {
    var end = rows[i];
    var start = end;
    while (i > 0 && rows[i - 1] === start - 1) {
      i--;
      start--;
    }
    // Sheets refuses to delete every non-frozen row; clear the last one instead.
    if (start === 2 && end >= sheet.getMaxRows()) {
      if (end > start) sheet.deleteRows(start + 1, end - start);
      sheet.getRange(2, 1, 1, sheet.getMaxColumns()).clearContent();
    } else {
      sheet.deleteRows(start, end - start + 1);
    }
    i--;
  }
}
