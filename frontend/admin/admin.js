'use strict';
(function () {
  const $ = (s) => document.querySelector(s);
  let CSRF = null;
  const state = { rqOffset: 0, rqLimit: 50, editingId: null };

  // ---------- helpers (all text via textContent -> no XSS) ----------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }
  const fmt = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString());
  const pct = (r) => (r === null || r === undefined ? '—' : (r * 100).toFixed(1) + '%');
  const ts = (t) => (t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '—');
  function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 3200); }

  async function api(path, opts = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (opts.method && opts.method !== 'GET' && CSRF) headers['X-CSRF-Token'] = CSRF;
    const r = await fetch('/admin/api' + path, Object.assign({}, opts, { headers, credentials: 'same-origin', cache: 'no-store' }));
    let body = null; try { body = await r.json(); } catch (e) { }
    if (r.status === 401 && path !== '/login') { showLogin(); throw new Error('请先登录'); }
    if (!r.ok) {
      const msg = (body && (body.detail || body.error)) || ('HTTP ' + r.status);
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)); err.status = r.status; err.body = body; throw err;
    }
    return body;
  }

  function table(el, cols, rows, empty = '暂无数据') {
    el.replaceChildren();
    el.append(h('thead', {}, h('tr', {}, cols.map((c) => h('th', { class: c.num ? 'num' : '' }, c.t)))));
    const tb = h('tbody');
    if (!rows.length) tb.append(h('tr', {}, h('td', { colspan: cols.length, class: 'muted' }, empty)));
    for (const r of rows) tb.append(h('tr', {}, cols.map((c) => { const v = c.f ? c.f(r) : r[c.k]; return h('td', { class: c.num ? 'num' : '' }, v === undefined || v === null ? '—' : v); })));
    el.append(tb);
  }
  const card = (k, v, s) => h('div', { class: 'card' }, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), s ? h('div', { class: 's' }, s) : null);
  const usageCols = (first) => [first,
    { t: '请求', k: 'requests', num: 1, f: (r) => fmt(r.requests) },
    { t: '错误', k: 'errors', num: 1, f: (r) => fmt(r.errors) },
    { t: '输入 tokens', num: 1, f: (r) => fmt(r.prompt_tokens) },
    { t: '缓存命中', num: 1, f: (r) => fmt(r.cached_tokens) },
    { t: '命中率', num: 1, f: (r) => pct(r.cache_hit_rate) },
    { t: '输出 tokens', num: 1, f: (r) => fmt(r.completion_tokens) },
    { t: '平均耗时', num: 1, f: (r) => (r.avg_latency_ms ? fmt(r.avg_latency_ms) + ' ms' : '—') }];

  // ---------- auth ----------
  function showLogin() { $('#app-view').classList.add('hidden'); $('#login-view').classList.remove('hidden'); $('#chat-frame').src = 'about:blank'; }
  function showApp(user) { $('#login-view').classList.add('hidden'); $('#app-view').classList.remove('hidden'); $('#who').textContent = '已登录：' + user; route(); }

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault(); $('#login-err').textContent = '';
    try {
      const r = await api('/login', { method: 'POST', body: JSON.stringify({ username: $('#lu').value, password: $('#lp').value }) });
      CSRF = r.csrf; $('#lp').value = ''; if (location.hash === '#login') location.hash = '#overview'; showApp(r.username);
    } catch (err) {
      $('#lp').value = '';
      $('#login-err').textContent = err.status === 429 ? `失败次数过多，请 ${Math.ceil(((err.body && err.body.retry_after) || 900) / 60)} 分钟后再试` : (err.status === 401 ? '用户名或密码错误' : err.message);
    }
  });
  $('#logout-btn').addEventListener('click', async () => { try { await api('/logout', { method: 'POST' }); } catch (e) { } CSRF = null; showLogin(); });
  $('#theme-btn').addEventListener('click', () => { const d = document.documentElement; const t = d.dataset.theme === 'dark' ? 'light' : 'dark'; d.dataset.theme = t; localStorage.setItem('admin_theme', t); });
  document.documentElement.dataset.theme = localStorage.getItem('admin_theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

  // ---------- routing ----------
  const pages = { overview: loadOverview, accounts: loadAccounts, requests: loadRequestsPage, chat: loadChat };
  function route() {
    let p = (location.hash || '#overview').slice(1); if (!pages[p]) p = 'overview';
    document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === p));
    document.querySelectorAll('.page').forEach((s) => s.classList.toggle('active', s.id === 'page-' + p));
    pages[p]().catch((e) => toast(e.message));
  }
  window.addEventListener('hashchange', () => { if (!$('#app-view').classList.contains('hidden')) route(); });

  // ---------- overview ----------
  async function loadOverview() {
    const d = await api('/overview');
    const a = d.accounts, t = d.last_24h, w = d.last_7d;
    $('#ov-cards').replaceChildren(
      card('账号（启用/总数）', `${a.enabled}/${a.total}`, `池内可用 ${a.active}，冷却 ${a.cooling}`),
      card('24h 请求', fmt(t.requests), `错误 ${fmt(t.errors)}`),
      card('24h 输入 tokens', fmt(t.prompt_tokens), `缓存命中 ${fmt(t.cached_tokens)}`),
      card('24h 缓存命中率', pct(t.cache_hit_rate)),
      card('24h 输出 tokens', fmt(t.completion_tokens)),
      card('7 天请求', fmt(w.requests), `命中率 ${pct(w.cache_hit_rate)}`),
      card('运行时长', Math.floor(d.uptime / 3600) + ' 小时', '模式 ' + (d.mode || '—')));
    const max = Math.max(1, ...d.by_day.map((r) => r.requests));
    table($('#ov-days'), usageCols({ t: '日期', k: 'day' }).concat([{ t: '', f: (r) => h('div', { class: 'bar', style: `width:${Math.round((r.requests / max) * 120)}px` }) }]), d.by_day);
    table($('#ov-errors'), [{ t: '时间', f: (r) => ts(r.ts) }, { t: '模型', k: 'model' }, { t: '状态', k: 'status' }, { t: '错误', f: (r) => r.error_code || '' }, { t: '信息', f: (r) => (r.error_message || '').slice(0, 120) }], d.recent_errors, '无错误 🎉');
  }

  // ---------- accounts ----------
  function creditsText(c) {
    if (!c) return '—';
    const b = c.basicCredits, p = c.premiumCredits; const parts = [];
    if (b) parts.push(`基础 ${fmt(b.userUsage)}/${fmt(b.userLimit)}`);
    if (p && p.totalCreditBalance !== undefined) parts.push(`credits 余额 ${fmt(p.totalCreditBalance)}`);
    if (c.isEligible !== undefined) parts.push(c.isEligible ? '可用' : '不可用');
    return parts.join(' · ') || '已获取';
  }
  async function loadAccounts() {
    const d = await api('/accounts');
    table($('#acc-table'), [
      { t: 'ID', k: 'id' },
      { t: '备注', f: (r) => r.label || '—' },
      { t: '邮箱', f: (r) => r.user_email || '—' },
      { t: 'space_id', f: (r) => (r.space_id || '').slice(0, 8) + '…' },
      { t: 'token', k: 'token_hint' },
      { t: '状态', f: (r) => !r.enabled ? h('span', { class: 'badge' }, '已停用') : r.cooling_seconds > 0 ? h('span', { class: 'badge warn' }, `冷却 ${r.cooling_seconds}s`) : h('span', { class: 'badge ok' }, '启用') },
      { t: '最近测试', f: (r) => r.last_test_at ? h('span', { class: 'badge ' + (r.last_test_ok ? 'ok' : 'bad'), title: ts(r.last_test_at) }, r.last_test_msg || (r.last_test_ok ? 'OK' : '失败')) : '—' },
      { t: '额度', f: (r) => h('span', { title: r.last_credits_at ? '更新于 ' + ts(r.last_credits_at) : '' }, creditsText(r.credits)) },
      { t: '操作', f: (r) => h('div', { class: 'actions' },
          h('button', { class: 'btn small', onclick: () => accAction(r.id, 'test') }, '测试'),
          h('button', { class: 'btn small', onclick: () => accAction(r.id, 'credits') }, '额度'),
          h('button', { class: 'btn small', onclick: () => toggleAcc(r) }, r.enabled ? '停用' : '启用'),
          h('button', { class: 'btn small', onclick: () => openAccDialog(r) }, '编辑'),
          h('button', { class: 'btn small danger', onclick: () => delAcc(r) }, '删除')) },
    ], d.items, '暂无账号');
  }
  async function accAction(id, kind) {
    try {
      const r = await api(`/accounts/${id}/${kind}`, { method: 'POST' });
      toast(kind === 'test' ? (r.ok ? '✅ ' : '❌ ') + r.message : '额度：' + creditsText(r.credits));
    } catch (e) { toast(e.status === 429 ? `操作太频繁，${(e.body && e.body.retry_after) || ''} 秒后再试` : e.message); }
    loadAccounts().catch(() => { });
  }
  async function toggleAcc(r) { try { await api(`/accounts/${r.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !r.enabled }) }); } catch (e) { toast(e.message); } loadAccounts(); }
  async function delAcc(r) { if (!confirm(`确定删除账号 #${r.id} ${r.label || r.user_email || ''}？`)) return; try { await api(`/accounts/${r.id}`, { method: 'DELETE' }); toast('已删除'); } catch (e) { toast(e.message); } loadAccounts(); }
  function openAccDialog(r) {
    const f = $('#acc-form'); f.reset(); $('#acc-err').textContent = ''; state.editingId = r ? r.id : null;
    $('#acc-dialog-title').textContent = r ? `编辑账号 #${r.id}` : '添加账号';
    f.querySelectorAll('.new-only').forEach((el) => el.classList.toggle('hidden', !!r));
    f.token_v2.placeholder = r ? `留空则不修改（当前 ${r.token_hint}）` : '';
    if (r) { f.label.value = r.label || ''; f.user_email.value = r.user_email || ''; }
    $('#acc-dialog').showModal();
  }
  $('#add-acc-btn').addEventListener('click', () => openAccDialog(null));
  $('#acc-save').addEventListener('click', async (e) => {
    e.preventDefault(); const f = $('#acc-form'); $('#acc-err').textContent = '';
    try {
      if (state.editingId) {
        const body = { label: f.label.value, user_email: f.user_email.value }; if (f.token_v2.value.trim()) body.token_v2 = f.token_v2.value.trim();
        await api(`/accounts/${state.editingId}`, { method: 'PATCH', body: JSON.stringify(body) });
      } else {
        await api('/accounts', { method: 'POST', body: JSON.stringify({ label: f.label.value, token_v2: f.token_v2.value.trim(), space_id: f.space_id.value.trim(), user_id: f.user_id.value.trim(), user_email: f.user_email.value.trim() }) });
      }
      f.token_v2.value = ''; $('#acc-dialog').close(); toast('已保存，账号池已热更新'); loadAccounts();
    } catch (err) { $('#acc-err').textContent = err.message; }
  });

  // ---------- requests & stats ----------
  async function loadRequestsPage() { await Promise.all([loadStats(), loadRequests()]); }
  async function loadStats() {
    const d = await api('/stats?days=' + $('#st-days').value); const t = d.totals;
    $('#st-cards').replaceChildren(card('请求', fmt(t.requests), `错误 ${fmt(t.errors)}`), card('输入 tokens', fmt(t.prompt_tokens)), card('缓存命中 tokens', fmt(t.cached_tokens)), card('缓存命中率', pct(t.cache_hit_rate)), card('输出 tokens', fmt(t.completion_tokens)));
    table($('#st-model'), usageCols({ t: '模型', k: 'model' }), d.by_model);
    table($('#st-account'), usageCols({ t: '账号', k: 'account' }), d.by_account);
    table($('#st-day'), usageCols({ t: '日期', k: 'day' }), d.by_day);
  }
  async function loadRequests() {
    const q = new URLSearchParams({ limit: state.rqLimit, offset: state.rqOffset, status: $('#rq-status').value, model: $('#rq-model').value.trim() });
    const d = await api('/requests?' + q);
    table($('#rq-table'), [
      { t: '时间', f: (r) => ts(r.ts) }, { t: '模型', k: 'model' }, { t: '流式', f: (r) => (r.stream ? '是' : '否') },
      { t: '状态', f: (r) => h('span', { class: 'badge ' + (r.status >= 400 || r.error_code ? 'bad' : 'ok') }, String(r.status || '—')) },
      { t: '账号', k: 'account' }, { t: '鉴权', k: 'auth' }, { t: 'IP', k: 'ip' },
      { t: '输入', num: 1, f: (r) => fmt(r.prompt_tokens) }, { t: '缓存', num: 1, f: (r) => fmt(r.cached_tokens) }, { t: '输出', num: 1, f: (r) => fmt(r.completion_tokens) },
      { t: '首字', num: 1, f: (r) => (r.ttfb_ms !== null && r.ttfb_ms !== undefined ? fmt(r.ttfb_ms) + ' ms' : '—') },
      { t: '耗时', num: 1, f: (r) => fmt(r.latency_ms) + ' ms' },
      { t: '错误', f: (r) => h('span', { title: r.error_message || '' }, r.error_code || '') }], d.items, '暂无请求');
    $('#rq-info').textContent = `${d.total ? state.rqOffset + 1 : 0}-${Math.min(state.rqOffset + state.rqLimit, d.total)} / ${d.total}`;
    $('#rq-prev').disabled = state.rqOffset <= 0; $('#rq-next').disabled = state.rqOffset + state.rqLimit >= d.total;
  }
  $('#st-days').addEventListener('change', () => loadStats().catch((e) => toast(e.message)));
  $('#rq-refresh').addEventListener('click', () => { state.rqOffset = 0; loadRequestsPage().catch((e) => toast(e.message)); });
  $('#rq-status').addEventListener('change', () => { state.rqOffset = 0; loadRequests().catch((e) => toast(e.message)); });
  $('#rq-prev').addEventListener('click', () => { state.rqOffset = Math.max(0, state.rqOffset - state.rqLimit); loadRequests(); });
  $('#rq-next').addEventListener('click', () => { state.rqOffset += state.rqLimit; loadRequests(); });

  // ---------- chat ----------
  async function loadChat() { const f = $('#chat-frame'); if (!f.src.endsWith('/chat/')) f.src = '/chat/'; }

  // ---------- boot ----------
  api('/session').then((s) => { if (s.authenticated) { CSRF = s.csrf; showApp(s.username); } else showLogin(); }).catch(() => showLogin());
})();
