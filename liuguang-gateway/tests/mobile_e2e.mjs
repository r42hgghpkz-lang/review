import puppeteer from 'puppeteer-core';

const password = process.env.LIUGUANG_TEST_PASSWORD || '';
const pageUrl = process.env.PAGE_URL || '';
const timeoutUrl = process.env.TIMEOUT_URL || '';
const plainUrl = process.env.PLAIN_URL || '';
const expectedDigest = process.env.EXPECTED_DIGEST || '';
const chromePath = process.env.CHROME_PATH || '/usr/bin/google-chrome';
if (!password || !pageUrl || !timeoutUrl || !plainUrl || !expectedDigest) {
  console.log('FAIL missing-env');
  process.exit(1);
}

const failures = [];
function check(name, ok, detail) {
  if (ok) console.log('PASS ' + name);
  else {
    console.log('FAIL ' + name + (detail ? ' ' + detail : ''));
    failures.push(name);
  }
}

async function launch() {
  let device = null;
  try {
    const mod = await import('puppeteer-core');
    device = mod.KnownDevices?.['iPhone 13'] || puppeteer.KnownDevices?.['iPhone 13'] || null;
  } catch (e) {
    device = null;
  }
  const browser = await puppeteer.launch({
    executablePath: chromePath,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  return {browser, device};
}

async function newPage(browser, device) {
  const page = await browser.newPage();
  if (device) await page.emulate(device);
  else {
    await page.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2});
    await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1');
  }
  page.setDefaultTimeout(15000);
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(String(err).slice(0, 180)));
  return {page, pageErrors};
}

async function seed(page) {
  await page.evaluateOnNewDocument((key) => {
    localStorage.setItem(key, JSON.stringify({
      version: 1,
      answers: {'2.1': {status: 'parked', text: 'SYNTHETIC-ONLY-KEEP', parkUntil: '2026-12-01', updatedAt: '2026-11-09T00:00:00.000Z'}},
    }));
  }, 'liuguang-checklist-20261109-v1');
}

async function snapshot(page) {
  return page.evaluate((pw) => {
    const draft = localStorage.getItem('liuguang-checklist-20261109-v1') || '';
    let token = '';
    try { token = JSON.parse(sessionStorage.getItem('liuguang-submit-session') || 'null').token || ''; } catch (e) { token = ''; }
    const status = document.getElementById('submit-status');
    const btn = document.getElementById('btn-submit');
    const input = document.getElementById('submit-password');
    const rect = btn.getBoundingClientRect();
    return {
      state: status.dataset.state,
      text: status.textContent,
      draftHasKeep: draft.includes('SYNTHETIC-ONLY-KEEP'),
      draftHasAlpha: draft.includes('SYNTHETIC-ONLY-ALPHA'),
      draftHasTimeout: draft.includes('SYNTHETIC-ONLY-TIMEOUT'),
      passwordInDraft: pw ? draft.includes(pw) : false,
      tokenInDraft: token ? draft.includes(token) : false,
      cookie: document.cookie,
      hasSession: token.length > 20,
      pending: localStorage.getItem('liuguang-checklist-20261109-v1-pending-submit') || '',
      btnH: rect.height,
      btnBottom: rect.bottom,
      innerH: window.innerHeight,
      innerW: window.innerWidth,
      scrollW: document.documentElement.scrollWidth,
      font: parseFloat(getComputedStyle(input).fontSize),
      inputType: input.type,
    };
  }, password);
}

async function fill(page, qid, text) {
  await page.waitForSelector(`[data-qid="${qid}"] textarea`);
  await page.evaluate((id, value) => {
    const ta = document.querySelector(`[data-qid="${id}"] textarea`);
    ta.value = value;
    ta.dispatchEvent(new Event('input', {bubbles: true}));
  }, qid, text);
  await page.focus('#submit-password');
  await page.evaluate((pw) => { document.getElementById('submit-password').value = pw; }, password);
}

const seen = {authorization: false, cookieHeader: false, setCookie: false};

function watch(page, origin) {
  page.on('request', (req) => {
    if (!req.url().includes('/v1/submissions') || req.method() !== 'POST') return;
    const headers = req.headers();
    if (headers.authorization && headers.authorization.toLowerCase().startsWith('bearer ')) seen.authorization = true;
    if (headers.cookie) seen.cookieHeader = true;
  });
  page.on('response', (res) => {
    if (res.headers()['set-cookie']) seen.setCookie = true;
  });
  return origin;
}

