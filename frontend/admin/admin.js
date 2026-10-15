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
    if (!rows.length) {
      const title = typeof empty === 'string' ? empty : '暂无数据';
      const cell = h('td', { colspan: cols.length, class: 'empty-cell' },
        h('div', { class: 'empty-title' }, title),
        h('div', { class: 'empty-hint' }, '数据就绪后会显示在这里'));
      tb.append(h('tr', {}, cell));
    }
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
  function syncThemeLabel() {
    const dark = document.documentElement.dataset.theme === 'dark';
    $('#theme-btn').textContent = dark ? '☀️ 浅色' : '🌙 深色';
  }
  $('#theme-btn').addEventListener('click', () => {
    const d = document.documentElement;
    const t = d.dataset.theme === 'dark' ? 'light' : 'dark';
    d.dataset.theme = t; localStorage.setItem('admin_theme', t); syncThemeLabel();
  });
  document.documentElement.dataset.theme = localStorage.getItem('admin_theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  syncThemeLabel();

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
    const rl = c.rateLimit;
    if (rl) {
      const t = (x) => x ? new Date(x * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '?';
      if (rl.rolling_pct != null) parts.push(`滚动(${rl.window || '6h'}) ${fmt(rl.rolling_pct)}% 重置 ${t(rl.status === 'rate_limited' && rl.limited_by !== 'billing_period' ? rl.resume_at : rl.rolling_reset_at)}`);
      if (rl.monthly_pct != null) parts.push(`月度 ${fmt(rl.monthly_pct)}% 重置 ${t(rl.monthly_reset_at)}`);
      if (rl.status === 'rate_limited') parts.push(`⛔ 已达上限(${rl.limited_by || ''})，恢复 ${t(rl.resume_at)}`);
    }
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
      { t: '状态', f: (r) => !r.enabled ? h('span', { class: 'badge' }, '已停用') : (r.pool_status && r.pool_status.state === 'exhausted') ? h('span', { class: 'badge bad', title: (r.pool_status.exhausted_reason || '') + ' / 复查 ' + ts(r.pool_status.exhausted_until) }, '额度耗尽') : r.cooling_seconds > 0 ? h('span', { class: 'badge warn' }, `冷却 ${r.cooling_seconds}s`) : h('span', { class: 'badge ok' }, '启用') },
      { t: '优先级', f: (r) => r.pool_status ? `${r.pool_status.priority}${r.pool_status.is_primary ? ' (当前)' : ''}` : '—' },
      { t: '最近测试', f: (r) => r.last_test_at ? h('span', { class: 'badge ' + (r.last_test_ok ? 'ok' : 'bad'), title: ts(r.last_test_at) }, r.last_test_msg || (r.last_test_ok ? 'OK' : '失败')) : '—' },
      { t: '额度', f: (r) => h('span', { title: r.last_credits_at ? '更新于 ' + ts(r.last_credits_at) : '' }, creditsText(r.credits)) },
      { t: '操作', f: (r) => h('div', { class: 'actions' },
          h('div', { class: 'actions-group' },
            h('button', { class: 'btn small', onclick: () => accAction(r.id, 'test') }, '测试'),
            h('button', { class: 'btn small', onclick: () => accAction(r.id, 'credits') }, '额度'),
            r.pool_status ? h('button', { class: 'btn small', onclick: () => setExhausted(r, r.pool_status.state !== 'exhausted') }, r.pool_status.state === 'exhausted' ? '恢复' : '标记耗尽') : null,
            h('button', { class: 'btn small', onclick: () => toggleAcc(r) }, r.enabled ? '停用' : '启用')),
          h('div', { class: 'actions-group' },
            h('button', { class: 'btn small', onclick: () => openLogin(r.user_email || '') }, '重新登录'),
            h('button', { class: 'btn small', onclick: () => openAccDialog(r) }, '编辑'),
            h('button', { class: 'btn small danger', onclick: () => delAcc(r) }, '删除'))) },
    ], d.items, '暂无账号');
    loadQuota().catch((e) => toast(e.message));
  }
  async function accAction(id, kind) {
    try {
      const r = await api(`/accounts/${id}/${kind}`, { method: 'POST' });
      toast(kind === 'test' ? (r.ok ? '✅ ' : '❌ ') + r.message : '额度：' + creditsText(r.credits));
    } catch (e) { toast(e.status === 429 ? `操作太频繁，${(e.body && e.body.retry_after) || ''} 秒后再试` : e.message); }
    loadAccounts().catch(() => { });
  }
  async function setExhausted(r, ex) { try { await api(`/accounts/${r.id}/exhausted`, { method: 'POST', body: JSON.stringify({ exhausted: ex }) }); } catch (e) { toast(e.message); } loadAccounts(); }
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

  // ---------- quota estimate ----------
  const usd = (v) => (v === null || v === undefined ? '—' : '$' + Number(v).toFixed(2));
  const shortT = (x) => x ? new Date(x * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
  function qbar(p, reset) {
    if (p === null || p === undefined) return '—';
    const cls = p >= 90 ? 'bad' : p >= 70 ? 'warn' : '';
    return h('span', { class: 'qcell', title: '重置 ' + shortT(reset) }, h('span', { class: 'qbar ' + cls }, h('i', { style: `width:${Math.min(100, Math.max(0, p))}%` })), `${Number(p).toFixed(1)}%`);
  }
  const SRC = { account: '本账号实测', pool_measured: '池中位数', pool_configured: '配置值', unknown: '未知' };
  function renderQuota(d) {
    const p = d.pool;
    $('#q-cards').replaceChildren(
      card('池月度剩余（估）', usd(p.month_remaining), `共 ${p.known}/${p.accounts} 个账号可估`),
      card('池月度容量（估）', usd(p.month_capacity), p.month_pct != null ? `已用 ${p.month_pct}%` : ''),
      card('池 6h 剩余（估）', usd(p.rolling_remaining)),
      card('单号月容量中位数', usd(p.month_capacity_median), '6h 中位数 ' + usd(p.rolling_capacity_median)));
    table($('#q-table'), [
      { t: 'ID', k: 'id' }, { t: '账号', f: (r) => r.label || '—' },
      { t: '状态', f: (r) => r.status === 'rate_limited' ? h('span', { class: 'badge bad' }, '限流 ' + (r.limited_by || '')) : r.status ? h('span', { class: 'badge ok' }, '正常') : '未查询' },
      { t: '6h 滚动', f: (r) => qbar(r.rolling_pct, r.rolling_reset_at) },
      { t: '6h 剩余$', num: 1, f: (r) => usd(r.rolling_remaining_usd) },
      { t: '月度', f: (r) => qbar(r.monthly_pct, r.monthly_reset_at) },
      { t: '本期已记录$', num: 1, f: (r) => usd(r.month_cost) },
      { t: '月容量$', num: 1, f: (r) => h('span', { title: '依据：' + (SRC[r.month_capacity_source] || r.month_capacity_source) }, usd(r.month_capacity_usd)) },
      { t: '月剩余$', num: 1, f: (r) => usd(r.month_remaining_usd) },
      { t: '月度重置', f: (r) => shortT(r.monthly_reset_at) },
      { t: '查询于', f: (r) => shortT(r.checked_at) },
    ], d.items, '暂无启用账号');
    const rt = d.rates;
    $('#q-note').textContent = `估算口径：按 Opus 官方价 输入 $${rt.input}/M、缓存读 $${rt.cache_read}/M、输出 $${rt.output}/M 计算本网关日志里的 token 花费；容量 = 窗口内花费 ÷ 已用%（日志需覆盖整个窗口且已用≥5%，否则取池中位数）。Notion 按 credits 计量，仅供参考。`;
  }
  async function loadQuota() { renderQuota(await api('/quota')); }
  $('#q-refresh').addEventListener('click', async () => {
    try { const d = await api('/quota/refresh', { method: 'POST' }); renderQuota(d); toast(`已刷新 ${d.refreshed} 个账号`); loadAccounts().catch(() => { }); }
    catch (e) { toast(e.status === 429 ? `操作太频繁，${(e.body && e.body.retry_after) || ''} 秒后再试` : e.message); }
  });

  // ---------- Notion e-mail login (server-side capture) ----------
  const nl = { step: 1, flow: null };
  function nlShow(step) {
    nl.step = step;
    ['1', '2', '3'].forEach((n) => $('#nl-step' + n).classList.toggle('hidden', String(step) !== n));
    $('#nl-next').textContent = step === 1 ? '发送验证码' : step === 2 ? '验证并登录' : '保存账号';
    $('#nl-next').disabled = false;
  }
  function openLogin(email) {
    const f = $('#nl-form'); f.reset(); $('#nl-err').textContent = ''; nl.flow = null;
    if (email) f.email.value = email;
    nlShow(1); $('#nl-dialog').showModal();
  }
  async function nlCommit(spaceId) {
    const f = $('#nl-form');
    const r = await api('/notion-login/commit', { method: 'POST', body: JSON.stringify({ flow_id: nl.flow, space_id: spaceId || null, label: f.label.value.trim() || null }) });
    nl.flow = null; $('#nl-dialog').close();
    toast(r.action === 'updated' ? `账号 #${r.id} token 已刷新` : `已添加账号 #${r.id}，账号池已热更新`);
    loadAccounts().catch(() => { });
  }
  $('#nl-btn').addEventListener('click', () => openLogin(''));
  $('#nl-next').addEventListener('click', async (e) => {
    e.preventDefault(); const f = $('#nl-form'); $('#nl-err').textContent = ''; $('#nl-next').disabled = true;
    try {
      if (nl.step === 1) {
        const r = await api('/notion-login/start', { method: 'POST', body: JSON.stringify({ email: f.email.value.trim() }) });
        nl.flow = r.flow_id; $('#nl-email').textContent = r.email; nlShow(2); f.code.focus();
      } else if (nl.step === 2) {
        const r = await api('/notion-login/verify', { method: 'POST', body: JSON.stringify({ flow_id: nl.flow, code: f.code.value }) });
        f.code.value = '';
        if (r.candidates.length === 1) { await nlCommit(r.candidates[0].space_id); return; }
        f.space.replaceChildren(...r.candidates.map((c) => h('option', { value: c.space_id }, `${c.space_name || c.space_id} (${c.plan || '?'}) — ${c.user_email}`)));
        nlShow(3);
      } else {
        await nlCommit(f.space.value);
      }
    } catch (err) { $('#nl-err').textContent = err.message; }
    finally { $('#nl-next').disabled = false; }
  });
  $('#nl-cancel').addEventListener('click', () => { if (nl.flow) api('/notion-login/cancel', { method: 'POST', body: JSON.stringify({ flow_id: nl.flow, code: '-' }) }).catch(() => { }); nl.flow = null; });

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
