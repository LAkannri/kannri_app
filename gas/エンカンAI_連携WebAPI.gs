/**
 * ============================================================
 * 🔗 エンカンAI 連携の入口（ウェブアプリとして公開する）
 * ------------------------------------------------------------
 * ⭐ このコードは **どのスプレッドシートでも、どの用途でも中身は同じ** です。
 *    （SMS送信でも、データローダー自動化でも、これ1つ）
 *    書き替えるのは、下の合言葉（API_TOKEN）の1行だけ。
 *
 * ⭐ 「どの処理を走らせるか」「どのシートをCSVにするか」は **アプリ側で選びます**。
 *    スプシごとに関数名もシート名も違うので、コードを読んで書き分けずに済むように。
 *
 * ⭐ 中身のロジックは**いまのまま**です。すでにあるあなたの関数を呼ぶだけで、
 *    このコードは何も作り変えません。画面（サイドバー）も出しません。
 *
 * ============================================================
 * 【入れ方】※ スプシごとに1回だけ
 * 1. スプレッドシート → 拡張機能 → Apps Script
 * 2. いまのコードの **いちばん下に、この中身をまるごと貼り付ける**
 *    （既存の関数は消さないこと）
 * 3. 右上「デプロイ」→「新しいデプロイ」→ ウェブアプリ
 *      次のユーザーとして実行：自分 ／ アクセスできるユーザー：全員
 * 4. 出てきた `.../exec` を、エンカンAI の設定画面に貼る
 * 5. 「🔌 つないで中身を見る」を押すと、**このスプシにある処理とシートが一覧で出ます**。
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
 *   ...?token=合言葉&action=inspect                  … このスプシの処理とシートを教える
 *   ...?token=合言葉&action=build&build=関数名,関数名 … その処理を順に走らせる
 *   ...?token=合言葉&action=csv&sheet=名前&build=関数名
 *                                                    … 走らせてから、そのシートのCSVを返す
 *       &drive=1 を付けると、これまでどおり Drive にも控えを残す
 */
function doGet(e) {
  const p = (e && e.parameter) || {};
  // ⚠️ ここで合言葉そのものを書かないこと。
  //    アプリは上の1行に合言葉を埋め込むので、同じ文字をここにも書くと
  //    両方が置き換わり、正しい合言葉でも必ず「未設定」になってしまう。
  //    （つなげて作ることで、埋め込みの対象にならないようにしている）
  const enkanUnset = ['ここに', '長い', '合言葉を', '書く'].join('');
  if (!API_TOKEN || API_TOKEN === enkanUnset) {
    return enkanJsonOut_({ error: 'API_TOKEN が未設定です。アプリに出ている合言葉を入れてください。' });
  }
  if (p.token !== API_TOKEN) {
    return enkanJsonOut_({
      error: '合言葉が違います（アプリ側 ' + String(p.token || '').length + '文字／'
             + 'スクリプト側 ' + String(API_TOKEN).length + '文字）',
    });
  }
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const action = String(p.action || 'inspect');

    // 🔎 このスプシに何があるかを教える。アプリはこれを見て選択肢を出す。
    if (action === 'inspect' || action === 'ping') {
      return enkanJsonOut_({
        ok: true,
        name: ss.getName(),
        sheets: ss.getSheets().map(function (s) { return s.getName(); }),
        functions: enkanFunctionNames_(),
        csvReady: (typeof buildCsvString_ === 'function'),
        // 📁 このスプシがDriveへの控えに対応しているか（対応していないスプシもある）
        driveReady: (typeof getTodayFolder_ === 'function'),
        driveSheets: enkanDriveSheets_(),
      });
    }

    const builds = String(p.build || '').split(',')
      .map(function (x) { return x.trim(); })
      .filter(function (x) { return x; });

    // 🛠 処理を走らせるだけ（データローダー：投入用シートの作り直し）
    if (action === 'build') {
      const r = enkanRunBuilds_(builds);
      if (r.error) return enkanJsonOut_({ error: r.error });
      return enkanJsonOut_({ ok: true, name: ss.getName(), 実行: r.done, 件数: enkanCounts_(ss) });
    }

    // 📄 走らせてから、CSVを返す（SMS送信）
    if (action === 'csv') {
      const name = String(p.sheet || '').trim();
      if (!name) return enkanJsonOut_({ error: 'sheet（シート名）が指定されていません' });

      const r = enkanRunBuilds_(builds);
      if (r.error) return enkanJsonOut_({ error: r.error });

      const sheet = ss.getSheetByName(name);
      if (!sheet) {
        return enkanJsonOut_({ error: 'シート「' + name + '」が見つかりません。'
                                      + 'あるのは：' + ss.getSheets().map(function (s) {
                                          return s.getName(); }).join(' / ') });
      }
      if (typeof buildCsvString_ !== 'function') {
        return enkanJsonOut_({ error: 'このスプシに buildCsvString_ がありません。'
                                      + 'CSVを作る関数が別の名前のようです。' });
      }

      // 中身づくりは、サイドバーのボタンとまったく同じ関数を使う
      const csvString = buildCsvString_(sheet, name);
      const conf = enkanConf_(ss, name);
      const stamp = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd_HHmm');
      const fileName = conf.label + '_' + stamp + '.csv';

      // Shift_JIS（Windows-31J）のまま渡す。ここで文字コードを変えない。
      const blob = Utilities.newBlob('', 'text/csv', fileName)
                            .setDataFromString(csvString, 'Windows-31J');

      // 📁 Driveへの控え。**黙って何もしない**ことがないよう、結果を必ず言葉で返す。
      let saved = '';
      if (String(p.drive || '') !== '1') {
        saved = '（残さない設定です）';
      } else if (!conf.root) {
        saved = '（このスプシにはDriveの保存先が設定されていないため、残せません）';
      } else if (typeof getTodayFolder_ !== 'function') {
        saved = '（このスプシに getTodayFolder_ が無いため、残せません）';
      } else {
        try {
          saved = getTodayFolder_(conf.root).createFile(blob.copyBlob()).getName();
        } catch (err) {
          saved = 'Drive保存に失敗：' + String(err);
        }
      }

      const lines = csvString ? csvString.split('\r\n').filter(function (x) { return x !== ''; }) : [];
      return enkanJsonOut_({
        ok: true,
        filename: fileName,
        encoding: 'Shift_JIS',
        rows: Math.max(0, lines.length - 1),
        実行: r.done,
        drive: saved,
        content: Utilities.base64Encode(blob.getBytes()),
      });
    }

    return enkanJsonOut_({ error: '知らない action です：' + action });
  } catch (err) {
    return enkanJsonOut_({ error: String(err) });
  }
}

