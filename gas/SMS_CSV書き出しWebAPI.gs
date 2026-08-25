/**
 * ============================================================
 * 📤 SMS用CSVをアプリに渡す入口（ウェブアプリとして公開する）
 * ------------------------------------------------------------
 * ⭐ このコードは **どのスプレッドシートでも中身は同じ** です。
 *    書き替えるのは、下の合言葉（API_TOKEN）の1行だけ。
 *    「どの関数で作るか」「どのシートをCSVにするか」は、**アプリ側で選びます**。
 *    （スプシごとに関数名やシート名が違うので、コードを読んで書き分けずに済むように）
 *
 * ⭐ 中身のロジックは**いまのまま**です。すでにあるあなたの関数を呼ぶだけ：
 *      ・作成   … アプリで選んだ関数（例：extractLifelineContacts_FINAL）
 *      ・CSV化  … buildCsvString_()
 *    画面（サイドバー）は作りません。人がいなくても走るようにするためです。
 *
 * ============================================================
 * 【入れ方】※ スプシごとに1回だけ
 * 1. スプレッドシート → 拡張機能 → Apps Script
 * 2. いまのコードの **いちばん下に、この中身をまるごと貼り付ける**
 *    （既存の buildCsvString_ などは消さないこと）
 * 3. 右上「デプロイ」→「新しいデプロイ」→ ウェブアプリ
 *      次のユーザーとして実行：自分 ／ アクセスできるユーザー：全員
 * 4. 出てきた `.../exec` を、エンカンAI の「📱 SMS送信 → 4️⃣」に貼る
 * 5. アプリの「🔌 つながるか試す」を押すと、**このスプシにある関数とシートが一覧で出ます**。
 *    そこから選ぶだけです。
 *
 * ⚠️「全員」は **URLを知っていれば誰でも叩ける**という意味です。合言葉は必須です。
 * ⚠️ コードを直したら毎回：デプロイ → デプロイを管理 → 鉛筆 → 新バージョン → デプロイ
 * ============================================================
 */

// 🔑 合言葉（アプリの設定画面に出ているものが、すでに入っています）
const API_TOKEN = 'ここに長い合言葉を書く';

/**
 * アプリからの呼び出し口。
 *   ...?token=合言葉&action=inspect              … このスプシの関数とシートを教える
 *   ...?token=合言葉&action=csv&sheet=CSV&build=関数名
 *                                                … 作成してから、そのシートのCSVを返す
 *       build は カンマ区切りで複数可／省略すると作成しない／&drive=1 でDriveにも保存
 */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!API_TOKEN || API_TOKEN === 'ここに長い合言葉を書く') {
    return smsJsonOut_({ error: 'API_TOKEN が未設定です。アプリに出ている合言葉を入れてください。' });
  }
  if (p.token !== API_TOKEN) {
    return smsJsonOut_({ error: '合言葉が違います' });
  }
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const action = String(p.action || 'csv');

    // 🔎 このスプシに何があるかを教える。アプリはこれを見て選択肢を出す。
    if (action === 'inspect' || action === 'ping') {
      return smsJsonOut_({
        ok: true,
        name: ss.getName(),
        sheets: ss.getSheets().map(function (s) { return s.getName(); }),
        functions: smsFunctionNames_(),
        csvReady: (typeof buildCsvString_ === 'function'),
      });
    }

    if (action === 'csv') {
      const name = String(p.sheet || '').trim();
      if (!name) return smsJsonOut_({ error: 'sheet（シート名）が指定されていません' });

      // 🛠 まず「作成」を走らせる（古い中身からCSVを作らないため）
      const builds = String(p.build || '').split(',')
        .map(function (x) { return x.trim(); })
        .filter(function (x) { return x; });
      const r = smsRunBuilds_(builds);
      if (r.error) return smsJsonOut_({ error: r.error });

      const sheet = ss.getSheetByName(name);
      if (!sheet) {
        return smsJsonOut_({ error: 'シート「' + name + '」が見つかりません。'
                                    + 'あるのは：' + ss.getSheets().map(function (s) {
                                        return s.getName(); }).join(' / ') });
      }
      if (typeof buildCsvString_ !== 'function') {
        return smsJsonOut_({ error: 'このスプシに buildCsvString_ がありません。'
                                    + 'CSVを作る関数が別の名前のようです。ご連絡ください。' });
      }

      // 📄 中身づくりは、サイドバーのボタンとまったく同じ関数を使う
      const csvString = buildCsvString_(sheet, name);
      const conf = smsConf_(ss, name);
      const stamp = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd_HHmm');
      const fileName = conf.label + '_' + stamp + '.csv';

      // Shift_JIS（Windows-31J）のまま渡す。ここで文字コードを変えない。
      const blob = Utilities.newBlob('', 'text/csv', fileName)
                            .setDataFromString(csvString, 'Windows-31J');

      let saved = '';
      if (String(p.drive || '') === '1' && conf.root) {
        try {
          saved = getTodayFolder_(conf.root).createFile(blob.copyBlob()).getName();
        } catch (err) {
          saved = 'Drive保存に失敗：' + String(err);
        }
      }

      const lines = csvString ? csvString.split('\r\n').filter(function (x) { return x !== ''; }) : [];
      return smsJsonOut_({
        ok: true,
        filename: fileName,
        encoding: 'Shift_JIS',
        rows: Math.max(0, lines.length - 1),
        作成: r.done,
        drive: saved,
        content: Utilities.base64Encode(blob.getBytes()),
      });
    }

    return smsJsonOut_({ error: '知らない action です：' + action });
  } catch (err) {
    return smsJsonOut_({ error: String(err) });
  }
}

