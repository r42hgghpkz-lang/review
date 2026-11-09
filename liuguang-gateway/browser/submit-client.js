/* Injected into the existing /review/liuguang/ page. No credential, token, or gateway host is stored here.
   Cross-site cookies are not used: Mobile Safari drops them. A bearer token from POST /v1/session
   stays in sessionStorage and is sent on Authorization. The checklist draft key is never cleared on submit. */
const PENDING_KEY = 'liuguang-checklist-20261109-v1-pending-submit';
const SESSION_KEY = 'liuguang-submit-session';
let GATEWAY = '';
let inflight = false;

function loopbackHost(){
  const h = location.hostname;
  return h === 'localhost' || h === '127.0.0.1' || h === '::1';
}
function pageIsSecure(){
  return window.isSecureContext === true;
}
function requestTimeoutMs(){
  if(!loopbackHost()) return 20000;
  const raw = new URLSearchParams(location.search).get('submitTimeoutMs');
  const n = Number(raw);
  if(!raw || !Number.isFinite(n)) return 20000;
  return Math.max(200, Math.min(20000, Math.floor(n)));
}
function abortAfter(ms){
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  return {signal: ctrl.signal, cancel(){ clearTimeout(timer); }};
}
function setStatus(state, msg){
  const el = document.getElementById('submit-status');
  el.dataset.state = state;
  el.textContent = msg;
}
function canonicalJSON(value){
  if(value === null || typeof value !== 'object') return JSON.stringify(value);
  if(Array.isArray(value)) return '[' + value.map(canonicalJSON).join(',') + ']';
  const keys = Object.keys(value).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + canonicalJSON(value[k])).join(',') + '}';
}
async function digestAnswers(answers){
  const buf = new TextEncoder().encode(canonicalJSON(answers));
  const hash = await crypto.subtle.digest('SHA-256', buf);
  const hex = [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, '0')).join('');
  return 'sha256:' + hex;
}
function collectAnswers(){
  const answers = {};
  SECTIONS.forEach(sec => sec.items.forEach(it => {
    const a = getAns(it.id);
    if(a.status !== 'open' || (a.text || '').trim() || a.parkUntil){
      answers[it.id] = {status: a.status, text: a.text || '', parkUntil: a.parkUntil || ''};
    }
  }));
  return answers;
}
function flushInputs(){
  document.querySelectorAll('.card').forEach(card => {
    const id = card.dataset.qid;
    if(!id) return;
    const ta = card.querySelector('textarea');
    const date = card.querySelector('input[type=date]');
    const text = ta ? ta.value : '';
    const parkUntil = date ? date.value : '';
    const cur = getAns(id);
    const patch = {};
    if((text || '') !== (cur.text || '')) patch.text = text;
    if((parkUntil || '') !== (cur.parkUntil || '')) patch.parkUntil = parkUntil;
    if(text.trim() && patch.text !== undefined && cur.status === 'open') patch.status = 'answered';
    if(Object.keys(patch).length) setAns(id, patch);
  });
}
function newIdempotencyKey(){
  if(crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map(x => x.toString(16).padStart(2, '0')).join('');
  return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
}
function pendingKey(){
  try{
    const raw = localStorage.getItem(PENDING_KEY);
    if(raw){
      const o = JSON.parse(raw);
      if(o && typeof o.idempotencyKey === 'string' && /^[A-Za-z0-9_-]{16,80}$/.test(o.idempotencyKey)){
        return o.idempotencyKey;
      }
    }
  }catch(e){}
  const idempotencyKey = newIdempotencyKey();
  localStorage.setItem(PENDING_KEY, JSON.stringify({idempotencyKey, startedAt: new Date().toISOString()}));
  return idempotencyKey;
}
function clearPending(){
  localStorage.removeItem(PENDING_KEY);
}
function ackOk(ack, key){
  if(!ack || ack.ok !== true || ack.persisted !== true) return false;
  if(ack.idempotencyKey !== key) return false;
  if(typeof ack.receiptId !== 'string' || !/^lg-[A-Za-z0-9-]{8,80}$/.test(ack.receiptId)) return false;
  if(typeof ack.digest !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(ack.digest)) return false;
  if(typeof ack.storedAt !== 'string' || !ack.storedAt) return false;
  return true;
}
function readSession(gateway){
  try{
    const o = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null');
    if(!o || o.gateway !== gateway || typeof o.token !== 'string' || o.token.length < 20) return '';
    if(Date.parse(o.expiresAt) <= Date.now() + 5000) return '';
    return o.token;
  }catch(e){ return ''; }
}
function loopbackGatewayOverride(raw){
  if(!loopbackHost() || !raw) return '';
  let u;
  try{ u = new URL(raw); }catch(e){ return ''; }
  if(u.username || u.password || u.search || u.hash) return '';
  if(u.pathname.replace(/\/+$/, '') !== '') return '';
  const host = u.hostname;
  if(u.protocol === 'http:' && (host === '127.0.0.1' || host === 'localhost')) return u.origin;
  // Ephemeral trial route only. A published github.io page ignores ?gw= entirely.
  if(u.protocol === 'https:' && host.endsWith('.trycloudflare.com') && host.split('.').length === 3) return u.origin;
  return '';
}
function publicGateway(raw){
  if(!raw || typeof raw !== 'string') return '';
  let u;
  try{ u = new URL(raw.trim()); }catch(e){ return ''; }
  if(u.protocol !== 'https:' || u.username || u.password || u.search || u.hash) return '';
  if(u.pathname.replace(/\/+$/, '') !== '') return '';
  return u.origin;
}
async function resolveGateway(){
  const over = loopbackGatewayOverride(new URLSearchParams(location.search).get('gw') || '');
  if(over) return over;
  try{
    const r = await fetch('gateway.json', {cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer'});
    if(!r.ok) return '';
    const j = await r.json();
    return publicGateway(j && j.gateway);
  }catch(e){
    return '';
  }
}
function setGatewayStatus(){
  if(!GATEWAY){
    setStatus('unconfigured', '提交网关还没配置。草稿只在这台设备上，没有发送。');
  }else{
    setStatus('idle', '填写提交口令后点提交。口令只发到网关，不写入页面和草稿。');
  }
}
async function ensureSession(gateway, password){
  const existing = readSession(gateway);
  if(existing) return existing;
  const ctrl = abortAfter(20000);
  let res;
  try{
    res = await fetch(gateway + '/v1/session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password}),
      signal: ctrl.signal,
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-store'
    });
  }finally{
    ctrl.cancel();
  }
  let ack = null;
  try{ ack = await res.json(); }catch(e){ ack = null; }
  if(res.status === 401 || res.status === 429){
    const err = new Error('unauthorized');
    err.code = 'unauthorized';
    throw err;
  }
  if(!res.ok || !ack || ack.ok !== true || typeof ack.token !== 'string' || ack.token.length < 20){
    const err = new Error('bad-session');
    err.code = 'bad-session';
    throw err;
  }
  sessionStorage.setItem(SESSION_KEY, JSON.stringify({gateway, token: ack.token, expiresAt: ack.expiresAt}));
  return ack.token;
}
async function postSubmission(gateway, token, key, answers){
  const ctrl = abortAfter(requestTimeoutMs());
  const body = {v: 1, kind: 'liuguang-checklist', storageKey: STORAGE_KEY, answers};
  if(loopbackHost() && new URLSearchParams(location.search).get('synthetic') === '1') body.synthetic = true;
  let res;
  try{
    res = await fetch(gateway + '/v1/submissions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
        'Idempotency-Key': key
      },
      body: JSON.stringify(body),
      signal: ctrl.signal,
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-store'
    });
  }finally{
    ctrl.cancel();
  }
  let ack = null;
  try{ ack = await res.json(); }catch(e){ ack = null; }
  return {status: res.status, ack};
}
function restoreDraftIfMutated(snapshot){
  if(localStorage.getItem(STORAGE_KEY) === snapshot) return;
  if(snapshot === null) localStorage.removeItem(STORAGE_KEY);
  else localStorage.setItem(STORAGE_KEY, snapshot);
  try{
    state.answers = snapshot ? (JSON.parse(snapshot).answers || {}) : {};
  }catch(e){
    state.answers = {};
  }
  render();
}
async function submitChecklist(){
  if(inflight) return;
  flushInputs();
  const draftSnapshot = localStorage.getItem(STORAGE_KEY);
  const answers = collectAnswers();
  if(!pageIsSecure()){
    setStatus('fail', '这个页面不是安全来源，已停止提交。草稿仍在本机。');
    return;
  }
  if(!GATEWAY){
    setStatus('unconfigured', '提交网关还没配置。草稿只在这台设备上，没有发送。');
    return;
  }
  if(Object.keys(answers).length === 0){
    setStatus('fail', '没有可提交的答案。草稿没有被清空。');
    return;
  }
  const password = document.getElementById('submit-password').value;
  if(!password){
    setStatus('fail', '请先填写提交口令。草稿没有发送，也没有清空。');
    return;
  }
  inflight = true;
  document.getElementById('btn-submit').disabled = true;
  setStatus('idle', '正在提交…');
  try{
    let token;
    try{
      token = await ensureSession(GATEWAY, password);
    }catch(e){
      setStatus('fail', e && e.code === 'unauthorized'
        ? '口令未被接受。草稿仍在本机，没有清空。'
        : '提交失败（网络或超时）。草稿仍在本机，没有清空。');
      return;
    }
    const key = pendingKey();
    let result;
    try{
      result = await postSubmission(GATEWAY, token, key, answers);
    }catch(e){
      setStatus('fail', '提交失败（网络或超时）。草稿仍在本机，没有清空。请再点一次，会沿用同一次提交。');
      return;
    }
    if(result.status === 401){
      sessionStorage.removeItem(SESSION_KEY);
      try{
        token = await ensureSession(GATEWAY, password);
        result = await postSubmission(GATEWAY, token, key, answers);
      }catch(e){
        setStatus('fail', '提交失败（网络或超时）。草稿仍在本机，没有清空。');
        return;
      }
    }
    if(!ackOk(result.ack, key)){
      setStatus('fail', result.status === 401
        ? '口令未被接受。草稿仍在本机，没有清空。'
        : '提交没有得到有效回执，草稿仍在本机，没有清空。');
      return;
    }
    const localDigest = await digestAnswers(answers);
    clearPending();
    if(localDigest === result.ack.digest){
      document.getElementById('submit-password').value = '';
      const again = result.ack.replay === true ? '（同一次重试，没有另存一份）' : '';
      setStatus('ok', '已写入私密收据 ' + result.ack.receiptId + '。本机草稿保留，没有清空。' + again);
    }else{
      setStatus('fail', '回执 ' + result.ack.receiptId + ' 是早前一次的内容，和当前草稿不一致。当前草稿仍在，没有被覆盖。');
    }
  }finally{
    restoreDraftIfMutated(draftSnapshot);
    inflight = false;
    const btn = document.getElementById('btn-submit');
    if(btn) btn.disabled = false;
  }
}
if(loopbackHost()){
  window.__liuguangTest = {canonicalJSON, digestAnswers, collectAnswers, STORAGE_KEY, PENDING_KEY, ackOk};
}
document.getElementById('btn-submit').addEventListener('click', () => { submitChecklist(); });
document.getElementById('submit-password').addEventListener('keydown', (e) => {
  if(e.key === 'Enter'){ e.preventDefault(); submitChecklist(); }
});
resolveGateway().then(g => { GATEWAY = g; setGatewayStatus(); });
