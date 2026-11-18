/**
 * 見守りカメラ → Googleスプレッドシート：ミルク記録の受け口（Google Apps Script）
 *
 * 使い方（くわしくは docs/milk.md）
 *  1. 記録用のスプレッドシートを作り、「拡張機能 → Apps Script」を開く
 *  2. このファイルの中身を貼り付け、下の TOKEN を自分だけの合言葉に変える
 *  3. 関数「setup」を一度だけ実行（シートと見出し、日別まとめを作る）
 *  4. 「デプロイ → 新しいデプロイ → ウェブアプリ」
 *       次のユーザーとして実行：自分 ／ アクセスできるユーザー：全員
 *     → 表示されたURLと TOKEN を、ラズパイの config/secrets.toml の [sheets] に書く
 *
 * 受け取るデータ（JSON）
 *  {"token":"…","action":"add","id":"…","time":"2026-11-18T19:16:05+09:00","amount":100,"kind":"ミルク","source":"ベッド横の画面"}
 *  {"token":"…","action":"delete","id":"…"}
 */

const TOKEN = 'ここを自分だけの合言葉に変える';   // secrets.toml の token と同じにする
const SHEET_NAME = 'ミルク';
const SUMMARY_NAME = '日別まとめ';
const HEADER = ['日時', '量(ml)', '種類', '記録した場所', 'ID'];

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.token !== TOKEN) return reply({ ok: false, error: '合言葉（token）が違います' });
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      const sh = getSheet();
      if (d.action === 'add') {
        if (findRow(sh, d.id) < 0) {          // 同じ記録が二重に届いても1行だけ
          sh.appendRow([new Date(d.time), Number(d.amount), d.kind || 'ミルク', d.source || '', d.id]);
          sh.getRange(sh.getLastRow(), 1).setNumberFormat('yyyy/MM/dd HH:mm');
        }
        return reply({ ok: true });
      }
      if (d.action === 'delete') {
        const row = findRow(sh, d.id);
        if (row > 0) sh.deleteRow(row);
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

/** 最初に一度だけ実行：シート・見出し・日別まとめを用意する */
function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ss.setSpreadsheetTimeZone('Asia/Tokyo');
  const sh = getSheet();
  sh.setFrozenRows(1);
  sh.setColumnWidth(1, 150);
  let sum = ss.getSheetByName(SUMMARY_NAME);
  if (!sum) sum = ss.insertSheet(SUMMARY_NAME);
  sum.clear();
  sum.getRange('A1').setFormula(
    "=QUERY(" + SHEET_NAME + "!A:C,\"select toDate(A), sum(B), count(B) where A is not null and C = 'ミルク' " +
    "group by toDate(A) order by toDate(A) desc label toDate(A) '日付', sum(B) '合計(ml)', count(B) '回数' format toDate(A) 'yyyy/MM/dd'\",1)");
  sum.setColumnWidth(1, 120);
}

/** 動作確認用：エディタで実行するとテスト行を1件追加して、すぐ消す */
function selfTest() {
  const sh = getSheet();
  const id = 'test-' + Date.now();
  sh.appendRow([new Date(), 1, 'テスト', 'selfTest', id]);
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
  const ids = sh.getRange(2, 5, sh.getLastRow() - 1, 1).getValues();
  for (let i = ids.length - 1; i >= 0; i--) {
    if (String(ids[i][0]) === String(id)) return i + 2;
  }
  return -1;
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
