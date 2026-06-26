/**
 * エンカンAI ステータス書き戻し用 Apps Script（任意機能）
 * =====================================================
 * SFAスプレッドシートは「リンクを知っている全員（閲覧者）」のまま読み取りに使い、
 * 申請が完了した行の「ステータス」列だけを、このスクリプト（＝シート所有者の権限）で更新します。
 * サービスアカウントもGoogle Cloudも不要です。
 *
 * 【セットアップ手順（所有者が一度だけ・約10分）】
 * 1. 対象のSFAスプレッドシートを開く → メニュー「拡張機能」→「Apps Script」
 * 2. このファイルの中身をすべて貼り付ける
 * 3. 下の TOKEN を、長いランダムな文字列に書き換える
 *    （同じ値を GitHub Secrets / secrets.toml の ENKAN_WRITEBACK_TOKEN に設定）
 * 4. 「デプロイ」→「新しいデプロイ」→ 種類「ウェブアプリ」
 *      - 次のユーザーとして実行：自分
 *      - アクセスできるユーザー：全員
 * 5. 初回は「このアプリはGoogleで確認されていません」と出ます（個人スクリプトでは正常）。
 *    「詳細」→「（安全ではないページ）に移動」→ 許可
 * 6. 表示された「ウェブアプリのURL（/exec で終わる）」をコピーし、
 *    GitHub Secrets / secrets.toml の ENKAN_WRITEBACK_URL に設定
 *
 * ⚠️ URLは「全員」公開なので、URL自体を秘密に保ち、必ず TOKEN を一致させてください。
 *    このスクリプトは「ステータス列」以外は書き換えません。
 */

// 🔑 長いランダム文字列に変更し、ENKAN_WRITEBACK_TOKEN と同じ値にする
var TOKEN = "ここに長いランダムな文字列を入れる";

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);

    // トークン照合（URLが漏れても、トークンが合わないと書き込めない）
    if (!TOKEN || TOKEN === "ここに長いランダムな文字列を入れる" || body.token !== TOKEN) {
      return _json({ ok: false, error: "bad token" });
    }

    var tab = body.tab_name || "";
    var triggerCol = body.trigger_col || "ステータス";
    var status = body.status;
    var match = body.match || {};

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = tab ? ss.getSheetByName(tab) : ss.getSheets()[0];
    if (!sheet) return _json({ ok: false, error: "sheet not found: " + tab });

    var values = sheet.getDataRange().getValues();
    if (values.length < 2) return _json({ ok: false, error: "no data rows" });

    var headers = values[0].map(function (h) { return String(h).trim(); });
    var statusIdx = headers.indexOf(triggerCol);
    if (statusIdx < 0) return _json({ ok: false, error: "status col not found: " + triggerCol });

    // 照合キー（match の全ペアが一致する行を探す）
    var matchKeys = Object.keys(match);
    if (matchKeys.length === 0) return _json({ ok: false, error: "empty match" });

    var found = [];
    for (var r = 1; r < values.length; r++) {
      var ok = true;
      for (var mi = 0; mi < matchKeys.length; mi++) {
        var col = matchKeys[mi];
        var ci = headers.indexOf(col);
        if (ci < 0) { ok = false; break; }
        if (String(values[r][ci]).trim() !== String(match[col]).trim()) { ok = false; break; }
      }
      if (ok) found.push(r);
    }

    // 0件・複数件なら誤更新を避けて何もしない
    if (found.length === 0) return _json({ ok: false, error: "no match" });
    if (found.length > 1) return _json({ ok: false, error: "ambiguous match: " + found.length });

    // 「ステータス」列だけを更新
    sheet.getRange(found[0] + 1, statusIdx + 1).setValue(status);
    return _json({ ok: true, row: found[0] + 1, status: status });

  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
