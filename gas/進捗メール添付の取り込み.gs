/**
 * 📥 進捗メールの添付ファイルを Drive に保存する（キャリアごと）
 *
 * ねらい：
 *   キャリアから届く進捗メールの添付を、決まった Drive フォルダに自動で置く。
 *   アプリ（エンカンAI）はそのフォルダを見るだけでよくなり、Gmail の認証設定が不要になる。
 *
 * 設定はコードに書かない：
 *   キャリアが増えるたびにコードを直すのは現実的でないため、
 *   取り込み条件は「設定シート」（スプレッドシートの表）に書く。GAS はそれを読むだけ。
 *   アプリ側からも同じ表を編集できるので、担当者はコードを触らなくてよい。
 *
 * 置き場所：
 *   メールが届くアカウント（info@lifeap.co.jp）で開いた Apps Script プロジェクトに貼る。
 *
 * 使い方：
 *   1) 下の CONFIG_SHEET_ID に、設定シートのあるスプレッドシートのIDを入れる
 *   2) setup() を1回実行 → 設定シート（見出し付き）と保存先フォルダが作られ、IDがログに出る
 *   3) 設定シートにキャリアごとの行を書く（Gmail の検索窓と同じ書き方）
 *   4) 保存先フォルダを、サービスアカウント（enkan-robot-reader@...）に共有する
 *   5) ウェブアプリとして公開し、そのURLをアプリの設定に貼る（アプリが必要なときに呼ぶ）
 *      ※トリガーで定期実行しても構いませんが、必要なときだけ呼ぶほうが無駄がありません
 *
 * 安全のため、メールの削除も送信もしません（読むのと、ラベルを付けるだけ）。
 */

// 設定シートのあるスプレッドシートID（URLの /d/ と /edit の間）
const CONFIG_SHEET_ID = 'ここにスプレッドシートIDを入れる';

// 設定を書くシート（タブ）の名前
const CONFIG_TAB = '取り込み設定';

// 保存先フォルダなど、アプリと共有する基本設定を書くタブ
// （アプリの画面で入力した値がここに入るので、GAS側で同じ値を書き直さなくてよい）
const BASIC_TAB = '基本設定';

// 保存先フォルダ。既にあるフォルダを使いたいときは、そのフォルダIDを入れる
//（Drive でフォルダを開いたときの URL の folders/ のうしろ）。
// 空のままなら、マイドライブ直下に ROOT_FOLDER_NAME のフォルダを自動で作る。
const ROOT_FOLDER_ID = '';
const ROOT_FOLDER_NAME = '進捗取り込み';

// 取り込み済みの目印。同じメールを二度取り込まないために付ける
const DONE_LABEL = '取り込み済み';

// 設定シートの見出し（この順番で作られる）
// 前半4列は GAS（メール取り込み）が使い、後半はアプリ（貼り付け）が使う。
// 進捗のスプレッドシートが複数（LL進捗反映／N進捗反映）あるため、
// 「どのスプシのどのシートへ貼るか」も行ごとに持たせる。
const CONFIG_HEADERS = [
  'キャリア名', '取り込み方法', 'Gmail検索条件', '添付の絞り込み(正規表現)', '有効',
  '貼り付け先スプシID', '元データシート名', '投入用シート名', '確認用シート名',
  '解錠パスワードの名前', 'ファイルの見出し行数', '貼り付け先の見出し行数',
  '取り込みロボット名', 'オブジェクトAPI名', '外部IDキー',
  // 下の3つは、アプリが検索条件を組み立てるための控え（GASは使わない）
  'メール件名', 'メール差出人', 'メール何日以内',
];

