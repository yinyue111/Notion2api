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
  const pages = { overview: loadOverview, accounts: loadAccounts, keys: loadKeys, requests: loadRequestsPage, chat: loadChat };
  function route() {
    let p = (location.hash || '#overview').slice(1).split('?')[0]; if (!pages[p]) p = 'overview';
    document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === p));
    document.querySelectorAll('.page').forEach((s) => s.classList.toggle('active', s.id === 'page-' + p));
    pages[p]().catch((e) => toast(e.message));
  }
  window.addEventListener('hashchange', () => { if (!$('#app-view').classList.contains('hidden')) route(); });

  // ---------- overview ----------
  const TZ8 = 8 * 3600;
  const fmt8 = (t) => (t ? new Date((t + TZ8) * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—');
  const toLocal8 = (t) => new Date((t + TZ8) * 1000).toISOString().slice(0, 16);
  const fromLocal8 = (v) => { const ms = Date.parse(v + ':00Z'); return Number.isFinite(ms) ? ms / 1000 - TZ8 : null; };
  const usdv = (v) => { if (v === null || v === undefined) return '—'; const n = Number(v); if (n > 0 && n < 0.01) return '$' + n.toFixed(4); return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
  const PRESET_LABEL = { today: '今天', yesterday: '昨天', '7d': '近7天', '30d': '近30天', month: '本月', all: '全部', custom: '自定义' };
  function ovGetRange() {
    const q = new URLSearchParams((location.hash.split('?')[1]) || '');
    if (q.get('s') || q.get('e')) return { start: Number(q.get('s')) || 0, end: q.get('e') ? Number(q.get('e')) : null };
    if (q.get('r')) return { r: q.get('r') };
    try { const v = JSON.parse(localStorage.getItem('ov_range') || 'null'); if (v && (v.r || v.start !== undefined)) return v; } catch (e) { }
    return { r: 'today' };
  }
  function ovSetRange(v) {
    localStorage.setItem('ov_range', JSON.stringify(v));
    const q = v.r ? 'r=' + encodeURIComponent(v.r) : 's=' + Math.floor(v.start || 0) + (v.end ? '&e=' + Math.floor(v.end) : '');
    const target = '#overview?' + q;
    if (location.hash !== target) location.hash = target; else loadOverview().catch((e) => toast(e.message));
  }
  const tokSub = (r) => h('span', { class: 'sub' }, `未缓存 ${fmt(r.uncached_tokens)} · 读 ${fmt(r.cached_tokens)} · 写 ${fmt(r.cache_write_tokens)}`);
  const valueCell = (r) => h('span', { title: `输入 ${usdv(r.input_usd)} · 缓存读 ${usdv(r.cache_read_usd)} · 缓存写 ${usdv(r.cache_write_usd)} · 输出 ${usdv(r.output_usd)}` + (r.unpriced_requests ? `\n无官方价 ${r.unpriced_requests} 次 / ${fmt(r.unpriced_tokens)} tokens 未计入` : '') },
    h('span', { class: 'usd' }, r.priced_requests || !r.unpriced_requests ? usdv(r.value_usd) : '无官方价'),
    r.unpriced_requests && r.priced_requests ? h('span', { class: 'sub' }, `另 ${fmt(r.unpriced_tokens)} tokens 无价`) : null);
  const valueCols = (first) => [first,
    { t: '请求', num: 1, f: (r) => fmt(r.requests) },
    { t: '错误', num: 1, f: (r) => fmt(r.errors) },
    { t: '输入 tokens', num: 1, f: (r) => h('span', {}, fmt(r.prompt_tokens), tokSub(r)) },
    { t: '命中率', num: 1, f: (r) => pct(r.cache_hit_rate) },
    { t: '输出 tokens', num: 1, f: (r) => fmt(r.completion_tokens) },
    { t: '官方价值', num: 1, f: valueCell },
    { t: '平均耗时', num: 1, f: (r) => (r.avg_latency_ms ? fmt(r.avg_latency_ms) + ' ms' : '—') }];
  async function loadOverview() {
    const rg = ovGetRange();
    const q = rg.r ? 'range=' + encodeURIComponent(rg.r) : 'start=' + (rg.start || 0) + (rg.end ? '&end=' + rg.end : '');
    const d = await api('/overview?' + q);
    const R = d.range, a = d.accounts, t = d.totals;
    document.querySelectorAll('#ov-presets button').forEach((b) => b.classList.toggle('on', b.dataset.r === R.preset));
    $('.range-custom').classList.toggle('on', R.preset === 'custom');
    $('#ov-start').value = toLocal8(R.preset === 'all' && d.first_log_ts ? d.first_log_ts : R.start);
    $('#ov-end').value = toLocal8(R.end);
    $('#ov-range-label').textContent = `${PRESET_LABEL[R.preset] || ''}：${fmt8(R.preset === 'all' ? (d.first_log_ts || R.start) : R.start)} → ${fmt8(R.end)}（UTC+8）`;
    $('#ov-cards').replaceChildren(
      card('官方价值（USD）', usdv(t.value_usd), `输入 ${usdv(t.input_usd)} · 缓存读 ${usdv(t.cache_read_usd)} · 缓存写 ${usdv(t.cache_write_usd)} · 输出 ${usdv(t.output_usd)}`),
      card('请求', fmt(t.requests), `错误 ${fmt(t.errors)}${t.requests ? '（' + pct(t.errors / t.requests) + '）' : ''}`),
      card('输入 tokens', fmt(t.prompt_tokens), `未缓存 ${fmt(t.uncached_tokens)} · 缓存读 ${fmt(t.cached_tokens)} · 缓存写 ${fmt(t.cache_write_tokens)}`),
      card('输出 tokens', fmt(t.completion_tokens), `缓存命中率 ${pct(t.cache_hit_rate)}`),
      card('平均耗时', t.avg_latency_ms ? fmt(t.avg_latency_ms) + ' ms' : '—', `有用量的请求 ${fmt(t.usage_requests)}`),
      card('账号（启用/总数）', `${a.enabled}/${a.total}`, `池内可用 ${a.active}，冷却 ${a.cooling}`),
      card('运行时长', Math.floor(d.uptime / 3600) + ' 小时', '模式 ' + (d.mode || '—')));
    $('#ov-cards').firstChild.classList.add('value');
    const notes = [];
    if (t.unpriced_requests) notes.push(`${fmt(t.unpriced_requests)} 次请求（${fmt(t.unpriced_tokens)} tokens）的模型无官方价，未计入总价值。`);
    if (t.long_context_requests) notes.push(`${fmt(t.long_context_requests)} 次请求按长上下文高价计。`);
    notes.push(`价格表 ${d.prices.count} 个模型，更新于 ${d.prices.updated || '—'}${d.prices.error ? '（⚠ 价格文件读取失败，已用内置默认：' + d.prices.error + '）' : ''}。价值 = 官方 API 标价折算，仅供参考，不是 Notion 实际扣费。`);
    $('#ov-value-note').textContent = notes.join(' ');
    $('#ov-time-title').textContent = R.bucket === 'hour' ? '趋势（按小时）' : '趋势（按天）';
    const maxV = Math.max(1e-9, ...d.by_time.map((r) => r.value_usd)); const maxR = Math.max(1, ...d.by_time.map((r) => r.requests));
    table($('#ov-time'), valueCols({ t: R.bucket === 'hour' ? '时间' : '日期', k: 'bucket' }).concat([{ t: '', f: (r) => h('div', {},
      h('div', { class: 'bar', style: `width:${Math.round((r.requests / maxR) * 120)}px`, title: '请求' }),
      h('div', { class: 'bar value', style: `width:${Math.round((r.value_usd / maxV) * 120)}px;margin-top:3px`, title: '官方价值' })) }]), d.by_time);
    table($('#ov-models'), valueCols({ t: '模型', f: (r) => h('span', {}, r.model, r.price_name ? h('span', { class: 'sub' }, r.price_name) : null) }).concat([
      { t: '单价 入/出/读/写', f: (r) => r.priced ? h('span', { class: 'sub', title: r.long_context ? `prompt ≥ ${fmt(r.long_context.min_prompt_tokens)}：${r.long_context.input}/${r.long_context.output}/${r.long_context.cache_read}/${r.long_context.cache_write}` : '' }, `$${r.price.input}/${r.price.output}/${r.price.cache_read}/${r.price.cache_write}` + (r.long_context ? ' · 长上下文分档' : '') + (r.offpeak ? ' · 峰谷价' : '')) : h('span', { class: 'badge warn' }, '无官方价') }]), d.by_model);
    table($('#ov-accounts'), valueCols({ t: '账号', k: 'account' }), d.by_account);
    table($('#ov-keys'), valueCols({ t: 'Key', f: (r) => r.auth === 'api_key' ? '主 Key' : r.auth === 'session' ? '后台会话' : String(r.auth || '?').replace(/^key:/, '') }), d.by_key);
    $('#ov-errcodes').replaceChildren(...d.errors_by_code.map((e) => h('span', { class: 'chip' }, `${e.error_code} × ${e.count}`)));
    table($('#ov-errors'), [{ t: '时间', f: (r) => fmt8(r.ts) }, { t: '模型', k: 'model' }, { t: '状态', k: 'status' }, { t: '账号', k: 'account' }, { t: '错误', f: (r) => r.error_code || '' }, { t: '信息', f: (r) => (r.error_message || '').slice(0, 120) }], d.recent_errors, '该时间段无错误 🎉');
    $('#ov-prices-meta').textContent = `（${d.prices.count} 个模型 · ${d.prices.file} · 更新 ${d.prices.updated || '—'}）`;
    if ($('#ov-prices-box').open) loadPrices().catch((e) => toast(e.message));
  }
  document.querySelectorAll('#ov-presets button').forEach((b) => b.addEventListener('click', () => ovSetRange({ r: b.dataset.r })));
  $('#ov-apply').addEventListener('click', () => {
    const s = fromLocal8($('#ov-start').value), e = fromLocal8($('#ov-end').value);
    if (s === null) { toast('请填写开始时间'); return; }
    if (e !== null && e <= s) { toast('结束时间必须晚于开始时间'); return; }
    ovSetRange({ start: s, end: e });
  });
  $('#ov-refresh').addEventListener('click', () => loadOverview().catch((e) => toast(e.message)));

  // ---------- official price table ----------
  let PR = null;
  const safeHref = (u) => (/^https?:\/\//i.test(u || '') ? u : null);
  async function loadPrices() {
    PR = await api('/prices');
    const rows = Object.entries(PR.models).map(([k, e]) => Object.assign({ key: k, aliases: (PR.aliases[k] || []).filter((x) => x !== k) }, e));
    const lc = (e) => e.long_context ? `prompt ≥ ${fmt(e.long_context.min_prompt_tokens)}：$${e.long_context.input}/${e.long_context.output}/${e.long_context.cache_read}/${e.long_context.cache_write}` : '—';
    table($('#pr-table'), [
      { t: '模型', f: (r) => h('span', {}, r.key, h('span', { class: 'sub' }, r.name + (r.aliases.length ? ' · 别名 ' + r.aliases.join(', ') : ''))) },
      { t: '厂商', k: 'provider' },
      { t: '输入', num: 1, f: (r) => r.unpriced ? '—' : '$' + r.input },
      { t: '输出', num: 1, f: (r) => r.unpriced ? '—' : '$' + r.output },
      { t: '缓存读', num: 1, f: (r) => r.unpriced ? '—' : '$' + r.cache_read },
      { t: '缓存写', num: 1, f: (r) => r.unpriced ? '—' : '$' + r.cache_write },
      { t: '长上下文', f: (r) => r.unpriced ? h('span', { class: 'badge warn' }, '无官方价') : lc(r) },
      { t: '说明', f: (r) => h('span', { class: 'sub' }, (r.offpeak ? `非高峰 ×${r.offpeak.factor}（高峰 UTC ${r.offpeak.peak_utc_hours.join(',')} 点，周${r.offpeak.peak_weekdays.join('')}）；` : '') + (r.note || '')) },
      { t: '来源', f: (r) => { const u = safeHref(r.source); return h('span', {}, u ? h('a', { href: u, target: '_blank', rel: 'noopener noreferrer' }, new URL(u).hostname) : (r.source || '—'), h('span', { class: 'sub' }, r.checked || '')); } }], rows, '价格表为空');
    if (PR.unmatched_models && PR.unmatched_models.length) toast('以下模型未配置价格：' + PR.unmatched_models.join(', '));
  }
  $('#ov-prices-box').addEventListener('toggle', () => { if ($('#ov-prices-box').open) loadPrices().catch((e) => toast(e.message)); });
  $('#pr-edit').addEventListener('click', async () => {
    if (!PR) await loadPrices();
    const { version, currency, unit, updated, models } = PR;
    $('#pr-json').value = JSON.stringify({ version, currency, unit, updated, models }, null, 1);
    $('#pr-err').textContent = ''; $('#pr-dialog').showModal();
  });
  $('#pr-save').addEventListener('click', async (e) => {
    e.preventDefault(); $('#pr-err').textContent = '';
    let body; try { body = JSON.parse($('#pr-json').value); } catch (err) { $('#pr-err').textContent = 'JSON 格式错误：' + err.message; return; }
    try { const r = await api('/prices', { method: 'PUT', body: JSON.stringify(body) }); $('#pr-dialog').close(); toast(`已保存 ${r.count} 个模型价格`); await loadPrices(); loadOverview().catch(() => { }); }
    catch (err) { $('#pr-err').textContent = err.message; }
  });
  $('#pr-reset').addEventListener('click', async () => {
    if (!confirm('恢复为内置的官方价格表（2026-10-18 核对）？当前修改会被覆盖。')) return;
    try { const r = await api('/prices/reset', { method: 'POST' }); toast(`已恢复默认（${r.count} 个模型）`); await loadPrices(); loadOverview().catch(() => { }); } catch (err) { toast(err.message); }
  });

  // ---------- 6h window reset (Asia/Taipei) ----------
  const tpeT = (x) => {
    if (!x) return '—';
    const p = {}; new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Taipei', hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      .formatToParts(new Date(x * 1000)).forEach((q) => { p[q.type] = q.value; });
    return `${p.month}-${p.day} ${p.hour === '24' ? '00' : p.hour}:${p.minute}`;
  };
  const relT = (x) => {
    if (!x) return '';
    let s = Math.round(x - Date.now() / 1000);
    if (s <= 0) return '即将刷新';
    const d = Math.floor(s / 86400); s -= d * 86400; const hh = Math.floor(s / 3600); const mm = Math.floor((s % 3600) / 60);
    return (d ? `${d}d${hh}h` : hh ? `${hh}h${mm}m` : `${Math.max(1, mm)}m`) + ' 后刷新';
  };
  // w: {status, rolling_pct, rolling_reset_at, resume_at, limited, limited_by}
  function resetCell(w) {
    if (!w || !w.status) return '—';
    const limited = w.status === 'rate_limited';
    const roll = limited && w.limited_by !== 'billing_period';
    const nxt = roll && w.resume_at ? w.resume_at : w.rolling_reset_at;
    if (limited) {
      const extra = w.resume_at && w.rolling_reset_at && Math.abs(w.resume_at - w.rolling_reset_at) > 60 ? `（窗口重置 ${tpeT(w.rolling_reset_at)}）` : '';
      const at = roll ? nxt : w.resume_at;
      return h('span', { class: 'badge bad', title: `已达上限(${w.limited_by || ''})，恢复 ${tpeT(w.resume_at)}${extra}，台北时间` },
        at ? `⛔ ${tpeT(at)} · ${relT(at)}` : `⛔ 已限流${w.limited_by === 'billing_period' ? '(月度)' : ''}`);
    }
    if (!w.rolling_reset_at) return h('span', { class: 'muted', title: '当前没有进行中的 6h 窗口（用量 0%）' }, '未开始');
    return h('span', { title: `6h 窗口刷新：${tpeT(w.rolling_reset_at)}（台北时间）` }, `${tpeT(w.rolling_reset_at)} · ${relT(w.rolling_reset_at)}`);
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
      const t = (x) => x ? tpeT(x) : '?';
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
      { t: '工作区', f: (r) => { const ps = r.pool_status || {}; const cr = r.credits || {}; const cap = ps.capability || ((cr.capabilities || null) ? (cr.capabilities.gpt_only ? 'gpt_only' : 'claude') : 'unknown'); const nm = ps.workspace_name || cr.workspace_name || ''; return h('span', { title: (r.space_id || '') + (ps.account_key ? ' / ' + ps.account_key : '') }, `${nm || (r.space_id || '').slice(0, 8) + '…'} `, h('span', { class: 'badge ' + (cap === 'claude' ? 'ok' : cap === 'gpt_only' ? 'warn' : '') }, cap === 'claude' ? 'Claude' : cap === 'gpt_only' ? '仅GPT' : '未知')); } },
      { t: 'token', k: 'token_hint' },
      { t: '状态', f: (r) => !r.enabled ? h('span', { class: 'badge' }, '已停用') : (r.pool_status && r.pool_status.state === 'exhausted') ? h('span', { class: 'badge bad', title: (r.pool_status.exhausted_reason || '') + ' / 复查 ' + ts(r.pool_status.exhausted_until) }, '额度耗尽') : r.cooling_seconds > 0 ? h('span', { class: 'badge warn' }, `冷却 ${r.cooling_seconds}s`) : h('span', { class: 'badge ok' }, '启用') },
      { t: '6h 刷新（台北）', f: (r) => { const w = r.rolling_window || ((r.pool_status || {}).usage) || null; const pct = w && w.rolling_pct != null ? `${Number(w.rolling_pct).toFixed(1)}% · ` : ''; const c = resetCell(w); return typeof c === 'string' ? (pct ? pct + c : c) : h('span', {}, pct, c); } },
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
  const shortT = (x) => tpeT(x);
  function qbar(p, reset) {
    if (p === null || p === undefined) return '—';
    const cls = p >= 90 ? 'bad' : p >= 70 ? 'warn' : '';
    return h('span', { class: 'qcell', title: '重置 ' + shortT(reset) }, h('span', { class: 'qbar ' + cls }, h('i', { style: `width:${Math.min(100, Math.max(0, p))}%` })), `${Number(p).toFixed(1)}%`);
  }
  const SRC = { account: '本账号实测', account_partial: '本账号(日志未满窗)', insufficient: '数据不足', pool_measured: '池中位数', pool_configured: '配置值', unknown: '未知' };
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
      { t: '6h 刷新（台北）', f: (r) => resetCell(r) },
      { t: '6h 剩余$', num: 1, f: (r) => usd(r.rolling_remaining_usd) },
      { t: '月度', f: (r) => qbar(r.monthly_pct, r.monthly_reset_at) },
      { t: '本期已记录$', num: 1, f: (r) => usd(r.month_cost) },
      { t: '月容量$', num: 1, f: (r) => { const src = SRC[r.month_capacity_source] || r.month_capacity_source || ''; const v = r.month_capacity_usd; const txt = (v === null || v === undefined) ? (r.month_capacity_source === 'insufficient' ? '不足' : '—') : ('$' + Number(v).toFixed(2)); return h('span', { title: '依据：' + src }, txt); } },
      { t: '月剩余$', num: 1, f: (r) => usd(r.month_remaining_usd) },
      { t: '月度重置', f: (r) => shortT(r.monthly_reset_at) },
      { t: '查询于', f: (r) => shortT(r.checked_at) },
    ], d.items, '暂无启用账号');
    const rt = d.rates;
    $('#q-note').textContent = `估算口径：按 Opus 官方价 输入 ${rt.input}/M、缓存读 ${rt.cache_read}/M、输出 ${rt.output}/M 计算本网关日志里的 token 花费；各账号月容量 = 该账号本期已记录$ ÷ 月度已用%（已用≥5%且花费>0）；日志未覆盖整窗时标为「本账号(日志未满窗)」，数据不足显示「不足」，不再套用池中位数。上方中位数仅为可估账号的汇总。Notion 按 credits 计量，仅供参考。`;
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
        if (r.candidates.length === 1 && !r.candidates[0].existing_id) { await nlCommit(r.candidates[0].space_id); return; }
        f.space.replaceChildren(...r.candidates.map((c) => h('option', { value: c.space_id }, `${c.space_name || c.space_id} [${(c.space_id || '').slice(0, 8)}] (${c.plan || '?'}) — ${c.user_email} — ${c.capability === 'claude' ? 'Claude 可用' : c.capability === 'gpt_only' ? '仅 GPT（受限）' : '能力未知'}${c.existing_id ? ` — 已添加 #${c.existing_id}（将刷新 token）` : ' — 新工作区'}`)));
        nlShow(3);
      } else {
        await nlCommit(f.space.value);
      }
    } catch (err) { $('#nl-err').textContent = err.message; }
    finally { $('#nl-next').disabled = false; }
  });
  $('#nl-cancel').addEventListener('click', () => { if (nl.flow) api('/notion-login/cancel', { method: 'POST', body: JSON.stringify({ flow_id: nl.flow, code: '-' }) }).catch(() => { }); nl.flow = null; });

  // ---------- API keys ----------
  const keyRevealCache = Object.create(null);
  const ksRevealed = new Set();
  const ks = { models: [], editing: null };
  const dtLocal = (t) => { if (!t) return ''; const d = new Date(t * 1000); const p = (n) => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };
  function scopeCell(r) {
    if (r.all_models) return h('span', { class: 'badge ok' }, '全部模型');
    if (!r.models.length) return h('span', { class: 'badge bad' }, '未授权任何模型');
    const shown = r.models.slice(0, 6);
    return h('div', { class: 'key-models-cell', title: r.models.join(', ') }, shown.map((m) => h('span', { class: 'badge' }, m)), r.models.length > shown.length ? h('span', { class: 'muted small' }, `+${r.models.length - shown.length}`) : null);
  }
  function usageCell(u) {
    if (!u) return h('span', { class: 'muted' }, '未使用');
    return h('span', { title: `错误 ${fmt(u.errors)} · 输入 ${fmt(u.prompt_tokens)} · 输出 ${fmt(u.completion_tokens)}` }, `${fmt(u.requests)} 次 · ${ts(u.last_ts)}`);
  }
  
  async function fetchKeyPlain(id) {
    if (keyRevealCache[id]) return keyRevealCache[id];
    const path = id === 'master' ? '/keys/master/reveal' : `/keys/${encodeURIComponent(id)}/reveal`;
    const d = await api(path, { method: 'POST' });
    keyRevealCache[id] = d.key;
    return d.key;
  }
  function copyText(t) {
    return navigator.clipboard.writeText(t).then(() => toast('已复制')).catch(() => toast('复制失败，请手动选择复制'));
  }
  function keyPlainCell(r, masterConfigured) {
    if (r._master) {
      if (!masterConfigured) return h('span', { class: 'muted' }, '未配置');
      return keyRevealControls('master', '主 Key（环境变量）');
    }
    if (!r.has_plaintext) {
      return h('div', { class: 'key-cell' },
        h('code', { class: 'kh' }, r.hint || r.fingerprint || '—'),
        h('span', { class: 'badge', title: '磁盘上只有 SHA-256，无法还原明文' }, '仅哈希'),
        h('span', { class: 'muted small' }, '需重新生成才能查看'));
    }
    return keyRevealControls(r.id, r.hint || 'sk-n2a-…');
  }
  function keyRevealControls(id, masked) {
    const shown = ksRevealed.has(id);
    const plain = keyRevealCache[id];
    return h('div', { class: 'key-cell' },
      h('code', { class: 'kh' + (shown ? ' shown' : '') }, shown && plain ? plain : masked),
      h('button', { class: 'btn small', type: 'button', title: shown ? '隐藏' : '显示明文', onclick: async () => {
        try {
          if (!ksRevealed.has(id)) { await fetchKeyPlain(id); ksRevealed.add(id); }
          else ksRevealed.delete(id);
          loadKeys();
        } catch (e) { toast(e.message); }
      } }, shown ? '隐藏' : '显示'),
      h('button', { class: 'btn small', type: 'button', title: '复制明文', onclick: async () => {
        try { await copyText(await fetchKeyPlain(id)); } catch (e) { toast(e.message); }
      } }, '复制'));
  }

  async function loadKeys() {
    const d = await api('/keys');
    ks.models = d.models || [];
    const active = d.items.filter((k) => k.enabled && !k.expired).length;
    $('#key-banner').textContent = (d.auth_required ? '已启用鉴权' : '⚠️ 未设置主 API_KEY：/v1 对任何请求开放') + ` · 主 Key（环境变量 API_KEY）拥有全部模型，不在此修改 · ${active} 把子 Key 生效 · 存储：data/${d.file}`;
    const rows = [{ _master: true, name: '主 Key（API_KEY）', usage: d.master.usage, has_plaintext: !!(d.master && d.master.has_plaintext) }].concat(d.items);
    table($('#key-table'), [
      { t: '名称', f: (r) => r._master ? h('b', {}, r.name) : h('div', {}, h('b', {}, r.name), r.note ? h('div', { class: 'muted small' }, r.note) : null) },
      { t: 'Key', f: (r) => keyPlainCell(r, d.master && d.master.configured) },
      { t: '可用模型', f: (r) => r._master ? h('span', { class: 'badge ok' }, '全部模型') : scopeCell(r) },
      { t: '状态', f: (r) => r._master ? h('span', { class: 'badge ok' }, d.master.configured ? '启用' : '未配置') : !r.enabled ? h('span', { class: 'badge' }, '已停用') : r.expired ? h('span', { class: 'badge bad' }, '已过期') : h('span', { class: 'badge ok' }, '启用') },
      { t: '创建', f: (r) => r._master ? '—' : (r.created_at ? ts(r.created_at) : (r.created || '—')) },
      { t: '过期', f: (r) => r._master ? '—' : (r.expires_at ? ts(r.expires_at) + (r.expired ? '（已过期）' : '') : '永不') },
      { t: '用量（30 天）', f: (r) => usageCell(r.usage) },
      { t: '操作', f: (r) => r._master ? h('button', { class: 'btn small', onclick: () => showKeyRequests('api_key') }, '请求') : h('div', { class: 'actions' },
          h('div', { class: 'actions-group' },
            h('button', { class: 'btn small', onclick: () => openKeyDialog(r) }, '编辑'),
            h('button', { class: 'btn small', onclick: () => toggleKey(r) }, r.enabled ? '停用' : '启用'),
            h('button', { class: 'btn small', onclick: () => showKeyRequests('key:' + r.name) }, '请求')),
          h('div', { class: 'actions-group' },
            h('button', { class: 'btn small', onclick: () => regenKey(r) }, '重新生成'),
            h('button', { class: 'btn small danger', onclick: () => delKey(r) }, '删除'))) },
    ], rows, '暂无 Key');
  }
  function showKeyRequests(auth) { $('#rq-auth').value = auth; state.rqOffset = 0; location.hash = '#requests'; }
  function renderModelChecks(selected, filter) {
    const box = $('#key-models'); const f = (filter || '').toLowerCase();
    box.replaceChildren(...ks.models.filter((m) => !f || (m.id + ' ' + m.name + ' ' + m.aliases.join(' ')).toLowerCase().includes(f)).map((m) =>
      h('label', { title: m.aliases.length ? '别名：' + m.aliases.join(', ') : m.id },
        h('input', { type: 'checkbox', value: m.id, 'data-fam': m.family, checked: selected.has(m.id) || undefined, onchange: (e) => { e.target.checked ? ks.sel.add(m.id) : ks.sel.delete(m.id); countModels(); } }),
        h('span', {}, m.id, m.aliases.length ? h('span', { class: 'alias' }, ' (' + m.aliases.join(', ') + ')') : null))));
    countModels();
  }
  function countModels() {
    const all = $('#key-form').all_models.checked;
    $('#key-models').classList.toggle('disabled', all);
    const extra = [...ks.sel].filter((m) => !ks.models.some((x) => x.id === m));
    $('#key-models-count').textContent = all ? '不限制模型' : `已选 ${ks.sel.size} 个模型` + (extra.length ? `（含自定义：${extra.join(', ')}）` : '');
  }
  function openKeyDialog(r) {
    const f = $('#key-form'); f.reset(); $('#key-err').textContent = ''; ks.editing = r ? r.id : null;
    $('#key-dialog-title').textContent = r ? `编辑 Key · ${r.name}` : '新建 Key';
    f.querySelectorAll('.new-only').forEach((el) => el.classList.toggle('hidden', !!r));
    f.name.value = r ? r.name : ''; f.note.value = r ? (r.note || '') : '';
    f.enabled.checked = r ? r.enabled : true; f.all_models.checked = r ? r.all_models : false;
    f.expires.value = r && r.expires_at ? dtLocal(r.expires_at) : '';
    // map aliases (e.g. claude-opus5.5) onto the catalog's primary id so the right box is ticked; globs / unknown ids kept as-is
    const canon = (m) => { const c = ks.models.find((x) => x.id === m || x.aliases.includes(m)); return c ? c.id : m; };
    ks.sel = new Set(r ? r.models.map(canon) : []);
    $('#key-model-filter').value = '';
    renderModelChecks(ks.sel, '');
    $('#key-dialog').showModal();
  }
  $('#key-add-btn').addEventListener('click', () => openKeyDialog(null));
  $('#key-form').all_models.addEventListener('change', countModels);
  $('#key-model-filter').addEventListener('input', (e) => renderModelChecks(ks.sel, e.target.value));
  document.querySelectorAll('.models-tools [data-fam]').forEach((b) => b.addEventListener('click', () => {
    const fam = b.dataset.fam;
    if (fam === 'none') ks.sel.clear();
    else ks.models.forEach((m) => { if (fam === '*' || m.family === fam) ks.sel.add(m.id); });
    renderModelChecks(ks.sel, $('#key-model-filter').value);
  }));
  function showSecret(key, id) { $('#key-secret').textContent = key; if (id) { keyRevealCache[id] = key; ksRevealed.add(id); } $('#key-secret-dialog').showModal(); }
  $('#key-copy').addEventListener('click', async () => { try { await navigator.clipboard.writeText($('#key-secret').textContent); toast('已复制'); } catch (e) { toast('复制失败，请手动选择复制'); } });
  $('#key-secret-dialog').addEventListener('close', () => { $('#key-secret').textContent = ''; });
  $('#key-save').addEventListener('click', async (e) => {
    e.preventDefault(); const f = $('#key-form'); $('#key-err').textContent = '';
    const exp = f.expires.value ? Math.floor(new Date(f.expires.value).getTime() / 1000) : 0;
    const body = { name: f.name.value.trim(), note: f.note.value.trim(), enabled: f.enabled.checked, all_models: f.all_models.checked, models: [...ks.sel], expires_at: exp };
    if (!body.name) { $('#key-err').textContent = '请填写名称'; return; }
    if (!body.all_models && !body.models.length && !confirm('没有勾选任何模型，这个 Key 将无法调用任何模型，确定保存？')) return;
    try {
      if (ks.editing) {
        await api(`/keys/${encodeURIComponent(ks.editing)}`, { method: 'PATCH', body: JSON.stringify(body) });
        $('#key-dialog').close(); toast('已保存，即时生效');
      } else {
        const ck = f.custom_key.value.trim(); if (ck) body.custom_key = ck;
        const r = await api('/keys', { method: 'POST', body: JSON.stringify(body) });
        f.custom_key.value = ''; $('#key-dialog').close(); showSecret(r.key, r.item && r.item.id);
      }
      loadKeys();
    } catch (err) { $('#key-err').textContent = err.message; }
  });
  async function toggleKey(r) { try { await api(`/keys/${encodeURIComponent(r.id)}`, { method: 'PATCH', body: JSON.stringify({ enabled: !r.enabled }) }); toast(r.enabled ? 'Key 已停用' : 'Key 已启用'); } catch (e) { toast(e.message); } loadKeys(); }
  async function regenKey(r) {
    if (!confirm(`重新生成「${r.name}」？旧 Key 会立刻失效，使用它的客户端需要换成新 Key。`)) return;
    try { const d = await api(`/keys/${encodeURIComponent(r.id)}/regenerate`, { method: 'POST' }); showSecret(d.key, d.item && d.item.id); } catch (e) { toast(e.message); }
    loadKeys();
  }
  async function delKey(r) {
    if (!confirm(`确定删除 Key「${r.name}」？使用它的客户端会立刻无法访问。`)) return;
    try { await api(`/keys/${encodeURIComponent(r.id)}`, { method: 'DELETE' }); toast('Key 已删除'); } catch (e) { toast(e.message); }
    loadKeys();
  }

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
    const q = new URLSearchParams({ limit: state.rqLimit, offset: state.rqOffset, status: $('#rq-status').value, model: $('#rq-model').value.trim(), auth: $('#rq-auth').value.trim() });
    const d = await api('/requests?' + q);
    table($('#rq-table'), [
      { t: '时间', f: (r) => ts(r.ts) }, { t: '模型', k: 'model' }, { t: '流式', f: (r) => (r.stream ? '是' : '否') },
      { t: '状态', f: (r) => h('span', { class: 'badge ' + (r.status >= 400 || r.error_code ? 'bad' : 'ok') }, String(r.status || '—')) },
      { t: '账号', k: 'account' }, { t: '鉴权', k: 'auth' }, { t: 'IP', k: 'ip' },
      { t: '思考', f: (r) => { const sent = r.effort_sent || r.effort_final || ''; const req = r.effort_req || '';
        return h('span', { title: `请求 ${req || '默认'} → 实发 ${r.effort_sent || '（旧记录：未记录实发值）'}` + (r.hedge_effort ? ` · 对冲 ${r.hedge_effort}` : '') },
          sent ? (r.effort_sent ? sent : sent + '*') : '—'); } },
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
