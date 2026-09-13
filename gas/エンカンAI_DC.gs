/**
 * 🔎 エンカンAI エントリー前DC（アプリが自動で書き込みます）
 *
 * 「DCルール表」（1行＝1ルール）を正本にして、チェック用シートの全案件を判定し、
 * **NGになった案件だけ**を「DCエラー一覧」に理由つきで書き出す。
 *
 * ⚠️ 条件付き書式の「色」は、Googleの仕組み上どこからも読めない。
 *    だから同じ数式を**判定用の隠しシート**で計算させ、TRUE になった行を拾う。
 *    数式は「2行目の形」で持つ（例：$J2="SB光"）。2行目に置いて下へコピーすれば、
 *    行番号はスプシが合わせてくれる（数式を読み解いて書き換えない＝壊さない）。
 *
 * アプリからの呼び方：action=build&build=enkanDcRun ／ enkanDcTry
 */
var DC_RULE_SHEET = 'DCルール表';
var DC_OUT_SHEET = 'DCエラー一覧';
var DC_TRY_SHEET = 'DC試し';
var DC_TRY_OUT_SHEET = 'DC試し結果';
var DC_HELPER_PREFIX = 'DC判定_';
var DC_KEY_COLS = ['案件番号', '個人名'];

/** ルール表のONのルールで、全案件を判定する（アプリの「🔍 チェックする」） */
function enkanDcRun() {
  enkanDcWithLock_(function () {
    enkanDcEvaluate_(enkanDcReadRules_(DC_RULE_SHEET, true), DC_OUT_SHEET);
  });
}

/** 登録する前の1本だけを試す（アプリの「🧪 いまのデータで試す」） */
function enkanDcTry() {
  enkanDcWithLock_(function () {
    enkanDcEvaluate_(enkanDcReadRules_(DC_TRY_SHEET, false), DC_TRY_OUT_SHEET);
  });
}

function enkanDcWithLock_(fn) {
  // 2人が同時に押すと、判定用シートを取り合って結果が混ざる
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(120000)) throw new Error('ほかの人がチェック中です。少し待ってからもう一度押してください。');
  try { fn(); } finally { lock.releaseLock(); }
}

function enkanDcReadRules_(sheetName, onlyOn) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(sheetName);
  if (!sh) throw new Error('「' + sheetName + '」シートがありません。');
  var v = sh.getDataRange().getValues();
  if (v.length < 2) return [];
  var h = {};
  v[0].forEach(function (x, i) { h[String(x).trim()] = i; });
  var need = ['ID', '対象', 'ルール名', '条件（2行目の形の数式）'];
  need.forEach(function (k) {
    if (h[k] === undefined) throw new Error('「' + sheetName + '」に「' + k + '」の列がありません。');
  });
  var get = function (row, k) { return h[k] === undefined ? '' : row[h[k]]; };
  var out = [];
  for (var r = 1; r < v.length; r++) {
    var row = v[r];
    var f = String(get(row, '条件（2行目の形の数式）') || '').trim().replace(/^=/, '');
    if (!String(get(row, 'ID')).trim() || !f) continue;
    var on = get(row, 'ON');
    if (onlyOn && !(on === true || String(on).toUpperCase() === 'TRUE')) continue;
    out.push({
      id: String(get(row, 'ID')).trim(), target: String(get(row, '対象')).trim(),
      kind: String(get(row, '種類') || 'NG').trim(), name: String(get(row, 'ルール名')).trim(),
      why: String(get(row, 'NGの理由') || get(row, 'ルール名')).trim(),
      cols: String(get(row, '見る列') || '').split(/[,、\s]+/).filter(function (x) { return x; }),
      formula: f,
    });
  }
  return out;
}