/** 1回だけ実行：設定シートと保存先フォルダを用意する */
function setup() {
  const ss = SpreadsheetApp.openById(CONFIG_SHEET_ID);
  let sheet = ss.getSheetByName(CONFIG_TAB);
  if (!sheet) {
    sheet = ss.insertSheet(CONFIG_TAB);
    sheet.getRange(1, 1, 1, CONFIG_HEADERS.length).setValues([CONFIG_HEADERS]);
    sheet.getRange(2, 1, 1, CONFIG_HEADERS.length).setValues([[
      'ドコモ光',
      'メールの添付',
      'from:example@docomo.example.jp subject:進捗 has:attachment newer_than:3d',
      '\\.(zip|xlsx|csv)$',
      'TRUE',
      '（N進捗反映のスプシID）',
      'GMO ドコモ元データ',
      'GMO ドコモ進捗反映（一括）',
      'GMO ドコモ進捗反映',
      '',
      '1',
      '1',
      '',
      '',
      '',
    ]]);
    sheet.setFrozenRows(1);
    Logger.log('設定シート「' + CONFIG_TAB + '」を作りました。1行目は見出し、2行目は記入例です。');
  }
  const root = getRootFolder_();
  Logger.log('保存先ルートフォルダID: ' + root.getId());
  Logger.log('このフォルダを enkan-robot-reader@... に共有してください（閲覧者でOK）');
}

/** 設定シートを読んで、取り込みルールの配列にする */
function readConfig_() {
  const ss = SpreadsheetApp.openById(CONFIG_SHEET_ID);
  const sheet = ss.getSheetByName(CONFIG_TAB);
  if (!sheet) throw new Error('設定シート「' + CONFIG_TAB + '」がありません。先に setup() を実行してください。');
  const values = sheet.getDataRange().getValues();
  const rules = [];
  const head = values[0].map(function (h) { return String(h).trim(); });
  const col = function (row, name) {
    const i = head.indexOf(name);
    return i >= 0 ? String(row[i] || '').trim() : '';
  };
  for (let i = 1; i < values.length; i++) {
    const name = col(values[i], 'キャリア名');
    const method = col(values[i], '取り込み方法');
    const query = col(values[i], 'Gmail検索条件');
    const files = col(values[i], '添付の絞り込み(正規表現)');
    const enabled = col(values[i], '有効');
    // メール以外（サイトからダウンロード・手動アップロード）は、この仕掛けの対象外
    if (method && method !== 'メールの添付') continue;
    if (!name || !query) continue;
    // 「有効」列が FALSE / いいえ / 0 のときは飛ばす（消さずに一時停止できる）
    const off = String(enabled).toUpperCase();
    if (off === 'FALSE' || off === 'いいえ' || off === '0') continue;
    rules.push({ name: String(name).trim(), query: String(query).trim(), files: String(files || '').trim() });
  }
  return rules;
}

/** 本体：条件に合うメールの添付を、キャリアごとのフォルダに保存する */
function importProgressAttachments() {
  const root = getRootFolder_();
  const label = getOrCreateLabel_(DONE_LABEL);
  const rules = readConfig_();
  const report = [];

  rules.forEach(function (c) {
    const folder = getOrCreateFolder_(root, c.name);
    // 取り込み済みラベルが付いたものは対象外にする（二重取り込みの防止）
    const q = c.query + ' -label:' + DONE_LABEL;
    const threads = GmailApp.search(q, 0, 20);
    let saved = 0;

    threads.forEach(function (thread) {
      thread.getMessages().forEach(function (msg) {
        msg.getAttachments().forEach(function (att) {
          const fileName = att.getName();
          if (c.files && !new RegExp(c.files, 'i').test(fileName)) return;
          // 受信日時を頭に付ける＝どのメール由来か分かる／同名でも上書きされない
          const stamp = Utilities.formatDate(msg.getDate(), 'JST', 'yyyyMMdd_HHmm');
          folder.createFile(att.copyBlob().setName(stamp + '_' + fileName));
          saved++;
        });
      });
      thread.addLabel(label);
    });
    report.push({ carrier: c.name, saved: saved, threads: threads.length });
    Logger.log(c.name + '：メール' + threads.length + '件 → 添付' + saved + '件を保存');
  });

  return report;
}