async function main() {
  const {browser, device} = await launch();
  try {
    check('mobile-device', true, device ? 'iPhone 13 emulation' : 'manual 390x844');
    const origin = new URL(pageUrl).origin;

    {
      const {page, pageErrors} = await newPage(browser, device);
      await seed(page);
      watch(page, origin);
      await page.goto(pageUrl, {waitUntil: 'networkidle0'});
      const digest = await page.evaluate(async () => window.__liuguangTest.digestAnswers({
        '1.1': {status: 'answered', text: 'SYNTHETIC-ONLY', parkUntil: ''},
      }));
      check('digest-vector', digest === expectedDigest, 'mismatch');
      const layout = await snapshot(page);
      check('viewport-mobile', layout.innerW >= 375 && layout.innerW <= 430 && layout.innerH >= 700, layout.innerW + 'x' + layout.innerH);
      check('submit-target-size', layout.btnH >= 44 && layout.btnBottom <= layout.innerH + 1, JSON.stringify({h: layout.btnH, bottom: layout.btnBottom, inner: layout.innerH}));
      check('password-font-16', layout.font >= 16, String(layout.font));
      check('no-horizontal-overflow', layout.scrollW <= layout.innerW + 1, String(layout.scrollW));
      check('password-input-type', layout.inputType === 'password');
      await fill(page, '1.1', 'SYNTHETIC-ONLY-ALPHA');
      await page.click('#btn-submit');
      await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'ok', {timeout: 10000});
      const snap = await snapshot(page);
      check('success-status', snap.state === 'ok' && snap.text.includes('已写入私密收据') && snap.text.includes('没有清空'));
      check('draft-retained', snap.draftHasAlpha && snap.draftHasKeep);
      check('secret-not-in-draft', !snap.passwordInDraft && !snap.tokenInDraft);
      check('session-not-cookie', snap.hasSession && snap.cookie === '' && seen.authorization && !seen.cookieHeader && !seen.setCookie);
      check('page-errors-success', pageErrors.length === 0, pageErrors.join('|'));
      await page.close();
    }

    {
      const {page, pageErrors} = await newPage(browser, device);
      let arm = 'delay';
      page.on('request', async (req) => {
        try {
          const hit = req.method() === 'POST' && req.url().includes('/v1/submissions');
          if (hit && arm === 'delay') {
            arm = 'pass';
            const headers = req.headers();
            const upstream = await fetch(req.url(), {
              method: 'POST',
              headers: {
                'content-type': 'application/json',
                'authorization': headers.authorization || '',
                'idempotency-key': headers['idempotency-key'] || '',
                'origin': headers.origin || origin,
              },
              body: req.postData(),
            });
            const text = await upstream.text();
            await new Promise((resolve) => setTimeout(resolve, 1200));
            try {
              await req.respond({
                status: upstream.status,
                headers: {'access-control-allow-origin': origin, 'content-type': 'application/json'},
                body: text,
              });
            } catch (e) { /* client already aborted */ }
            return;
          }
          await req.continue();
        } catch (e) { /* navigation raced the interceptor */ }
      });
      await page.setRequestInterception(true);
      await seed(page);
      await page.goto(timeoutUrl, {waitUntil: 'networkidle0'});
      await fill(page, '1.2', 'SYNTHETIC-ONLY-TIMEOUT');
      await page.click('#btn-submit');
      await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'fail', {timeout: 8000});
      const failed = await snapshot(page);
      check('timeout-shows-failure', failed.state === 'fail' && failed.text.includes('草稿仍在本机'));
      check('timeout-keeps-draft-and-key', failed.draftHasTimeout && failed.draftHasKeep && failed.pending.includes('idempotencyKey'));
      const pendingDuring = failed.pending;
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await page.click('#btn-submit');
      await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'ok', {timeout: 10000});
      const retried = await snapshot(page);
      check('retry-same-key-succeeds', retried.state === 'ok' && retried.text.includes('同一次重试'));
      check('retry-keeps-draft', retried.draftHasTimeout && retried.draftHasKeep && !retried.passwordInDraft);
      check('pending-cleared-after-ack', retried.pending === '' && pendingDuring !== '');
      check('page-errors-retry', pageErrors.length === 0, pageErrors.join('|'));
      await page.close();
    }

    {
      const {page} = await newPage(browser, device);
      let armed = true;
      page.on('request', async (req) => {
        try {
          if (armed && req.method() === 'POST' && req.url().includes('/v1/submissions')) {
            armed = false;
            await req.respond({
              status: 200,
              headers: {'access-control-allow-origin': origin, 'content-type': 'application/json'},
              body: JSON.stringify({ok: true}),
            });
            return;
          }
          await req.continue();
        } catch (e) {}
      });
      await page.setRequestInterception(true);
      await seed(page);
      await page.goto(pageUrl, {waitUntil: 'networkidle0'});
      await fill(page, '1.3', 'SYNTHETIC-ONLY-BADACK');
      await page.click('#btn-submit');
      await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'fail', {timeout: 8000});
      const snap = await snapshot(page);
      check('invalid-ack-not-success', snap.state === 'fail' && !snap.text.includes('已写入私密收据'));
      check('invalid-ack-keeps-draft', snap.draftHasKeep && snap.text.includes('没有清空'));
      await page.close();
    }

    {
      const {page} = await newPage(browser, device);
      await seed(page);
      let posts = 0;
      page.on('request', (req) => {
        if (req.method() === 'POST' && req.url().includes('/v1/')) posts += 1;
      });
      await page.goto(plainUrl, {waitUntil: 'networkidle0'});
      await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'unconfigured');
      await fill(page, '1.4', 'SYNTHETIC-ONLY-OFFLINE');
      await page.click('#btn-submit');
      await new Promise((resolve) => setTimeout(resolve, 300));
      const snap = await snapshot(page);
      check('unconfigured-no-post', snap.state === 'unconfigured' && posts === 0 && snap.draftHasKeep);
      await page.close();
    }

    {
      const {page} = await newPage(browser, device);
      await seed(page);
      await page.goto(pageUrl, {waitUntil: 'networkidle0'});
      await fill(page, '1.5', 'SYNTHETIC-ONLY-DENIED');
      await page.evaluate(() => { document.getElementById('submit-password').value = 'not-the-secret'; });
      await page.click('#btn-submit');
      await page.waitForFunction(() => document.getElementById('submit-status').dataset.state === 'fail', {timeout: 8000});
      const snap = await snapshot(page);
      check('bad-password-keeps-draft', snap.state === 'fail' && snap.text.includes('口令未被接受') && snap.draftHasKeep && !snap.hasSession);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  if (failures.length) {
    console.log('FAILED ' + failures.join(','));
    process.exit(1);
  }
  console.log('MOBILE_E2E_OK');
}

main().catch((err) => {
  console.log('FAIL harness ' + String(err && err.message || err).slice(0, 300));
  process.exit(1);
});
