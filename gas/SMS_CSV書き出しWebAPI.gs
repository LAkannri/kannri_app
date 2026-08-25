/**
 * ============================================================
 * 📤 CSVをアプリに渡すための入口（ウェブアプリとして公開する）
 * ------------------------------------------------------------
 * これまで：人がサイドバーの「⬇ PC保存＋Drive保存」を押していた
 * これから：エンカンAI がこのURLを叩けば、同じCSVがその場で返ってくる
 *
 * ⭐ CSVの中身を作るのは、いまお使いの buildCsvString_() のままです。
 *   電話番号の頭の0付けも Shift_JIS 化も、これまでどおり GAS がやります。
 *   （同じ整形をアプリ側にも書くと、片方だけ直して食い違うため）
 *
 * ============================================================
 * 【入れ方】
 * 1. スプレッドシート → 拡張機能 → Apps Script を開く
 * 2. いまのコードの **いちばん下に、このファイルの中身を貼り付ける**
 *    （EXPORT_CONFIG / buildCsvString_ / getTodayFolder_ は既にあるので消さないこと）
 * 3. 下の API_TOKEN を、**長い合言葉**に書き換える（推測されない文字列）
 * 4. 右上「デプロイ」→「新しいデプロイ」→ 種類は「ウェブアプリ」
 *      説明             ：SMS用CSV
 *      次のユーザーとして実行：自分
 *      アクセスできるユーザー：全員
 * 5. 出てきた `https://script.google.com/macros/s/AKfy.../exec` をコピーして、
 *    エンカンAI の「📱 SMS送信 → パターンの設定 → 4️⃣」に貼る
 *
 * ⚠️「アクセスできるユーザー：全員」は、**URLを知っていれば誰でも叩ける**という意味です。
 *    だから合言葉（API_TOKEN）を必ず設定してください。合わない呼び出しは断ります。
 *    合言葉は、アプリの設定画面にも同じものを入れます。
 *
 * ⚠️ コードを直したら、そのたびに「デプロイ」→「デプロイを管理」→ 鉛筆 →
 *    バージョン「新バージョン」→ デプロイ、で更新してください（URLは変わりません）。
 * ============================================================
 */

// 🔑 合言葉。ここを必ず書き換える（例：適当な英数字を30文字ほど）
const API_TOKEN = 'ここに長い合言葉を書く';

/**
 * アプリからの呼び出し口。
 *   ...?token=合言葉&action=ping                     … つながるか確認
 *   ...?token=合言葉&action=sheets                   … 書き出せるシート名の一覧
 *   ...?token=合言葉&action=csv&sheet=CSV            … そのシートのCSVを返す
 *   ...&drive=1 を付けると、これまでどおり Drive にも保存する
 */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!API_TOKEN || API_TOKEN === 'ここに長い合言葉を書く') {
    return jsonOut_({ error: 'API_TOKEN が未設定です。スクリプトを直してください。' });
  }
  if (p.token !== API_TOKEN) {
    return jsonOut_({ error: '合言葉が違います' });
  }
  try {
    const action = String(p.action || 'csv');

    if (action === 'ping') {
      return jsonOut_({ ok: true, name: SpreadsheetApp.getActiveSpreadsheet().getName() });
    }

    if (action === 'sheets') {
      return jsonOut_({ ok: true, sheets: Object.keys(EXPORT_CONFIG) });
    }

    if (action === 'csv') {
      const name = String(p.sheet || '').trim();
      if (!name) return jsonOut_({ error: 'sheet（シート名）が指定されていません' });

      const ss = SpreadsheetApp.getActiveSpreadsheet();
      const sheet = ss.getSheetByName(name);
      if (!sheet) return jsonOut_({ error: 'シート「' + name + '」が見つかりません' });

      // 📄 中身づくりは、サイドバーのボタンとまったく同じ関数を使う
      const csvString = buildCsvString_(sheet, name);
      const conf = EXPORT_CONFIG[name] || {};
      const stamp = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd_HHmm');
      const fileName = (conf.label || name) + '_' + stamp + '.csv';

      // Shift_JIS（Windows-31J）のまま渡す。ここで文字コードを変えない。
      const blob = Utilities.newBlob('', 'text/csv', fileName)
                            .setDataFromString(csvString, 'Windows-31J');

      // 証跡をこれまでどおり Drive にも残したいときは &drive=1
      let saved = '';
      if (String(p.drive || '') === '1' && conf.root) {
        try {
          saved = getTodayFolder_(conf.root).createFile(blob.copyBlob()).getName();
        } catch (err) {
          saved = 'Drive保存に失敗：' + String(err);
        }
      }

      // 行数（見出しを除く）も返す。アプリ側で件数の食い違いに気づけるように。
      const lines = csvString ? csvString.split('\r\n').filter(function (x) { return x !== ''; }) : [];

      return jsonOut_({
        ok: true,
        filename: fileName,
        encoding: 'Shift_JIS',
        rows: Math.max(0, lines.length - 1),
        drive: saved,
        content: Utilities.base64Encode(blob.getBytes()),
      });
    }

    return jsonOut_({ error: '知らない action です：' + action });
  } catch (err) {
    return jsonOut_({ error: String(err) });
  }
}

/** JSONで返す（この関数が既にある場合は、こちらは消してください） */
function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