/** このスクリプトにある関数の名前を集める（アプリの選択肢に出すため） */
function smsFunctionNames_() {
  const out = [];
  try {
    for (const k in this) {
      if (k.indexOf('sms') === 0) continue;              // この入口の部品は出さない
      if (k === 'doGet' || k === 'doPost') continue;
      try {
        if (typeof this[k] === 'function') out.push(k);
      } catch (e) { /* 触れないものは飛ばす */ }
    }
  } catch (e) { /* 取れなければ空で返す */ }
  return out.sort();
}

/** 🛠「作成」の関数を順に走らせる。戻り値：{done:[名前...]} または {error:"..."} */
function smsRunBuilds_(names) {
  const done = [];
  for (let i = 0; i < (names || []).length; i++) {
    const fname = names[i];
    let fn = null;
    try { fn = this[fname]; } catch (e) { fn = null; }
    if (typeof fn !== 'function') {
      return { error: '「' + fname + '」という関数がこのスプシにありません。'
                      + 'アプリの「作成に使う処理」を選び直してください。' };
    }
    try {
      fn();
      done.push(fname);
    } catch (err) {
      const msg = String(err);
      // ⚠️ 人がいないところで動かすので、画面を出す命令は使えない。
      if (msg.indexOf('getUi') >= 0 || msg.indexOf('Cannot call') >= 0) {
        return { error: '「' + fname + '」は画面（ui.alert など）を使っているため、'
                        + 'アプリからは走らせられません。関数の中の '
                        + 'const ui = SpreadsheetApp.getUi(); を '
                        + 'let ui = null; try { ui = SpreadsheetApp.getUi(); } catch (e) {} に変え、'
                        + 'ui.alert(...) を if (ui) ui.alert(...) にしてください。'
                        + '（メニューから押したときは、これまでどおり画面が出ます）' };
      }
      return { error: '「' + fname + '」でエラー：' + msg };
    }
  }
  return { done: done };
}

/** 書き出し先の設定を探す（スプシごとに書き方が違うので、どちらでも拾う） */
function smsConf_(ss, name) {
  const out = { root: '', label: name };
  try {
    if (typeof EXPORT_CONFIG !== 'undefined' && EXPORT_CONFIG[name]) {
      const c = EXPORT_CONFIG[name];
      if (c.root) out.root = c.root;
      if (c.label) out.label = c.label;
      return out;
    }
  } catch (e) { /* この書き方は使っていないスプシ */ }
  try {
    if (typeof ROOT_FOLDER_IDS !== 'undefined' && ROOT_FOLDER_IDS[name]) {
      out.root = ROOT_FOLDER_IDS[name];
    }
  } catch (e) { /* 同上 */ }
  try {
    if (typeof FILE_SUFFIX_MAP !== 'undefined' && FILE_SUFFIX_MAP[name]) {
      out.label = ss.getName() + '_' + FILE_SUFFIX_MAP[name];
    }
  } catch (e) { /* 同上 */ }
  return out;
}

/** JSONで返す（既存の jsonOut_ とぶつからないよう別名にしてあります） */
function smsJsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
