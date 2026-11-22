/**
 * 見守りカメラ → Googleスプレッドシート：授乳（ミルク・母乳）記録の受け口（Google Apps Script）
 *
 * 使い方（くわしくは docs/milk.md）
 *  1. 記録用のスプレッドシートを作り、「拡張機能 → Apps Script」を開く
 *  2. このファイルの中身を貼り付け、下の TOKEN を自分だけの合言葉に変える
 *  3. 関数「setup」を一度だけ実行（シートと見出し、日別まとめを作る。古い形式のシートは新しい形式に直す）
 *  4. 「デプロイ → 新しいデプロイ → ウェブアプリ」
 *       次のユーザーとして実行：自分 ／ アクセスできるユーザー：全員
 *     → 表示されたURLと TOKEN を、ラズパイの config/secrets.toml の [sheets] に書く
 *  ※ 貼り直したときは「デプロイ → デプロイを管理 → 編集 → バージョン：新しいバージョン」（URLは変わりません）
 *
 * 受け取るデータ（JSON）
 *  {"token":"…","action":"upsert","id":"…","start":"2026-11-18T19:16:05+09:00","end":"…（授乳中は空）",
 *   "milk":80,"breast":30,"total":110,"source":"ベッド横の画面"}
 *  {"token":"…","action":"delete","id":"…"}
 *  （v2.5 までの {"action":"add","time":…,"amount":…} も受け付けます）
 */

const TOKEN = 'ここを自分だけの合言葉に変える';   // secrets.toml の token と同じにする
const SHEET_NAME = 'ミルク';
const SUMMARY_NAME = '日別まとめ';
const HEADER = ['開始', '終了', 'ミルク(ml)', '母乳(ml)', '合計(ml)', '記録した場所', 'ID'];
const OLD_HEADER = ['日時', '量(ml)', '種類', '記録した場所', 'ID'];
const ID_COL = 7;

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.token !== TOKEN) return reply({ ok: false, error: '合言葉（token）が違います' });
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      const sh = getSheet();
      migrateOld(sh);                    // 古い形式のままなら、先に新しい形式に直す
      if (d.action === 'add') {          // 旧形式：ミルクだけ・終わった記録
        d.action = 'upsert'; d.start = d.time; d.end = d.time;
        d.milk = Number(d.amount); d.breast = 0; d.total = Number(d.amount);
      }
      if (d.action === 'upsert') {
        const row = [toDate(d.start), toDate(d.end), num(d.milk), num(d.breast), num(d.total), d.source || '', d.id];
        let r = findRow(sh, d.id);
        if (r < 0) { sh.appendRow(row); r = sh.getLastRow(); }
        else sh.getRange(r, 1, 1, row.length).setValues([row]);
        sh.getRange(r, 1, 1, 2).setNumberFormat('yyyy/MM/dd HH:mm');
        return reply({ ok: true });
      }
      if (d.action === 'delete') {
        const r = findRow(sh, d.id);
        if (r > 0) sh.deleteRow(r);
        return reply({ ok: true });
      }
      return reply({ ok: false, error: '不明な action: ' + d.action });
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  }
}

/** 最初に一度だけ実行：シート・見出し・日別まとめを用意する（古い形式なら新しい形式に直す） */
function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ss.setSpreadsheetTimeZone('Asia/Tokyo');
  const sh = getSheet();
  migrateOld(sh);
  sh.setFrozenRows(1);
  sh.setColumnWidth(1, 140); sh.setColumnWidth(2, 140);
  if (sh.getLastRow() > 1) sh.getRange(2, 1, sh.getLastRow() - 1, 2).setNumberFormat('yyyy/MM/dd HH:mm');
  let sum = ss.getSheetByName(SUMMARY_NAME);
  if (!sum) sum = ss.insertSheet(SUMMARY_NAME);
  sum.clear();
  sum.getRange('A1').setFormula(
    "=IFERROR(QUERY('" + SHEET_NAME + "'!A:E,\"select toDate(A), sum(E), sum(C), sum(D), count(E) where A is not null and E is not null " +
    "group by toDate(A) order by toDate(A) desc label toDate(A) '日付', sum(E) '合計(ml)', sum(C) 'ミルク(ml)', sum(D) '母乳(ml)', count(E) '回数' " +
    "format toDate(A) 'yyyy/MM/dd'\",1),\"まだ記録がありません\")");
  sum.getRange('A2:A').setNumberFormat('yyyy/MM/dd');   // 日付が数字で出ないように
  sum.setColumnWidth(1, 120);
}

/** v2.5 までの「日時・量・種類・記録した場所・ID」を、新しい列に並べ替える（ミルクとして引き継ぐ） */
function migrateOld(sh) {
  const head = sh.getRange(1, 1, 1, OLD_HEADER.length).getValues()[0];
  if (String(head[0]) !== OLD_HEADER[0] || String(head[1]) !== OLD_HEADER[1]) return;
  const n = sh.getLastRow() - 1;
  const rows = n > 0 ? sh.getRange(2, 1, n, OLD_HEADER.length).getValues() : [];
  sh.clear();
  sh.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
  if (rows.length) {
    const conv = rows.map((r) => [r[0], r[0], Number(r[1]), 0, Number(r[1]), r[3], r[4]]);
    sh.getRange(2, 1, conv.length, HEADER.length).setValues(conv);
  }
  Logger.log('古い形式の ' + rows.length + ' 件を新しい形式に直しました');
}

/** 動作確認用：エディタで実行するとテスト行を1件追加して、すぐ消す */
function selfTest() {
  const sh = getSheet();
  const id = 'test-' + Date.now();
  sh.appendRow([new Date(), new Date(), 1, 0, 1, 'selfTest', id]);
  SpreadsheetApp.flush();
  sh.deleteRow(findRow(sh, id));
  Logger.log('OK：シートへの書き込みと削除ができました');
}

function getSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) sh = ss.insertSheet(SHEET_NAME, 0);
  if (sh.getLastRow() === 0) sh.appendRow(HEADER);
  return sh;
}

function findRow(sh, id) {
  if (!id || sh.getLastRow() < 2) return -1;
  const ids = sh.getRange(2, ID_COL, sh.getLastRow() - 1, 1).getValues();
  for (let i = ids.length - 1; i >= 0; i--) {
    if (String(ids[i][0]) === String(id)) return i + 2;
  }
  return -1;
}

function toDate(s) { return s ? new Date(s) : ''; }
function num(v) { return v === '' || v === null || v === undefined ? '' : Number(v); }

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