/**
 * 🌐 アプリから呼び出す入口（ウェブアプリとして公開して使う）
 *
 * 時間ごとの自動実行（トリガー）ではなく、アプリが「いま必要」なタイミングで呼ぶ。
 * そのほうが待ち時間がなく、無駄な実行もしない。
 *
 * 公開のしかた：
 *   デプロイ → 新しいデプロイ → 種類「ウェブアプリ」
 *   次のユーザーとして実行：自分（info@lifeap.co.jp）
 *   アクセスできるユーザー：全員      ← URLを知っている人だけが使える状態
 *   発行されたURLを、アプリの「進捗反映」設定に貼る
 *
 * ⚠️ URLを知っていれば誰でも呼べてしまうため、合言葉（トークン）で守る。
 *    合言葉は「基本設定」タブの『GAS合言葉』に書く（アプリが自動で書き込む）。
 *
 * 使い方（アプリが自動で組み立てるので、人が打つ必要はない）：
 *   ...?token=合言葉&action=intake        添付の取り込みを今すぐ実行
 *   ...?token=合言葉&action=code          認証コードの取り出しを今すぐ実行
 */
function doGet(e) {
  const params = (e && e.parameter) || {};
  const expected = readBasic_('GAS合言葉');
  if (!expected || params.token !== expected) {
    return jsonOut_({ error: '合言葉が違います' });
  }
  try {
    if (params.action === 'code') {
      return jsonOut_({ ok: true, result: fetchAuthCodes() });
    }
    if (params.action === 'intake') {
      return jsonOut_({ ok: true, result: importProgressAttachments() });
    }
    // 指定が無ければ両方
    const a = importProgressAttachments();
    const b = fetchAuthCodes();
    return jsonOut_({ ok: true, intake: a, code: b });
  } catch (err) {
    return jsonOut_({ error: String(err) });
  }
}

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** 基本設定タブから値を1つ読む（A列＝項目名、B列＝値）。無ければ空文字。 */
function readBasic_(name) {
  try {
    const sheet = SpreadsheetApp.openById(CONFIG_SHEET_ID).getSheetByName(BASIC_TAB);
    if (!sheet) return '';
    const values = sheet.getDataRange().getValues();
    for (let i = 0; i < values.length; i++) {
      if (String(values[i][0]).trim() === name) return String(values[i][1] || '').trim();
    }
  } catch (e) {
    // 基本設定タブがまだ無いときは、下の既定値にフォールバックする
  }
  return '';
}

/** 保存先のルートフォルダを返す。
 *  優先順位：基本設定タブ（アプリで入力した値）→ ROOT_FOLDER_ID → 名前で自動作成。
 *  アプリ側と二重に設定しなくて済むよう、まず共有の設定タブを見る。 */
function getRootFolder_() {
  const fromSheet = readBasic_('取り込みフォルダID');
  if (fromSheet) return DriveApp.getFolderById(fromSheet);
  if (ROOT_FOLDER_ID) return DriveApp.getFolderById(ROOT_FOLDER_ID);
  return getOrCreateFolder_(DriveApp.getRootFolder(), ROOT_FOLDER_NAME);
}

function getOrCreateFolder_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

// ==========================================================
// 🔐 ここから：ログイン時の認証コードをメールから取り出す
// ==========================================================
/**
 * 進捗を取りに行くロボットが「メールに届いた認証コード」を自分で入力できるようにする。
 * ロボットにメールを読ませる代わりに、GASが読んでセルに書き、ロボットはセルを見る。
 *
 * 使い方：
 *   1) setupAuthCode() を1回実行 → 「認証コード設定」「認証コード」タブができる
 *   2) 設定はアプリ（進捗反映タブ）からも編集できる
 *   3) fetchAuthCodes() を 1分おきのトリガーに設定する
 *
 * ⚠️ 認証コードは短時間で失効するため、受信から SEARCH_MINUTES 分以内のメールだけを見る。
 */
const AUTH_CONFIG_TAB = '認証コード設定';
const AUTH_CODE_TAB = '認証コード';

// 何分以内に届いたメールを対象にするか（古いコードを拾わないための保険）
const SEARCH_MINUTES = 10;

const AUTH_CONFIG_HEADERS = ['キャリア名', 'Gmail検索条件', '抜き出しパターン(正規表現)', '有効'];
const AUTH_CODE_HEADERS = ['キャリア名', 'コード', '取得時刻'];

