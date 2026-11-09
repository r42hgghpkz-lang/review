/** Prove an existing liuguang-checklist-20261109-v1 draft survives loading the new page. Synthetic values only. */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

const pageDir = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../liuguang');
const questions = JSON.parse(fs.readFileSync(path.join(pageDir, 'questions.json'), 'utf8'));
const ids = questions.flatMap((sec) => sec.items.map((item) => item.id));
if (ids.length !== 112) {
  console.log('FAIL id-count ' + ids.length);
  process.exit(1);
}
const KEY = 'liuguang-checklist-20261109-v1';
const answers = {};
ids.forEach((id, i) => {
  const status = i % 3 === 0 ? 'answered' : (i % 3 === 1 ? 'parked' : 'open');
  answers[id] = {
    status,
    text: status === 'open' ? '' : ('SYNTHETIC-ONLY-' + id),
    parkUntil: status === 'parked' ? '2026-12-01' : '',
    updatedAt: '2026-11-09T01:02:03.000Z',
  };
});
answers['1.1'].extraNote = 'SYNTHETIC-ONLY-EXTRA';
const payload = {
  version: 1,
  title: '流光 · 问题勾选｜2026-11-09',
  savedAt: '2026-11-09T01:00:00.000Z',
  tzHint: 'Asia/Shanghai',
  answers,
};
const raw = JSON.stringify(payload);
const fields = ['status', 'text', 'parkUntil', 'updatedAt'];

const server = http.createServer((req, res) => {
  const url = req.url.split('?')[0];
  const name = {
    '/review/liuguang': 'index.html',
    '/review/liuguang/': 'index.html',
    '/review/liuguang/index.html': 'index.html',
    '/review/liuguang/gateway.json': 'gateway.json',
    '/review/liuguang/questions.json': 'questions.json',
  }[url];
  if (!name) {
    res.writeHead(404);
    res.end('not found');
    return;
  }
  const body = fs.readFileSync(path.join(pageDir, name));
  res.writeHead(200, {'Content-Type': name.endsWith('.json') ? 'application/json' : 'text/html; charset=utf-8', 'Cache-Control': 'no-store'});
  res.end(body);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
});
let failed = false;
function check(name, ok, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' ' + detail : ''));
  if (!ok) failed = true;
}
try {
  const page = await browser.newPage();
  await page.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true});
  await page.evaluateOnNewDocument((key, value) => localStorage.setItem(key, value), KEY, raw);
  await page.goto(`http://127.0.0.1:${port}/review/liuguang/`, {waitUntil: 'networkidle0'});
  await page.waitForFunction(() => document.getElementById('c-total').textContent === '112');
  const afterLoad = await page.evaluate((key) => localStorage.getItem(key), KEY);
  check('load-does-not-rewrite-draft', afterLoad === raw);
  const stored = JSON.parse(afterLoad);
  check('storage-key', true, KEY);
  check('top-version', stored.version === 1);
  check('top-title', stored.title === '流光 · 问题勾选｜2026-11-09');
  check('top-savedAt', stored.savedAt === '2026-11-09T01:00:00.000Z');
  check('top-tzHint', stored.tzHint === 'Asia/Shanghai');
  check('answer-count', Object.keys(stored.answers).length === 112, String(Object.keys(stored.answers).length));
  let fieldOk = true;
  const missing = [];
  for (const id of ids) {
    const row = stored.answers[id];
    for (const field of fields) {
      if (!row || !Object.prototype.hasOwnProperty.call(row, field) || row[field] !== answers[id][field]) {
        fieldOk = false;
        missing.push(id + '.' + field);
        break;
      }
    }
  }
  check('every-id-status-text-parkUntil-updatedAt', fieldOk, missing.slice(0, 5).join(','));
  check('extra-field-kept', stored.answers['1.1'].extraNote === 'SYNTHETIC-ONLY-EXTRA');
  const dom = await page.evaluate(() => {
    const card = document.querySelector('[data-qid="1.1"]');
    return {
      text: card.querySelector('textarea').value,
      park: document.querySelector('[data-qid="1.2"] input[type=date]').value,
      status: card.dataset.status,
    };
  });
  check('dom-shows-stored-text', dom.text === 'SYNTHETIC-ONLY-1.1' && dom.status === 'answered');
  check('dom-shows-parkUntil', dom.park === '2026-12-01');
  const beforeEdit = JSON.parse(afterLoad);
  await page.evaluate(() => {
    const ta = document.querySelector('[data-qid="1.1"] textarea');
    ta.value = 'SYNTHETIC-ONLY-1.1-EDIT';
    ta.dispatchEvent(new Event('input', {bubbles: true}));
  });
  await new Promise((resolve) => setTimeout(resolve, 500));
  const editedRaw = await page.evaluate((key) => localStorage.getItem(key), KEY);
  const edited = JSON.parse(editedRaw);
  check('key-still-present', typeof editedRaw === 'string' && editedRaw.length > 0);
  check('edited-text', edited.answers['1.1'].text === 'SYNTHETIC-ONLY-1.1-EDIT');
  check('edited-status-park-extra', edited.answers['1.1'].status === 'answered' && edited.answers['1.1'].parkUntil === '' && edited.answers['1.1'].extraNote === 'SYNTHETIC-ONLY-EXTRA');
  check('edited-updatedAt-moved', edited.answers['1.1'].updatedAt !== beforeEdit.answers['1.1'].updatedAt);
  let others = true;
  for (const id of ids) {
    if (id === '1.1') continue;
    if (JSON.stringify(edited.answers[id]) !== JSON.stringify(beforeEdit.answers[id])) others = false;
  }
  check('other-111-answers-unchanged', others);
  check('top-fields-after-edit', edited.version === 1 && edited.title === beforeEdit.title && edited.tzHint === beforeEdit.tzHint);
  check('clear-not-called', editedRaw.length > 1000);
  const pending = await page.evaluate(() => localStorage.getItem('liuguang-checklist-20261109-v1-pending-submit'));
  check('load-does-not-create-submit-key', pending === null);
} finally {
  await browser.close();
  server.close();
}
if (failed) {
  console.log('DRAFT_COMPAT_FAILED');
  process.exit(1);
}
console.log('DRAFT_COMPAT_OK');