/** このスクリプトにある関数の名前を集める（アプリの選択肢に出すため） */
function enkanFunctionNames_() {
  const out = [];
  try {
    for (const k in this) {
      if (k.indexOf('enkan') === 0) continue;            // この入口の部品は出さない
      if (k === 'doGet' || k === 'doPost' || k === 'onOpen') continue;
      try {
        if (typeof this[k] === 'function') out.push(k);
      } catch (e) { /* 触れないものは飛ばす */ }
    }
  } catch (e) { /* 取れなければ空で返す */ }
  return out.sort();
}

/** Driveへの控えができるシート名（保存先が決まっているものだけ） */
function enkanDriveSheets_() {
  const out = [];
  try {
    if (typeof EXPORT_CONFIG !== 'undefined') {
      for (const k in EXPORT_CONFIG) {
        if (EXPORT_CONFIG[k] && EXPORT_CONFIG[k].root) out.push(k);
      }
      return out;
    }
  } catch (e) { /* 無い */ }
  try {
    if (typeof ROOT_FOLDER_IDS !== 'undefined') {
      for (const k in ROOT_FOLDER_IDS) {
        if (ROOT_FOLDER_IDS[k]) out.push(k);
      }
    }
  } catch (e) { /* 無い */ }
  return out;
}


/** 各シートのデータ行数（見出しを除く）。作り直したあとの確認に使う。 */
function enkanCounts_(ss) {
  const out = {};
  ss.getSheets().forEach(function (s) {
    try { out[s.getName()] = Math.max(0, s.getLastRow() - 1); } catch (e) { /* 飛ばす */ }
  });
  return out;
}

/** 🛠 選ばれた処理を順に走らせる。戻り値：{done:[名前...]} または {error:"..."} */
function enkanRunBuilds_(names) {
  const done = [];
  for (let i = 0; i < (names || []).length; i++) {
    const fname = names[i];
    let fn = null;
    try { fn = this[fname]; } catch (e) { fn = null; }
    if (typeof fn !== 'function') {
      return { error: '「' + fname + '」という関数がこのスプシにありません。'
                      + 'アプリの「走らせる処理」を選び直してください。' };
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
function enkanConf_(ss, name) {
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
function enkanJsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
