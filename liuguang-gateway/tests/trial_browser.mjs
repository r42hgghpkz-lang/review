/** One mobile-viewport submit through a caller-supplied gateway. Prints no passwords or bearer tokens. */
import puppeteer from 'puppeteer-core';

const password = process.env.LIUGUANG_TEST_PASSWORD || '';
const pageUrl = process.env.PAGE_URL || '';
const chromePath = process.env.CHROME_PATH || '/usr/bin/google-chrome';
if (!password || !pageUrl) {
  console.log('FAIL missing-env');
  process.exit(1);
}

const browser = await puppeteer.launch({
  executablePath: chromePath,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
});
try {
  const mod = await import('puppeteer-core');
  const device = mod.KnownDevices?.['iPhone 13'] || null;
  const page = await browser.newPage();
  if (device) await page.emulate(device);
  else await page.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2});
  page.setDefaultTimeout(30000);
  let idempotency = '';
  let sawBearer = false;
  let sawCookie = false;
  page.on('request', (req) => {
    if (req.method() !== 'POST' || !req.url().includes('/v1/submissions')) return;
    const headers = req.headers();
    if (headers['idempotency-key']) idempotency = headers['idempotency-key'];
    if ((headers.authorization || '').toLowerCase().startsWith('bearer ')) sawBearer = true;
    if (headers.cookie) sawCookie = true;
  });
  await page.evaluateOnNewDocument(() => {
    localStorage.setItem('liuguang-checklist-20261109-v1', JSON.stringify({
      version: 1,
      answers: {'2.1': {status: 'parked', text: 'SYNTHETIC-ONLY-KEEP', parkUntil: '2026-12-01'}},
    }));
  });
  await page.goto(pageUrl, {waitUntil: 'domcontentloaded'});
  await page.waitForSelector('[data-qid="1.1"] textarea');
  await page.evaluate((pw) => {
    const ta = document.querySelector('[data-qid="1.1"] textarea');
    ta.value = 'SYNTHETIC-ONLY-TUNNEL';
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    document.getElementById('submit-password').value = pw;
  }, password);
  await page.click('#btn-submit');
  await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'ok', {timeout: 30000});
  const snap = await page.evaluate((pw) => {
    const draft = localStorage.getItem('liuguang-checklist-20261109-v1') || '';
    const status = document.getElementById('submit-status');
    const rect = document.getElementById('btn-submit').getBoundingClientRect();
    return {
      state: status.dataset.state,
      text: status.textContent,
      keep: draft.includes('SYNTHETIC-ONLY-KEEP'),
      tunnel: draft.includes('SYNTHETIC-ONLY-TUNNEL'),
      passwordInDraft: draft.includes(pw),
      cookie: document.cookie,
      width: window.innerWidth,
      btnH: rect.height,
      btnBottom: rect.bottom,
      innerH: window.innerHeight,
    };
  }, password);
  const ok = snap.state === 'ok' && snap.text.includes('已写入私密收据') && snap.text.includes('没有清空')
    && snap.keep && snap.tunnel && !snap.passwordInDraft && snap.cookie === ''
    && sawBearer && !sawCookie && snap.width <= 430 && snap.btnH >= 44 && snap.btnBottom <= snap.innerH + 1
    && /^[A-Za-z0-9_-]{16,80}$/.test(idempotency);
  console.log(ok ? 'PASS tunnel-mobile-submit' : 'FAIL tunnel-mobile-submit');
  const receipt = (snap.text.match(/lg-[A-Za-z0-9-]+/) || [''])[0];
  console.log('RECEIPT ' + receipt);
  console.log('IDEMPOTENCY ' + idempotency);
  console.log('VIEWPORT ' + snap.width);
  if (!ok) process.exit(1);
} finally {
  await browser.close();
}
