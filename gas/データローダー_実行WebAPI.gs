/**
 * ============================================================
 * 🗃 データローダー用シートを作り直す入口（ウェブアプリとして公開する）
 * ------------------------------------------------------------
 * これまで：人がメニューから「CSVダウンロード表示」を押して、5つの処理を走らせていた
 * これから：エンカンAI がこのURLを叩けば、同じ5つの処理が走る
 *
 * ⭐ 中身のロジックは**いまのまま**です。すでにあるこの5つを呼ぶだけ：
 *      generateNoGuideDL / generateCheckError / updateTargetToDateSpecified /
 *      extractOverseasProjects / extractPointDL
 *    （画面（サイドバー）は作りません。人がいなくても走るようにするためです）
 *
 * ============================================================
 * 【入れ方】
 * 1. スプレッドシート → 拡張機能 → Apps Script
 * 2. いまのコードの **いちばん下に、このファイルの中身を貼り付ける**
 * 3. 下の API_TOKEN を、**長い合言葉**に書き換える（推測されない文字列）
 * 4. 右上「デプロイ」→「新しいデプロイ」→ 種類は「ウェブアプリ」
 *      次のユーザーとして実行：自分
 *      アクセスできるユーザー：全員
 * 5. 出てきた `https://script.google.com/macros/s/AKfy.../exec` を
 *    エンカンAI の「🗃 データローダー自動化 → ジョブの設定 → 3️⃣」に貼る
 *
 * ⚠️「アクセスできるユーザー：全員」は、**URLを知っていれば誰でも叩ける**という意味です。
 *    必ず合言葉を設定してください。合わない呼び出しは断ります。
 * ⚠️ コードを直したら「デプロイ」→「デプロイを管理」→ 鉛筆 → 新バージョン → デプロイ
 *    で更新してください（URLは変わりません）。
 * ============================================================
 */

// 🔑 合言葉。ここを必ず書き換える（例：適当な英数字を30文字ほど）
const DL_API_TOKEN = 'ここに長い合言葉を書く';

// 作り直したあと、何件できたかを返すシート
const DL_RESULT_SHEETS = ['案内不要DL', '情報確認総務', '海外案件', '地点DL', '検討エラーリスト'];

/**
 * アプリからの呼び出し口。
 *   ...?token=合言葉&action=ping    … つながるか確認
 *   ...?token=合言葉                … シートを作り直す（既定）
 */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!DL_API_TOKEN || DL_API_TOKEN === 'ここに長い合言葉を書く') {
    return dlJsonOut_({ error: 'DL_API_TOKEN が未設定です。スクリプトを直してください。' });
  }
  if (p.token !== DL_API_TOKEN) {
    return dlJsonOut_({ error: '合言葉が違います' });
  }
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();

    if (String(p.action || '') === 'ping') {
      return dlJsonOut_({ ok: true, name: ss.getName() });
    }

    // 🔁 メニューから押していたのと同じ5つ。順番も同じ。
    generateNoGuideDL();
    generateCheckError();
    updateTargetToDateSpecified();
    extractOverseasProjects();
    extractPointDL();

    // 何件できたかを返す（アプリの画面に出して、人が確かめられるように）
    const counts = {};
    DL_RESULT_SHEETS.forEach(function (n) {
      const sh = ss.getSheetByName(n);
      counts[n] = sh ? Math.max(0, sh.getLastRow() - 1) : -1;   // -1 は「シートが無い」
    });
    return dlJsonOut_({ ok: true, name: ss.getName(), 件数: counts });
  } catch (err) {
    return dlJsonOut_({ error: String(err) });
  }
}

/** JSONで返す（jsonOut_ が既にある場合でもぶつからないよう別名にしてあります） */
function dlJsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