function enkanDcColLetter_(n) {
  var s = '';
  while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

function enkanDcColIndex_(letter) {
  var n = 0;
  String(letter).toUpperCase().split('').forEach(function (c) { n = n * 26 + (c.charCodeAt(0) - 64); });
  return n;
}

function enkanDcEvaluate_(rules, outName) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var stamp = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy/MM/dd HH:mm');
  var out = [];
  var byTarget = {};
  rules.forEach(function (r) { (byTarget[r.target] = byTarget[r.target] || []).push(r); });

  Object.keys(byTarget).forEach(function (target) {
    var list = byTarget[target];
    var src = ss.getSheetByName(target);
    if (!src) {
      list.forEach(function (r) {
        out.push([stamp, target, '', '', '', '⚠️ 設定', r.id, r.name, '「' + target + '」シートがありません', '']);
      });
      return;
    }
    var disp = src.getDataRange().getDisplayValues();
    var heads = disp[0] || [];
    var n = 0;                                    // 中身のある最後の行
    for (var i = disp.length - 1; i >= 1; i--) {
      if (disp[i].some(function (x) { return String(x).trim() !== ''; })) { n = i + 1; break; }
    }
    if (n < 2) return;                            // 案件が1件もない

    var W = src.getMaxColumns();
    var R = list.length;
    var hName = DC_HELPER_PREFIX + target;
    var h = ss.getSheetByName(hName) || ss.insertSheet(hName);
    if (!h.isSheetHidden()) h.hideSheet();
    h.clear();
    if (h.getMaxColumns() < W + R) h.insertColumnsAfter(h.getMaxColumns(), W + R - h.getMaxColumns());
    if (h.getMaxRows() < n) h.insertRowsAfter(h.getMaxRows(), n - h.getMaxRows());

    // 左側にチェック用シートをそのまま映す → 数式の $J2 などが同じ列を指す
    h.getRange(1, 1).setFormula("=ARRAYFORMULA('" + target.replace(/'/g, "''") + "'!A1:"
                                + enkanDcColLetter_(W) + n + ')');
    list.forEach(function (r, k) {
      var cell = h.getRange(2, W + 1 + k);
      cell.setFormula('=IFERROR(IF((' + r.formula + '),TRUE,FALSE),"式エラー")');
      if (n > 2) cell.copyTo(h.getRange(3, W + 1 + k, n - 2, 1));
    });
    SpreadsheetApp.flush();
    var res = h.getRange(2, W + 1, n - 1, R).getValues();

    var keyIdx = DC_KEY_COLS.map(function (k) { return heads.indexOf(k); });
    var hitRows = [];                              // この対象シートで拾った行（あとで重なりを外す）
    // ⚠️ 1行だけ計算できない（生年月日が空で DATEDIF が失敗、など）は、色付けと同じく「NGではない」。
    //    **全行で**計算できないときだけ、数式そのものが壊れているとみなす。
    var errCount = {};
    var checked = 0;                               // 判定した案件の行数
    for (var row = 0; row < res.length; row++) {
      var line = disp[row + 1] || [];
      // ⚠️ SFのレポートを貼ると、案件ではない行（合計・条件の説明など）が下に付く。
      //    案件番号の無い行を判定すると「空なのでNG」が大量に出るので、案件の行だけ見る。
      if (keyIdx[0] >= 0 && String(line[keyIdx[0]] || '').trim() === '') continue;
      checked++;
      for (var k = 0; k < R; k++) {
        var val = res[row][k];
        var r = list[k];
        if (val === '式エラー' || (typeof val === 'string' && val.indexOf('#') === 0)) {
          errCount[r.id] = (errCount[r.id] || 0) + 1;
          continue;
        }
        if (val !== true) continue;
        var shown = r.cols.map(function (c) {
          var ci = enkanDcColIndex_(c) - 1;
          return (heads[ci] || (c + '列')) + '＝' + (String(line[ci] || '').trim() || '（空）');
        }).join('／');
        hitRows.push({ cols: r.cols.join(','), line: [stamp, target, row + 2,
                  keyIdx[0] >= 0 ? line[keyIdx[0]] : '', keyIdx[1] >= 0 ? line[keyIdx[1]] : '',
                  r.kind, r.id, r.name, r.why, shown] });
      }
    }
    // ⚠️ 「期限切れ（NG）」の行は「ギリギリ（注意）」にも当てはまる。色付けは先のルールの色だけが出るが、
    //    ここでは両方拾ってしまうので、**同じ行・同じ見る列でNGがあれば、注意は出さない**。
    hitRows.forEach(function (x) {
      if (x.line[5] !== 'NG') {
        var dup = hitRows.some(function (y) {
          return y.line[5] === 'NG' && y.line[2] === x.line[2] && y.cols === x.cols;
        });
        if (dup) return;
      }
      out.push(x.line);
    });
    Object.keys(errCount).forEach(function (id) {
      if (!checked || errCount[id] < checked) return;
      var r = list.filter(function (x) { return x.id === id; })[0];
      out.push([stamp, target, '', '', '', '⚠️ 式エラー', id, r.name,
                '全部の行で数式が計算できませんでした。条件の書き方を確かめてください', r.formula]);
    });
    h.clear();                                   // 計算を残すとスプシが重くなるので消す
  });

  var o = ss.getSheetByName(outName) || ss.insertSheet(outName);
  o.clear();
  var head = [['実行日時', '対象', '行', '案件番号', '個人名', '種類', 'ルールID', 'ルール名', 'NGの理由', '見る列の中身']];
  o.getRange(1, 1, 1, head[0].length).setValues(head).setFontWeight('bold');
  if (out.length) {
    o.getRange(2, 1, out.length, head[0].length).setNumberFormat('@').setValues(out);
  } else {
    o.getRange(2, 1, 1, 3).setValues([[stamp, '', 'NGはありませんでした']]);
  }
}
