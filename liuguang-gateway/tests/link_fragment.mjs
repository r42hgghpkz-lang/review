/** #k= link auth. The secret is never printed. Synthetic draft only. */
import puppeteer from 'puppeteer-core';

const password = process.env.LIUGUANG_TEST_PASSWORD || '';
const pageBase = process.env.PAGE_BASE || '';
const chromePath = process.env.CHROME_PATH || '/usr/bin/google-chrome';
if (!password || !pageBase) {
  console.log('FAIL missing-env');
  process.exit(1);
}
const KEY = 'liuguang-checklist-20261109-v1';
const failures = [];
function check(name, ok) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name);
  if (!ok) failures.push(name);
}

const browser = await puppeteer.launch({
  executablePath: chromePath,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
});

async function openWithKey(fragmentKey) {
  const page = await browser.newPage();
  await page.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true});
  page.setDefaultTimeout(15000);
  await page.evaluateOnNewDocument((key) => {
    localStorage.setItem(key, JSON.stringify({
      version: 1,
      title: '流光 · 问题勾选｜2026-11-09',
      savedAt: '2026-11-09T01:00:00.000Z',
      tzHint: 'Asia/Shanghai',
      answers: {'1.1': {status: 'answered', text: 'SYNTHETIC-ONLY-LINK', parkUntil: '', updatedAt: '2026-11-09T01:02:03.000Z'}},
    }));
  }, KEY);
  let idempotency = '';
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/v1/submissions')) {
      idempotency = req.headers()['idempotency-key'] || idempotency;
    }
  });
  const url = pageBase + '#k=' + encodeURIComponent(fragmentKey);
  await page.goto(url, {waitUntil: 'networkidle0'});
  await page.waitForFunction(() => document.getElementById('c-total').textContent === '112');
  await page.waitForFunction(() => {
    const text = document.getElementById('submit-status').textContent || '';
    return text.includes('不必再填口令') || text.includes('未被接受') || text.includes('网关还没配置');
  });
  return {page, getIdempotency: () => idempotency};
}

function leaked(storeText) {
  return storeText.includes(password);
}

try {
  {
    const {page, getIdempotency} = await openWithKey(password);
    const snap = await page.evaluate((key) => {
      const label = document.getElementById('submit-password-label');
      const input = document.getElementById('submit-password');
      const draft = localStorage.getItem(key) || '';
      const session = sessionStorage.getItem('liuguang-submit-session') || '';
      return {
        hash: location.hash,
        hidden: label.hidden === true,
        disabled: input.disabled === true,
        inputValue: input.value,
        draft,
        session,
        cookie: document.cookie,
        status: document.getElementById('submit-status').textContent,
      };
    }, KEY);
    check('hash-removed', snap.hash === '');
    check('password-box-not-blocking', snap.hidden && snap.disabled && snap.inputValue === '');
    check('secret-not-in-draft', !leaked(snap.draft) && snap.draft.includes('SYNTHETIC-ONLY-LINK'));
    check('secret-not-in-session', !leaked(snap.session));
    check('no-cookie', snap.cookie === '');
    check('link-status-has-no-secret', !leaked(snap.status) && snap.status.includes('不必再填口令'));
    await page.click('#btn-submit');
    await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'ok', {timeout: 10000});
    const done = await page.evaluate((key) => {
      const draft = localStorage.getItem(key) || '';
      const session = sessionStorage.getItem('liuguang-submit-session') || '';
      const status = document.getElementById('submit-status').textContent;
      return {draft, session, status, hash: location.hash};
    }, KEY);
    check('fragment-submit-ok', done.status.includes('已写入私密收据') && done.status.includes('没有清空') && !leaked(done.status));
    check('draft-retained-after-submit', done.draft.includes('SYNTHETIC-ONLY-LINK') && !leaked(done.draft));
    check('secret-still-absent', done.hash === '' && !leaked(done.session));
    const idem = getIdempotency();
    check('idempotency-captured', /^[A-Za-z0-9_-]{16,80}$/.test(idem));
    await page.evaluate((pending, value) => {
      localStorage.setItem(pending, JSON.stringify({idempotencyKey: value, startedAt: '2026-11-09T01:00:00.000Z'}));
    }, 'liuguang-checklist-20261109-v1-pending-submit', idem);
    await page.click('#btn-submit');
    await page.waitForFunction(() => document.getElementById('submit-status').textContent.includes('同一次重试'), {timeout: 10000});
    const replay = await page.evaluate((key) => ({
      status: document.getElementById('submit-status').textContent,
      draft: localStorage.getItem(key) || '',
    }), KEY);
    check('fragment-replay-same-receipt', replay.status.includes('同一次重试') && replay.draft.includes('SYNTHETIC-ONLY-LINK') && !leaked(replay.status + replay.draft));
    await page.close();
  }
  {
    const {page} = await openWithKey('not-the-secret');
    await page.click('#btn-submit');
    await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'fail', {timeout: 10000});
    const snap = await page.evaluate((key) => {
      const status = document.getElementById('submit-status');
      return {
        state: status.dataset.state,
        text: status.textContent,
        draft: localStorage.getItem(key) || '',
        hash: location.hash,
      };
    }, KEY);
    check('bad-fragment-fails', snap.state === 'fail' && snap.text.includes('未被接受') && !snap.text.includes('已写入私密收据'));
    check('bad-fragment-keeps-draft', snap.draft.includes('SYNTHETIC-ONLY-LINK') && snap.hash === '' && !snap.text.includes('not-the-secret') && !snap.draft.includes('not-the-secret'));
    await page.close();
  }
} finally {
  await browser.close();
}
if (failures.length) {
  console.log('LINK_FRAGMENT_FAILED ' + failures.join(','));
  process.exit(1);
}
console.log('LINK_FRAGMENT_OK');
