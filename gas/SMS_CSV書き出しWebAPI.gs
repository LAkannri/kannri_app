/**
 * ============================================================
 * 📤 SMS用CSVをアプリに渡す入口（ウェブアプリとして公開する）
 * ------------------------------------------------------------
 * これまで：人がメニューで「作成」を押し、次にサイドバーで「⬇ PC保存＋Drive保存」を押していた
 * これから：エンカンAI がこのURLを叩けば、**作成 → CSV** が一度に走って返ってくる
 *
 * ⭐ 中身のロジックは**いまのまま**です。すでにあるあなたの関数を呼ぶだけ：
 *      ・作成   … 下の BUILD_FUNCTIONS に書いた関数（例：extractLifelineContacts_FINAL）
 *      ・CSV化  … buildCsvString_()
 *    （画面（サイドバー）は作りません。人がいなくても走るようにするためです）
 *
 * ============================================================
 * 【入れ方】
 * 1. スプレッドシート → 拡張機能 → Apps Script
 * 2. いまのコードの **いちばん下に、この中身をまるごと貼り付ける**
 *    （既存の buildCsvString_ / ROOT_FOLDER_IDS などは消さないこと）
 * 3. 下の BUILD_FUNCTIONS に、**「作成」ボタンで走らせている関数名**を書く
 * 4. 右上「デプロイ」→「新しいデプロイ」→ ウェブアプリ
 *      次のユーザーとして実行：自分 ／ アクセスできるユーザー：全員
 * 5. 出てきた `.../exec` を、エンカンAI の「📱 SMS送信 → 4️⃣」に貼る
 *
 * ⚠️「全員」は **URLを知っていれば誰でも叩ける**という意味です。合言葉は必須です。
 * ⚠️ コードを直したら毎回：デプロイ → デプロイを管理 → 鉛筆 → 新バージョン → デプロイ
 * ============================================================
 */

// 🔑 合言葉（アプリの設定画面に出ているものを、そのまま入れてください）
const API_TOKEN = 'ここに長い合言葉を書く';

// 🛠 CSVを作る前に走らせる「作成」の処理。
//    このスプシにある関数名だけを書いてください。無ければ空 [] のままでOK。
//    例：['extractLifelineContacts_FINAL']
//        ['generateMoveReminderMessages']
const BUILD_FUNCTIONS = [];

/**
 * アプリからの呼び出し口。
 *   ...?token=合言葉&action=ping                  … つながるか確認
 *   ...?token=合言葉&action=sheets                … 書き出せるシート名の一覧
 *   ...?token=合言葉&action=build                 … 「作成」だけ走らせる
 *   ...?token=合言葉&action=csv&sheet=CSV         … 作成してから、そのシートのCSVを返す
 *       &build=0 を付けると「作成」を飛ばす／&drive=1 でDriveにも保存
 */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!API_TOKEN || API_TOKEN === 'ここに長い合言葉を書く') {
    return smsJsonOut_({ error: 'API_TOKEN が未設定です。スクリプトを直してください。' });
  }
  if (p.token !== API_TOKEN) {
    return smsJsonOut_({ error: '合言葉が違います' });
  }
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const action = String(p.action || 'csv');

    if (action === 'ping') {
      return smsJsonOut_({ ok: true, name: ss.getName(), 作成の処理: BUILD_FUNCTIONS });
    }

    if (action === 'sheets') {
      return smsJsonOut_({ ok: true, sheets: smsSheetNames_(ss) });
    }

    if (action === 'build') {
      const r = smsRunBuilds_();
      return r.error ? smsJsonOut_({ error: r.error }) : smsJsonOut_({ ok: true, 作成: r.done });
    }

    if (action === 'csv') {
      const name = String(p.sheet || '').trim();
      if (!name) return smsJsonOut_({ error: 'sheet（シート名）が指定されていません' });

      // 🛠 まず「作成」を走らせる（古い中身からCSVを作らないため）
      let built = [];
      if (String(p.build || '1') !== '0') {
        const r = smsRunBuilds_();
        if (r.error) return smsJsonOut_({ error: r.error });
        built = r.done;
      }

      const sheet = ss.getSheetByName(name);
      if (!sheet) {
        return smsJsonOut_({ error: 'シート「' + name + '」が見つかりません。'
                                    + 'このスプシにあるのは：' + smsSheetNames_(ss).join(' / ') });
      }

      // 📄 中身づくりは、サイドバーのボタンとまったく同じ関数を使う
      const csvString = buildCsvString_(sheet, name);
      const conf = smsConf_(ss, name);
      const stamp = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd_HHmm');
      const fileName = conf.label + '_' + stamp + '.csv';

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

      const lines = csvString ? csvString.split('\r\n').filter(function (x) { return x !== ''; }) : [];
      return smsJsonOut_({
        ok: true,
        filename: fileName,
        encoding: 'Shift_JIS',
        rows: Math.max(0, lines.length - 1),
        作成: built,
        drive: saved,
        content: Utilities.base64Encode(blob.getBytes()),
      });
    }

    return smsJsonOut_({ error: '知らない action です：' + action });
  } catch (err) {
    return smsJsonOut_({ error: String(err) });
  }
}

/** 🛠「作成」の関数を順に走らせる。戻り値：{done:[名前...]} または {error:"..."} */
function smsRunBuilds_() {
  const done = [];
  for (let i = 0; i < BUILD_FUNCTIONS.length; i++) {
    const fname = BUILD_FUNCTIONS[i];
    try {
      const fn = this[fname] || eval(fname);
      if (typeof fn !== 'function') {
        return { error: '「' + fname + '」という関数が見つかりません。'
                        + 'BUILD_FUNCTIONS に書いた名前を確かめてください。' };
      }
      fn();
      done.push(fname);
    } catch (err) {
      const msg = String(err);
      // ⚠️ 人がいないところで動かすので、画面を出す命令は使えない。
      //    どこを直せばよいかを、はっきり伝える。
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

/** このスプシの、書き出し先の設定を探す（スプシごとに書き方が違うため） */
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

/** 書き出せるシート名の候補（設定があればそれ、無ければ全シート） */
function smsSheetNames_(ss) {
  try {
    if (typeof EXPORT_CONFIG !== 'undefined') return Object.keys(EXPORT_CONFIG);
  } catch (e) { /* 無い */ }
  try {
    if (typeof ROOT_FOLDER_IDS !== 'undefined') return Object.keys(ROOT_FOLDER_IDS);
  } catch (e) { /* 無い */ }
  return ss.getSheets().map(function (s) { return s.getName(); });
}

/** JSONで返す（既存の jsonOut_ とぶつからないよう別名にしてあります） */
function smsJsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