/** 1回だけ実行：2つのタブを用意する */
function setupAuthCode() {
  const ss = SpreadsheetApp.openById(CONFIG_SHEET_ID);
  let cfg = ss.getSheetByName(AUTH_CONFIG_TAB);
  if (!cfg) {
    cfg = ss.insertSheet(AUTH_CONFIG_TAB);
    cfg.getRange(1, 1, 1, AUTH_CONFIG_HEADERS.length).setValues([AUTH_CONFIG_HEADERS]);
    cfg.getRange(2, 1, 1, AUTH_CONFIG_HEADERS.length).setValues([[
      'ドコモ光',
      'from:no-reply@example.jp subject:認証コード',
      '認証コード[^0-9]{0,10}([0-9]{4,8})',
      'TRUE',
    ]]);
    cfg.setFrozenRows(1);
  }
  let out = ss.getSheetByName(AUTH_CODE_TAB);
  if (!out) {
    out = ss.insertSheet(AUTH_CODE_TAB);
    out.getRange(1, 1, 1, AUTH_CODE_HEADERS.length).setValues([AUTH_CODE_HEADERS]);
    out.setFrozenRows(1);
  }
  Logger.log('「' + AUTH_CONFIG_TAB + '」に検索条件を書いて、fetchAuthCodes を1分おきのトリガーにしてください。');
}

/** 本体：条件に合う最新メールからコードを抜き出し、「認証コード」タブに書く */
function fetchAuthCodes() {
  const report = [];
  const ss = SpreadsheetApp.openById(CONFIG_SHEET_ID);
  const cfg = ss.getSheetByName(AUTH_CONFIG_TAB);
  if (!cfg) throw new Error('「' + AUTH_CONFIG_TAB + '」がありません。先に setupAuthCode() を実行してください。');
  const out = ss.getSheetByName(AUTH_CODE_TAB) || ss.insertSheet(AUTH_CODE_TAB);

  const values = cfg.getDataRange().getValues();
  const limit = new Date(Date.now() - SEARCH_MINUTES * 60 * 1000);

  for (let i = 1; i < values.length; i++) {
    const name = String(values[i][0] || '').trim();
    const query = String(values[i][1] || '').trim();
    const pattern = String(values[i][2] || '').trim();
    const enabled = String(values[i][3] || 'TRUE').toUpperCase();
    if (!name || !query || enabled === 'FALSE') continue;

    // newer_than:1h で粗く絞り、正確な時刻は下で判定する
    const threads = GmailApp.search(query + ' newer_than:1h', 0, 5);
    let found = null, foundAt = null;
    threads.forEach(function (t) {
      t.getMessages().forEach(function (m) {
        if (m.getDate() < limit) return;                 // 古いメールは使わない
        if (foundAt && m.getDate() <= foundAt) return;    // いちばん新しいものを採用
        const body = m.getPlainBody() || m.getBody() || '';
        const re = new RegExp(pattern || '([0-9]{4,8})');
        const hit = re.exec(body);
        if (hit) {
          found = hit[1] || hit[0];
          foundAt = m.getDate();
        }
      });
    });

    if (!found) continue;

    // 同じキャリアの行があれば上書き、無ければ追加
    const rows = out.getDataRange().getValues();
    let target = -1;
    for (let r = 1; r < rows.length; r++) {
      if (String(rows[r][0]).trim() === name) { target = r + 1; break; }
    }
    const stamp = Utilities.formatDate(foundAt, 'JST', 'yyyy/MM/dd HH:mm:ss');
    // ⚠️ コードは「文字」として書く。数字のまま書くと 0042 が 42 になり、
    //    先頭の0が消えて桁数が足りなくなる。
    if (target <= 0) {
      out.appendRow([name, '', stamp]);
      target = out.getLastRow();
    }
    out.getRange(target, 1, 1, 3).setNumberFormats([['@', '@', '@']]);
    out.getRange(target, 1, 1, 3).setValues([[name, found, stamp]]);
    report.push({ name: name, at: stamp });
    Logger.log(name + '：コードを取得しました（' + stamp + '）');
  }
  return report;
}
