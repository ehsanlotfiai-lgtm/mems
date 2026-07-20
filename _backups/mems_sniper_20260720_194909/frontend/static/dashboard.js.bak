/* ============================================================
   MemeCoin Sniper Dashboard - vanilla JS
   ============================================================ */

const $ = (q) => document.querySelector(q);

/* ═══ Shared pagination helper for history tables (Signals/Scalp/LIT) ═══ */
const _paginationState = {}; // key -> current page (0-indexed)

function paginate(items, key, pageSize = 15) {
  const page = _paginationState[key] || 0;
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const clampedPage = Math.min(page, totalPages - 1);
  _paginationState[key] = clampedPage;
  const start = clampedPage * pageSize;
  return {
    pageItems: items.slice(start, start + pageSize),
    page: clampedPage,
    totalPages,
    totalItems: items.length,
  };
}

function renderPaginationControls(key, totalPages, currentPage, onChangeFnName) {
  if (totalPages <= 1) return '';
  const prevDisabled = currentPage <= 0 ? 'disabled style="opacity:0.4;cursor:not-allowed"' : '';
  const nextDisabled = currentPage >= totalPages - 1 ? 'disabled style="opacity:0.4;cursor:not-allowed"' : '';
  return `
    <div class="pagination-bar" style="display:flex;align-items:center;justify-content:center;gap:10px;padding:10px 0;font-size:12px">
      <button class="btn btn-secondary" ${prevDisabled} onclick="event.stopPropagation();${onChangeFnName}(${currentPage - 1})" style="padding:4px 12px">◀ قبلی</button>
      <span style="color:var(--text-muted)">صفحه ${currentPage + 1} از ${totalPages}</span>
      <button class="btn btn-secondary" ${nextDisabled} onclick="event.stopPropagation();${onChangeFnName}(${currentPage + 1})" style="padding:4px 12px">بعدی ▶</button>
    </div>`;
}
const $$ = (q) => document.querySelectorAll(q);
const fmtTime = (s) => new Date((s || 0) * 1000).toLocaleString('fa-IR', {
  year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
});
const fmtN = (n, d = 6) => (n === null || n === undefined) ? '—' : Number(n).toLocaleString('en-US', {maximumFractionDigits: d});
const fmtPrice = (v, d) => {
  if (v === null || v === undefined || v === 0) return '—';
  if (d !== undefined) return '$' + Number(v).toLocaleString('en-US', {maximumFractionDigits: d});
  if (Math.abs(v) >= 1000) return '$' + fmtN(v, 2);
  if (Math.abs(v) >= 1) return '$' + fmtN(v, 4);
  if (Math.abs(v) >= 0.01) return '$' + fmtN(v, 6);
  if (Math.abs(v) >= 0.0001) return '$' + fmtN(v, 8);
  return '$' + fmtN(v, 10);
};

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  setTimeout(() => t.classList.add('hidden'), 2500);
}

/* ---------- RESET SECTIONS ---------- */
async function resetSection(section) {
  const confirmMsg = {
    signals: 'آیا از پاک کردن تاریخچه سیگنال‌ها مطمئنید؟',
    scalping: 'آیا از پاک کردن سیگنال‌های اسکلپ مطمئنید؟',
    hunter: 'آیا از ریست شکارچی مطمئنید؟ (اسکن مجدد انجام می‌شود)',
    success: 'آیا از پاک کردن آمار موفقیت مطمئنید؟',
    positions: 'آیا از بستن همه پوزیشن‌ها و ریست موجودی مطمئنید؟',
    trades: '⚠️ آیا از پاک کردن کل تاریخچه تریدها مطمئنید؟ این عمل غیرقابل بازگشت است!',
    universe: 'آیا از پاک کردن واچ‌لیست مطمئنید؟',
    settings: 'آیا از ریست تنظیمات به حالت پیش‌فرض مطمئنید؟ (ریستارت سرویس نیاز است)',
  };
  if (!confirm(confirmMsg[section] || 'آیا مطمئنید؟')) return;

  try {
    const r = await fetch(`/api/reset/${section}`, { method: 'POST' });
    const res = await r.json();
    if (res.ok) {
      toast('✅ ' + res.msg);
      // Reload relevant data
      if (section === 'signals') loadSignals();
      if (section === 'scalping') loadScalping();
      if (section === 'hunter') loadHunterResults();
      if (section === 'success') loadSuccessRate();
      if (section === 'positions' || section === 'trades') { loadTrades(); loadPosWinRates(); refreshState(); }
      if (section === 'universe') { stateCache.universe = {}; renderUniverse(); }
      if (section === 'settings') loadSettings();
    } else {
      toast('❌ خطا: ' + (res.error || 'ناشناخته'));
    }
  } catch (e) {
    toast('❌ خطا در ارتباط با سرور');
    console.error('resetSection error:', e);
  }
}

/* ---------- TABS ---------- */
$$('.tab-btn').forEach(btn => btn.addEventListener('click', () => {
  $$('.tab-btn').forEach(b => b.classList.remove('active'));
  $$('.tab-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  $(`#tab-${btn.dataset.tab}`).classList.add('active');
  if (btn.dataset.tab === 'signals') loadSignals();
  if (btn.dataset.tab === 'scalping') loadScalping();
  if (btn.dataset.tab === 'hunter') loadHunterResults();
  if (btn.dataset.tab === 'success') loadSuccessRate();
  if (btn.dataset.tab === 'positions') { loadTrades(); loadPosWinRates(); }
  if (btn.dataset.tab === 'assistant') loadAssistantLog();
  if (btn.dataset.tab === 'universe') renderUniverse();
  if (btn.dataset.tab === 'settings') loadSettings();
}));

/* ---------- STATE ---------- */
async function json(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    // Try to extract short error message, not full HTML page
    let msg = 'خطای سرور';
    try {
      const text = await r.text();
      // FastAPI returns {"detail":"..."} for HTTPException
      try { const j = JSON.parse(text); msg = j.detail || j.error || text.slice(0, 100); }
      catch { msg = text.slice(0, 100); }  // fallback: first 100 chars
    } catch {}
    throw new Error(msg);
  }
  return r.json();
}

async function refreshState() {
  try {
    const st = await json('/api/state');
    $('#equity').textContent = fmtN(st.risk.equity, 2) + ' USDT';
    $('#equity').className = 'card-value ' + (st.risk.blocked ? 'danger' : (st.risk.daily_pnl_pct >= 0 ? 'success' : ''));
    $('#open_count').textContent = st.risk.open_count;
    const pnl = st.risk.daily_pnl_pct;
    $('#daily_pnl').textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%';
    $('#daily_pnl').className = 'card-value ' + (pnl >= 0 ? 'success' : 'danger');
    stateCache.universe = st.universe;
    renderUniverse();
  } catch (e) { console.error(e); }
  // Fetch live BTC/ETH prices
  try {
    const btcResp = await fetch('/api/chart/binance/BTC_USDT/1m?limit=1');
    const btcData = await btcResp.json();
    if (btcData.candles && btcData.candles.length > 0) {
      const p = btcData.candles[btcData.candles.length - 1].close;
      const el = document.getElementById('live_btc');
      if (el) el.textContent = '$' + Number(p).toLocaleString('en-US', {maximumFractionDigits: 0});
      livePrices['BTC/USDT'] = p;
    }
    const ethResp = await fetch('/api/chart/binance/ETH_USDT/1m?limit=1');
    const ethData = await ethResp.json();
    if (ethData.candles && ethData.candles.length > 0) {
      const p = ethData.candles[ethData.candles.length - 1].close;
      const el = document.getElementById('live_eth');
      if (el) el.textContent = '$' + Number(p).toLocaleString('en-US', {maximumFractionDigits: 1});
      livePrices['ETH/USDT'] = p;
    }
  } catch(e) { /* non-critical */ }
}

/* ---------- MARKET SENTIMENT ---------- */
async function loadMarketSentiment() {
  try {
    const { sentiment } = await json('/api/market/sentiment');
    renderMarketSentiment(sentiment);
  } catch (e) { console.error('loadMarketSentiment:', e); }
}

function renderMarketSentiment(s) {
  const el = $('#market_sentiment');
  if (!el || !s || !s.fear_greed_value) return;
  const fg = s.fear_greed_value;
  const label = s.fear_greed_label || 'Neutral';
  const change = s.fear_greed_change || 0;
  const btc = s.btc_24h_change || 0;
  const trending = s.trending_count || 0;

  // Color based on fear/greed
  let color = 'var(--text-dim)';
  if (fg <= 25) color = '#ff5a6e';      // Extreme Fear
  else if (fg <= 45) color = '#f97316'; // Fear
  else if (fg <= 55) color = 'var(--text-dim)'; // Neutral
  else if (fg <= 75) color = '#34d399'; // Greed
  else color = '#22c55e';               // Extreme Greed

  const changeIcon = change > 0 ? '📈' : change < 0 ? '📉' : '➡️';
  const btcColor = btc >= 0 ? 'var(--success)' : 'var(--danger)';
  const btcIcon = btc >= 0 ? '🟢' : '🔴';

  el.innerHTML = `
    <span style="color:${color};font-weight:700">Fear&Greed: ${fg}</span>
    <span style="color:var(--text-dim);font-size:11px">(${label} ${changeIcon}${change > 0 ? '+' : ''}${change})</span>
    <span style="margin:0 4px;color:var(--border)">|</span>
    <span style="color:${btcColor}">${btcIcon} BTC ${btc >= 0 ? '+' : ''}${btc.toFixed(1)}%</span>
    <span style="margin:0 4px;color:var(--border)">|</span>
    <span style="color:var(--accent)">🔥 ${trending} trending</span>
  `;
  el.style.fontSize = '13px';
}

const stateCache = { signals: [], universe: {} };
let livePrices = {};  // {symbol: price} — live prices from WebSocket
const liveSignalBox = $('#signals_live');

function renderSignalCard(s) {
  const sideFa = s.side === 'long' ? '🟢 خرید (LONG)' : '🔴 فروش (SHORT)';
  const methods = [...new Set(s.hits.map(h => h.name))];
  const methodFa = {
    new_listing: '🆕 تازه‌لیست', volume_spike: '🔊 حجم', orderbook_imbalance: '⚖️ اردربوک',
    liquidity_grab: '🌊 شکار', momentum_ignition: '🔥 مومنتوم', rsi_divergence: '📊 واگرایی',
    bb_breakout: '🎯 بولینگر', funding_oi_spike: '💸 funding/OI', social_momentum: '📱 سوشال',
    ema_cross: '📈 EMA', adx_trend: '📊 ADX', squeeze_momentum: '💎 Squeeze', vwap: '📐 VWAP'
  };
  const statusFa = { open: '🟢 فعال', tp: '✅ سود', sl: '❌ ضرر', trailing: '🔄 تریلینگ', closed: '⏰ بسته', no_position: '⏭️ رد شد (ظرفیت پر)' };
  const statusClass = { open: 'status-active', tp: 'status-tp', sl: 'status-sl', trailing: 'status-trailing', closed: 'status-closed' };
  const isNew = methods.includes('new_listing');
  const displayName = s.base || (s.symbol.startsWith('DEX:') ? s.symbol.split(':').pop().slice(0, 8) : s.symbol);
  const isDex = s.symbol.startsWith('DEX:');
  const chainTag = isDex ? (() => { const parts = s.symbol.split(':'); return parts.length > 1 ? parts[1] : 'dex'; })() : '';
  // TF breakdown display — bold & prominent
  const tfBreakdown = s.confluence_tf_breakdown || {};
  const tfPills = Object.entries(tfBreakdown)
    .filter(([, v]) => v > 0)
    .map(([tf, v]) => {
      const pct = Math.round(v * 100);
      const color = pct >= 70 ? 'var(--success)' : pct >= 50 ? 'var(--accent)' : 'var(--warning)';
      return `<span class="pill tf-pill" style="border-color:${color};color:${color};font-weight:700">${tf}: ${pct}%</span>`;
    })
    .join('');
  // Detect dominant TF (highest score)
  let dominantTf = '';
  let maxTfScore = 0;
  for (const [tf, v] of Object.entries(tfBreakdown)) {
    if (v > maxTfScore) { maxTfScore = v; dominantTf = tf; }
  }
  const dominantPill = dominantTf
    ? `<span class="pill tf-dominant-pill" style="background:var(--accent);color:#fff;font-weight:700">⚡ مبنا: ${dominantTf}</span>`
    : '';
  const sigStatus = s.status || 'open';
  return `
    <div class="signal-card ${s.side} ${isNew ? 'new' : ''} ${isDex ? 'dex' : ''} ${statusClass[sigStatus] || ''}"
         style="cursor:pointer" onclick="showSignalOnChart('${s.id}')" title="کلیک کنید تا روی نمودار نمایش داده شود">
      <div class="sym-row">
        <span class="sym">${displayName}</span>
        <span class="exch">${s.exchange}${chainTag ? ' <small>(' + chainTag + ')</small>' : ''}</span>
        <span class="sig-badge ${statusClass[sigStatus] || ''}">${statusFa[sigStatus] || sigStatus}</span>
      </div>
      <div class="pills">
        ${isDex ? '<span class="pill dex-pill">🔗 DEX</span>' : '<span class="pill" style="background:rgba(59,130,246,0.15);color:#3b82f6;font-weight:700">🔵 فیوچرز</span>'}
        ${methods.map(m => `<span class="pill">${methodFa[m] || m}</span>`).join('')}
      </div>
      ${tfPills ? '<div class="pills tf-pills">' + dominantPill + tfPills + '</div>' : ''}
      <div class="side ${s.side}">${sideFa}</div>
      <div class="scorebar"><div style="width:${Math.round(s.score * 100)}%"></div></div>
      <div>امتیاز سیگنال: <b>${s.score.toFixed(2)}</b></div>
      <div class="levels">
        <div class="level">ورود<b>${fmtPrice(s.entry)}</b></div>
        <div class="level" style="color:#10b981">TP1<b>${fmtPrice(s.take_profit)}</b></div>
        <div class="level" style="color:#a855f7">TP2<b>${s.tp2 ? fmtPrice(s.tp2) : '—'}</b></div>
        <div class="level" style="color:#f97316">TP3<b>${s.tp3 ? fmtPrice(s.tp3) : '—'}</b></div>
        <div class="level" style="color:#ef4444">SL<b>${fmtPrice(s.stop_loss)}</b></div>
        <div class="level" style="color:#38bdf8;background:rgba(56,189,248,0.1)">💲 فعلی<b>${livePrices[s.symbol] ? fmtPrice(livePrices[s.symbol]) : '...'}</b></div>
      </div>
      <div class="rationale">${s.rationale || ''}</div>
      <div style="margin-top:6px;font-size:11px;color:#38bdf8">📊 کلیک کنید → نمایش روی نمودار</div>
    </div>
  `;
}

function addLiveSignal(s) {
  stateCache.signals.unshift(s);
  if (stateCache.signals.length > 30) stateCache.signals.pop();
  liveSignalBox.insertAdjacentHTML('afterbegin', renderSignalCard(s));
  $('#signal_count').textContent = stateCache.signals.length;
}

/* Signal status filter */
let signalFilter = 'all';
function setSignalFilter(f) {
  signalFilter = f;
  document.querySelectorAll('.sig-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  _paginationState['signals_history'] = 0;
  loadSignals();
}

async function loadSignals() {
  try {
    const { signals } = await json('/api/signals?limit=100');
    stateCache.signals = signals.slice(0, 30);
    // Filter live cards
    const filtered = signalFilter === 'all' ? stateCache.signals : stateCache.signals.filter(s => (s.status || 'open') === signalFilter);
    liveSignalBox.innerHTML = filtered.map(renderSignalCard).join('') || '<p style="color:#8e95ac">سیگنالی در این دسته نیست.</p>';
    $('#signal_count').textContent = signals.length;
    // history table (paginated — see renderSignalsHistoryPage)
    window._allSignalsHistory = signals;
    renderSignalsHistoryPage();
    // Load signal win rates
    loadSignalWinRates();
    // Fetch live prices for displayed signals
    fetchPricesForSignals(signals);
  } catch (e) { console.error(e); }
}

const statusFaMain = { open: '🟢 فعال', tp: '✅ سود', sl: '❌ ضرر', trailing: '🔄 تریلینگ', closed: '⏰ بسته', no_position: '⏭️ رد شد (ظرفیت پر)' };

function changeSignalsHistoryPage(newPage) {
  _paginationState['signals_history'] = newPage;
  renderSignalsHistoryPage();
}

function renderSignalsHistoryPage() {
  const tb = $('#signals_history');
  const pagerEl = $('#signals_history_pager');
  const signals = window._allSignalsHistory || [];
  if (!tb) return;
  const { pageItems, page, totalPages } = paginate(signals, 'signals_history', 15);
  tb.innerHTML = pageItems.map(s => {
    const curPrice = livePrices[s.symbol] ? fmtPrice(livePrices[s.symbol]) : '...';
    const curColor = livePrices[s.symbol] ? (livePrices[s.symbol] > s.entry ? (s.side === 'long' ? 'var(--success)' : 'var(--danger)') : (s.side === 'long' ? 'var(--danger)' : 'var(--success)')) : '';
    return `
    <tr class="clickable-row" style="cursor:pointer" onclick="showSignalOnChart('${s.id}')" title="کلیک کنید تا نمودار ورود/خروج این سیگنال نمایش داده شود">
      <td>${fmtTime(s.created_at)}</td><td>${s.exchange}</td>
      <td>${s.base || s.symbol}${s.symbol.startsWith('DEX:') ? ' <small style="color:#f97316">DEX</small>' : ''}</td>
      <td class="${s.side}">${s.side.toUpperCase()}</td>
      <td>${s.score.toFixed(2)}</td>
      <td>${fmtPrice(s.entry)}</td><td>${fmtPrice(s.take_profit)}</td><td>${fmtPrice(s.stop_loss)}</td>
      <td style="color:${curColor};font-weight:bold">${curPrice}</td>
      <td><span class="sig-badge-inline status-${s.status || 'open'}">${statusFaMain[s.status || 'open'] || s.status}</span></td>
      <td>${[...new Set(s.hits.map(h => h.name))].join(', ')}</td>
    </tr>`;
  }).join('');
  if (pagerEl) pagerEl.innerHTML = renderPaginationControls('signals_history', totalPages, page, 'changeSignalsHistoryPage');
}

/* Fetch live prices for signal symbols */
async function fetchPricesForSignals(signals) {
  const symbols = [...new Set(signals.map(s => s.symbol).filter(s => s && !s.startsWith('DEX:')))].slice(0, 20);
  for (const sym of symbols) {
    if (livePrices[sym]) continue; // Already have it
    try {
      const resp = await fetch(`/api/chart/binance/${sym.replace('/', '_')}/1m?limit=1`);
      const data = await resp.json();
      if (data.candles && data.candles.length > 0) {
        livePrices[sym] = data.candles[data.candles.length - 1].close;
      }
    } catch(e) {}
  }
  // Re-render after prices loaded
  if (symbols.length > 0) {
    const liveBox = $('#signals_live');
    if (liveBox && stateCache.signals.length > 0) {
      const filtered = signalFilter === 'all' ? stateCache.signals : stateCache.signals.filter(s => (s.status || 'open') === signalFilter);
      liveBox.innerHTML = filtered.map(renderSignalCard).join('') || '';
    }
  }
}

/* Signal Win Rates */
async function loadSignalWinRates() {
  try {
    const resp = await json('/api/trades/win-rates');
    const win_rates = resp.win_rates;
    const strategy_stats = resp.strategy_stats || {};
    if (!win_rates) return;
    
    const periods = [
      { key: 'hourly', label: 'آخرین ساعت' },
      { key: '4hour', label: 'آخرین ۴ ساعت' },
      { key: 'daily', label: 'امروز' },
      { key: 'weekly', label: 'هفته اخیر' },
    ];
    
    // Update the grid boxes
    const el1h = document.getElementById('sig_wr_5m');
    const el30 = document.getElementById('sig_wr_30m');
    const el1 = document.getElementById('sig_wr_1h');
    const el4h = document.getElementById('sig_wr_4h');
    const el24h = document.getElementById('sig_wr_24h');
    
    const hr = win_rates.hourly || {};
    const h4 = win_rates['4hour'] || {};
    const dy = win_rates.daily || {};
    const wk = win_rates.weekly || {};
    
    const fmt = (d) => {
      if (!d || d.total === 0) return '—';
      return d.win_rate + '%';
    };
    const sub = (d) => {
      if (!d || d.total === 0) return '';
      return `${d.wins}W/${d.losses}L`;
    };
    const color = (d) => {
      if (!d || d.total === 0) return 'color:var(--text-muted)';
      return d.win_rate >= 60 ? 'color:var(--success)' : d.win_rate >= 40 ? 'color:var(--accent)' : 'color:var(--danger)';
    };
    
    if (el1h) { el1h.innerHTML = `<span style="${color(hr)}">${fmt(hr)}</span><br><small style="color:var(--text-muted)">${sub(hr)}</small>`; }
    if (el30) { el30.innerHTML = `<span style="${color(h4)}">${fmt(h4)}</span><br><small style="color:var(--text-muted)">${sub(h4)}</small>`; }
    if (el1) { el1.innerHTML = `<span style="${color(dy)}">${fmt(dy)}</span><br><small style="color:var(--text-muted)">${sub(dy)}</small>`; }
    if (el4h) { el4h.innerHTML = `<span style="${color(wk)}">${fmt(wk)}</span><br><small style="color:var(--text-muted)">${sub(wk)}</small>`; }
    if (el24h) { 
      // Total all-time
      const all = win_rates.all || {};
      el24h.innerHTML = `<span style="${color(all)}">${fmt(all)}</span><br><small style="color:var(--text-muted)">${sub(all)}</small>`; 
    }
    // Render per-strategy win rates
    const stratBox = document.getElementById('sig_strategy_stats');
    if (stratBox && Object.keys(strategy_stats).length > 0) {
      const sn = {'confluence':'🔗 کانفلوئنس','momentum':'🔥 مومنتوم','breakout':'🚀 بریک‌اوت','reversal':'🔄 ریورسال','trend':'📈 ترند','range':'📦 رنج','volume':'📊 حجم','pattern':'🕯 پترن','unknown':'📋 سایر'};
      var html = '<div style="margin-top:16px"><h4 style="margin:0 0 10px 0;font-size:13px;color:var(--text-muted)">📊 وین ریت هر استراتژی</h4><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px">';
      Object.entries(strategy_stats).sort(function(a,b){return b[1].total-a[1].total;}).forEach(function(e){
        var nm=e[0],st=e[1];
        var wc=st.win_rate>=60?'var(--success)':st.win_rate>=40?'var(--accent)':'var(--danger)';
        var pc=st.avg_pnl>=0?'var(--success)':'var(--danger)';
        html+='<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px;text-align:center">'
          +'<div style="font-size:11px;font-weight:bold;margin-bottom:4px">'+(sn[nm]||nm)+'</div>'
          +'<div style="font-size:20px;font-weight:bold;color:'+(st.total>0?wc:'var(--text-dim)')+'">'+( st.total>0?st.win_rate+'%':'—')+'</div>'
          +'<div style="font-size:11px;color:var(--text-muted)">'+st.wins+'W / '+st.losses+'L ('+st.total+' ترید)</div>'
          +'<div style="font-size:11px;color:'+pc+'">میانگین: '+(st.avg_pnl>=0?'+':'')+st.avg_pnl+'%</div>'
          +'</div>';
      });
      html += '</div></div>';
      stratBox.innerHTML = html;
    }
  } catch(e) { console.debug('Signal WR:', e); }
}

/* Position filter */
let posFilter = 'all';
function setPosFilter(f) {
  posFilter = f;
  document.querySelectorAll('.pos-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  loadTrades();
}

/* ---------- POSITION WIN RATES ---------- */
async function loadPosWinRates() {
  try {
    const { win_rates } = await json('/api/trades/win-rates');
    renderPosWinRates(win_rates);
  } catch (e) { console.error('loadPosWinRates:', e); }
}

function renderPosWinRates(wr) {
  const box = $('#pos_win_rates');
  if (!box || !wr) return;

  const periods = [
    { key: 'hourly', label: '⏰ وین ریت ساعتی', icon: '⏰' },
    { key: '4hour', label: '🕓 وین ریت ۴ ساعته', icon: '🕓' },
    { key: 'daily', label: '📅 وین ریت روزانه', icon: '📅' },
    { key: 'all', label: '🎯 وین ریت کل', icon: '🎯' },
  ];

  box.innerHTML = periods.map(p => {
    const d = wr[p.key] || {};
    const wrVal = d.win_rate || 0;
    const wrClass = wrVal >= 60 ? 'success' : wrVal >= 40 ? 'accent' : wrVal > 0 ? 'danger' : '';
    const wrColor = wrVal >= 60 ? 'var(--success)' : wrVal >= 40 ? 'var(--accent)' : 'var(--danger)';
    const pnlVal = d.avg_pnl_pct || 0;
    const pnlColor = pnlVal >= 0 ? 'var(--success)' : 'var(--danger)';
    const openCount = d.open || 0;
    const detail = d.total > 0
      ? `${d.wins} برد / ${d.losses} باخت از ${d.total} ترید بسته‌شده` + (openCount > 0 ? ` + ${openCount} باز` : '')
      : openCount > 0
        ? `${openCount} ترید باز — هنوز بسته نشده`
        : 'هنوز تریدی ثبت نشده';
    return `
      <div class="pos-winrate-card ${wrClass}">
        <div class="wr-period">${p.label}</div>
        <div class="wr-value" style="color:${wrVal > 0 ? wrColor : 'var(--text-dim)'}">${d.total > 0 ? wrVal.toFixed(1) + '%' : '—'}</div>
        <div class="wr-detail">${detail}</div>
        <div class="wr-pnl" style="color:${pnlVal >= 0 ? pnlColor : 'var(--danger)'}">
          ${d.total > 0 ? 'میانگین: ' + (pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(1) + '%' : ''}
          ${(d.total_pnl_usdt && d.total_pnl_usdt !== 0) ? ' | ' + (d.total_pnl_usdt >= 0 ? '+' : '') + fmtN(d.total_pnl_usdt, 2) + ' USDT' : ''}
        </div>
      </div>
    `;
  }).join('');
}

function fmtDuration(startSec, endSec) {
  if (!startSec || !endSec) return '—';
  const diff = Math.abs(endSec - startSec);
  if (diff < 60) return Math.round(diff) + ' ثانیه';
  if (diff < 3600) return Math.round(diff / 60) + ' دقیقه';
  if (diff < 86400) return (diff / 3600).toFixed(1) + ' ساعت';
  return (diff / 86400).toFixed(1) + ' روز';
}

async function loadTrades() {
  try {
    // Also load time-based win rates
    loadPosWinRates();
    const { trades } = await json('/api/trades?limit=200');
    const filtered = posFilter === 'all' ? trades : trades.filter(t => (t.status || 'open') === posFilter);
    const tb = $('#trades_table');
    tb.innerHTML = filtered.map(t => {
      const mktType = t.market_type || (t.symbol && t.symbol.startsWith('DEX:') ? 'dex' : 'spot');
      const mktLabel = { spot: '🟢 اسپات', futures: '🔵 فیوچرز', dex: '🔗 DEX' }[mktType] || mktType;
      const lev = t.leverage || 1;
      const levStr = lev > 1 ? lev.toFixed(1) + 'x' : 'اسپات';
      const duration = fmtDuration(t.opened_at, t.closed_at);
      const statusOpen = t.status === 'open';
      const tp1Hit = t.tp1_hit;
      const riskFree = t.risk_free;
      // Live price and PnL
      const livePrice = livePrices[t.symbol] || null;
      let pnlStr = '—', pnlClass = '';
      if (statusOpen && livePrice) {
        const pnlPct = t.side === 'long'
          ? ((livePrice - t.entry) / t.entry * 100)
          : ((t.entry - livePrice) / t.entry * 100);
        pnlStr = (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%';
        pnlClass = pnlPct >= 0 ? 'pos' : 'neg';
      } else if (t.pnl_pct != null) {
        pnlStr = t.pnl_pct + '%';
        pnlClass = (t.pnl_pct || 0) >= 0 ? 'pos' : 'neg';
      }
      // Status badges
      let badges = '';
      if (riskFree) badges += '<span class="pill" style="background:rgba(16,185,129,0.15);color:#10b981;font-size:10px">🛡 ریسک‌فری</span> ';
      if (tp1Hit) badges += '<span class="pill" style="background:rgba(59,130,246,0.15);color:#3b82f6;font-size:10px">✅ TP1</span> ';
      if (t.close_reason === 'tp2') badges += '<span class="pill" style="background:rgba(168,85,247,0.15);color:#a855f7;font-size:10px">🎯 TP2</span> ';
      if (t.close_reason === 'tp3') badges += '<span class="pill" style="background:rgba(249,115,22,0.15);color:#f97316;font-size:10px">🏆 TP3</span> ';
      return `
      <tr class="${statusOpen ? 'row-open' : ''}">
        <td>${fmtTime(t.opened_at)}</td>
        <td>${t.base || t.symbol}${t.symbol.startsWith('DEX:') ? ' <small style="color:#f97316">DEX</small>' : ''}</td>
        <td class="${t.side}">${t.side.toUpperCase()}</td>
        <td>${mktLabel}</td>
        <td>${levStr}</td>
        <td>${fmtPrice(t.entry)}</td>
        <td style="font-size:11px">
          ${statusOpen && livePrice ? '<b style="color:#fff">' + fmtPrice(livePrice) + '</b>' : (t.exit_price ? fmtPrice(t.exit_price) : '—')}
        </td>
        <td style="font-size:11px;color:#8e95ac">
          <span style="color:#10b981">TP1:${fmtPrice(t.take_profit)}</span><br>
          ${t.tp2 ? '<span style="color:#a855f7">TP2:' + fmtPrice(t.tp2) + '</span><br>' : ''}
          ${t.tp3 ? '<span style="color:#f97316">TP3:' + fmtPrice(t.tp3) + '</span>' : ''}
        </td>
        <td style="font-size:11px;color:#ef4444">${fmtPrice(t.stop_loss)}${riskFree ? '<br><span style="color:#10b981">→ ' + fmtPrice(t.entry) + '</span>' : ''}</td>
        <td>${t.closed_at ? fmtTime(t.closed_at) : '—'}</td>
        <td>${duration}</td>
        <td>${fmtN(t.size_usdt, 2)}</td>
        <td class="${pnlClass}"><b>${pnlStr}</b>${badges ? '<br>' + badges : ''}</td>
      </tr>`;
    }).join('');
  } catch (e) { console.error(e); }
}

/* ---------- UNIVERSE ---------- */
function renderUniverse() {
  const box = $('#universe_grid');
  if (!box) return;
  const u = stateCache.universe;
  if (!u || !Object.keys(u).length) {
    box.innerHTML = '<p style="color:#8e95ac">در حال بارگذاری واچ‌لیست...</p>';
    return;
  }
  let html = '';
  // CEX exchanges (e.g. {binance: [...], bybit: [...]})
  for (const ex in u) {
    if (ex === 'dex') continue;  // handled separately below
    const items = Array.isArray(u[ex]) ? u[ex] : [];
    html += items.slice(0, 60).map(s => `
      <div class="signal-card long">
        <div class="sym-row"><span class="sym">${s.symbol || s.base}</span><span class="exch">${ex}</span></div>
        <div class="rationale">
          ${s.listed_at ? '🆕 لیست در: ' + new Date(s.listed_at).toLocaleDateString('fa-IR') : '👤 عضو واچ‌لیست'}
        </div>
      </div>`).join('');
  }
  // DEX tokens
  const dexTokens = u.dex || [];
  if (dexTokens.length) {
    html += `<div class="dex-header">🔗 توکن‌های DEX — ${dexTokens.length} توکن فعال</div>`;
    html += dexTokens.slice(0, 80).map(t => {
      const ageMin = Math.floor((t.age_seconds || 0) / 60);
      const ageStr = ageMin < 1 ? 'تازه' : ageMin < 60 ? ageMin + ' دقیقه' : Math.floor(ageMin / 60) + ' ساعت';
      return `
        <div class="signal-card long ${t.is_brand_new ? 'new' : ''}">
          <div class="sym-row">
            <span class="sym">${t.symbol}</span>
            <span class="exch">${t.dex} <small>(${t.chain})</small></span>
          </div>
          <div class="pills">
            <span class="pill dex-pill">🔗 DEX</span>
            ${t.is_brand_new ? '<span class="pill new-pill">🆕 تازه</span>' : ''}
          </div>
          <div class="rationale">
            💲 ${fmtN(t.price_usd, 8)} &nbsp;|&nbsp; 💧 ${fmtN(t.liquidity_usd, 0)} &nbsp;|&nbsp; 📊 ${fmtN(t.volume_1h_usd, 0)}/h
          </div>
          <div class="rationale">
            ⏱️ ${ageStr} &nbsp;|&nbsp; ${t.mcap > 0 ? 'MC: $' + fmtN(t.mcap, 0) : ''}
            ${t.price_change_5m_pct !== 0 ? '&nbsp;|&nbsp; 5m: ' + (t.price_change_5m_pct > 0 ? '+' : '') + t.price_change_5m_pct.toFixed(1) + '%' : ''}
          </div>
          ${t.url ? '<div class="rationale"><a href="' + t.url + '" target="_blank" style="color:#38bdf8">🔗 مشاهده</a></div>' : ''}
        </div>`;
    }).join('');
  }
  box.innerHTML = html || '<p style="color:#8e95ac">واچ‌لیست خالی است</p>';
}

/* ---------- MEME HUNTER (شکارچی میم‌کوین) ---------- */
let hunterFilter = 'all';
let hunterCache = { summary: {}, hits: {} };

const hunterStrategyFa = {
  pre_pump: '💥 شکار قبل از پامپ',
  post_migration: '🚀 بعد از Migration',
  smart_money: '🐋 Smart Money',
  narrative: '📖 روایت‌ها',
  contract_safety: '🔒 امنیت قرارداد',
  whale_activity: '🐋 فعالیت نهنگ',
  liquidity_health: '💧 سلامت نقدینگی',
  holder_distribution: '👥 توزیع هولدرها',
  volume_profile: '📊 پروفایل حجم',
  momentum_breakout: '🚀 بریک‌اوت مومنتوم',
};
const hunterStrategyColor = {
  pre_pump: '#ef4444',
  post_migration: '#34d399',
  smart_money: '#8b5cf6',
  narrative: '#f97316',
  contract_safety: '#06b6d4',
  whale_activity: '#a855f7',
  liquidity_health: '#3b82f6',
  holder_distribution: '#10b981',
  volume_profile: '#eab308',
  momentum_breakout: '#f43f5e',
};

function setHunterFilter(f) {
  hunterFilter = f;
  document.querySelectorAll('.hunter-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  renderHunterResults();
}

/* ---------- SCORE FILTER (فیلتر امتیاز — درصد موفقیت) ---------- */
let scoreFilterMin = 0;
let scoreFilterMax = 1;

function setScoreFilter(min, max, btn) {
  scoreFilterMin = min;
  scoreFilterMax = max;
  document.querySelectorAll('.score-filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  loadSuccessRate();
}

function renderHunterHit(h) {
  const ageStr = h.detail?.age_display || durationFromSeconds(h.age_seconds);
  const strategyLabel = hunterStrategyFa[h.strategy] || h.strategy;
  const stratColor = hunterStrategyColor[h.strategy] || '#8e95ac';
  const scorePercent = Math.round(h.score * 100);
  const scoreClass = h.score >= 0.7 ? 'hunter-score-hot' : h.score >= 0.4 ? 'hunter-score-warm' : 'hunter-score-cool';
  const isBrandNew = h.is_brand_new || h.age_seconds < 600;
  const dexLabel = h.dex || '';
  const chainLabel = h.chain || '';

  // Signals — full text with icons
  const signals = (h.signals || []).map(s =>
    `<span class="pill hunter-signal-pill" style="border-color:${stratColor}">${s}</span>`
  ).join('');

  // Risk flags — red warnings
  const risks = (h.risk_flags || []).map(r =>
    `<span class="pill hunter-risk-pill">${r}</span>`
  ).join('');

  // Price changes
  const pc5m = h.price_change_5m_pct;
  const pc1h = h.price_change_1h_pct;
  const pc5mStr = pc5m != null ? (pc5m >= 0 ? '+' : '') + pc5m.toFixed(1) + '%' : '';
  const pc1hStr = pc1h != null ? (pc1h >= 0 ? '+' : '') + pc1h.toFixed(1) + '%' : '';

  const fmtUSD = (v) => {
    if (!v || v === 0) return '$0';
    if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
    if (v >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K';
    return '$' + fmtN(v, 0);
  };

  return `
    <div class="signal-card long hunter-card ${isBrandNew ? 'new' : ''}" style="border-left: 4px solid ${stratColor}">
      <div class="sym-row">
        <span class="sym">${h.symbol}</span>
        <span class="exch">${dexLabel} <small>(${chainLabel})</small></span>
      </div>

      <!-- Strategy badge + tags -->
      <div class="pills">
        <span class="pill hunter-strat-pill" style="background:${stratColor}22;color:${stratColor};border:1px solid ${stratColor}44">${strategyLabel}</span>
        ${isBrandNew ? '<span class="pill new-pill">🆕 تازه</span>' : ''}
      </div>

      <!-- Score bar -->
      <div class="hunter-score-row">
        <div class="scorebar"><div class="${scoreClass}" style="width:${scorePercent}%"></div></div>
        <span class="hunter-score-val ${scoreClass}">${scorePercent}%</span>
      </div>

      <!-- Market data -->
      <div class="levels">
        <div class="level">قیمت<b>${fmtPrice(h.price_usd)}</b></div>
        <div class="level">نقدینگی<b>${fmtUSD(h.liquidity_usd)}</b></div>
        <div class="level">حجم ۱س<b>${fmtUSD(h.volume_1h_usd)}</b></div>
      </div>
      <div class="levels" style="margin-top:4px">
        <div class="level">سن<b>${ageStr}</b></div>
        <div class="level">MC<b>${fmtUSD(h.mcap)}</b></div>
        <div class="level">تراکنش<b>${h.txns_24h || 0}</b></div>
      </div>

      <!-- Price changes -->
      <div class="hunter-price-changes">
        ${pc5mStr ? '<span class="' + (pc5m >= 0 ? 'pos' : 'neg') + '">۵ دقیقه: ' + pc5mStr + '</span>' : ''}
        ${pc1hStr ? '<span class="' + (pc1h >= 0 ? 'pos' : 'neg') + '">۱ ساعت: ' + pc1hStr + '</span>' : ''}
      </div>

      <!-- Detection signals -->
      ${signals ? '<div class="hunter-signals-area">' + signals + '</div>' : ''}

      <!-- Risk warnings -->
      ${risks ? '<div class="hunter-risks-area">' + risks + '</div>' : ''}

      <!-- Link -->
      ${h.url ? '<div class="rationale"><a href="' + h.url + '" target="_blank" style="color:#38bdf8">🔗 مشاهده در DexScreener</a></div>' : ''}
    </div>
  `;
}

function renderHunterSummary(summary) {
  const box = $('#hunter_summary');
  if (!box || !summary) return;
  const total = summary.total_unique || 0;
  const byStrat = summary.by_strategy || {};
  const avgScore = summary.avg_score || 0;
  const topTokens = summary.top_tokens || [];
  box.innerHTML = `
    <div class="hunter-summary-cards">
      <div class="card stat-card hunter-stat">
        <span class="card-label">🎯 کل فرصت‌ها</span>
        <span class="card-value">${total}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">💥 قبل از پامپ</span>
        <span class="card-value" style="color:${hunterStrategyColor.pre_pump}">${byStrat.pre_pump || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">🚀 بعد از Migration</span>
        <span class="card-value" style="color:${hunterStrategyColor.post_migration}">${byStrat.post_migration || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">📖 روایت‌ها</span>
        <span class="card-value" style="color:${hunterStrategyColor.narrative}">${byStrat.narrative || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">🔒 امنیت قرارداد</span>
        <span class="card-value" style="color:${hunterStrategyColor.contract_safety}">${byStrat.contract_safety || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">🐋 فعالیت نهنگ</span>
        <span class="card-value" style="color:${hunterStrategyColor.whale_activity}">${byStrat.whale_activity || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">💧 سلامت نقدینگی</span>
        <span class="card-value" style="color:${hunterStrategyColor.liquidity_health}">${byStrat.liquidity_health || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">👥 توزیع هولدرها</span>
        <span class="card-value" style="color:${hunterStrategyColor.holder_distribution}">${byStrat.holder_distribution || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">📊 پروفایل حجم</span>
        <span class="card-value" style="color:${hunterStrategyColor.volume_profile}">${byStrat.volume_profile || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">🚀 بریک‌اوت</span>
        <span class="card-value" style="color:${hunterStrategyColor.momentum_breakout}">${byStrat.momentum_breakout || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">میانگین امتیاز</span>
        <span class="card-value">${Math.round(avgScore * 100)}%</span>
      </div>
    </div>
    ${topTokens.length ? `
    <div class="hunter-top-tokens">
      <span class="hunter-top-label">🏆 برترین‌ها:</span>
      ${topTokens.slice(0, 6).map(t => `<span class="pill hunter-top-pill" style="border-color:${hunterStrategyColor[t.strategy] || '#8e95ac'}">${t.symbol} (${Math.round(t.score * 100)}%)</span>`).join('')}
    </div>` : ''}
  `;
}

function renderDailyPicks(picks) {
  const box = $('#daily_picks');
  if (!box) return;
  if (!picks || !picks.length) {
    box.innerHTML = '<p style="color:#8e95ac;font-size:12px;padding:8px">هنوز توکن با احتمال بالا شناسایی نشده</p>';
    return;
  }
  box.innerHTML = picks.map(p => {
    const scorePercent = Math.round(p.score * 100);
    const ageStr = durationFromSeconds(p.age_seconds);
    const chainLabel = p.chain || '';
    const dexLabel = p.dex || '';
    const strats = (p.strategies || []).map(s => hunterStrategyFa[s] || s);
    const signals = (p.signals || []).slice(0, 3);
    return `
      <div class="daily-pick-card">
        <div class="daily-pick-header">
          <span class="daily-pick-symbol">${p.symbol}</span>
          <span class="daily-pick-score">${scorePercent}%</span>
        </div>
        <div class="daily-pick-meta">
          <span>${chainLabel}</span>
          <span>${dexLabel}</span>
          <span>قیمت: ${fmtPrice(p.price_usd)}</span>
          <span>سن: ${ageStr}</span>
          <span>MC: ${fmtPrice(p.mcap)}</span>
          <span>نقدینگی: ${fmtPrice(p.liquidity_usd)}</span>
        </div>
        <div class="daily-pick-strats">
          ${strats.map(s => `<span class="daily-pick-strat-tag">${s}</span>`).join('')}
        </div>
        ${signals.length ? `<div class="daily-pick-signals">${signals.join(' • ')}</div>` : ''}
      </div>
    `;
  }).join('');
}

function renderHunterResults() {
  const box = $('#hunter_results');
  if (!box) return;
  const hits = hunterCache.hits || {};
  let allHits = [];
  // Flatten all strategy hits
  for (const strat in hits) {
    if (Array.isArray(hits[strat])) {
      allHits = allHits.concat(hits[strat]);
    }
  }
  // Apply filter
  if (hunterFilter !== 'all') {
    allHits = allHits.filter(h => h.strategy === hunterFilter);
  }
  // Sort by score
  allHits.sort((a, b) => (b.score || 0) - (a.score || 0));
  $('#hunter_total').textContent = allHits.length;
  if (!allHits.length) {
    box.innerHTML = '<p style="color:#8e95ac">🔍 در حال اسکن... نتایج به‌زودی نمایش داده می‌شوند.</p>';
    return;
  }
  box.innerHTML = allHits.map(renderHunterHit).join('');

  // Update per-strategy lists
  for (const strat of ['pre_pump', 'post_migration', 'narrative', 'contract_safety', 'whale_activity', 'liquidity_health', 'holder_distribution', 'volume_profile', 'momentum_breakout']) {
    const list = $(`#hunter_${strat}_list`);
    if (!list) continue;
    const stratHits = hits[strat] || [];
    if (stratHits.length) {
      list.innerHTML = stratHits.slice(0, 10).map(h => `
        <div class="hunter-mini-token">
          <span class="hunter-mini-sym">${h.symbol}</span>
          <span class="hunter-mini-score" style="color:${hunterStrategyColor[strat]}">${Math.round(h.score * 100)}%</span>
          <span class="hunter-mini-price">${fmtN(h.price_usd, 8)}</span>
          ${h.url ? '<a href="' + h.url + '" target="_blank" style="color:#38bdf8;font-size:11px">🔗</a>' : ''}
        </div>
      `).join('');
    } else {
      list.innerHTML = '<p style="color:#8e95ac;font-size:12px;padding:8px">هنوز فرصتی شناسایی نشده</p>';
    }
  }
}

async function loadHunterResults() {
  try {
    const data = await json('/api/hunter/results');
    hunterCache.summary = data.summary || {};
    hunterCache.hits = data.hits || {};
    renderHunterSummary(hunterCache.summary);
    renderHunterResults();
  } catch (e) {
    console.error('loadHunterResults:', e);
  }
  // Load daily picks from summary endpoint
  try {
    const sumData = await json('/api/hunter/summary');
    renderDailyPicks(sumData.daily_picks || []);
  } catch (e) {
    console.error('loadDailyPicks:', e);
  }
}

function durationFromSeconds(sec) {
  if (!sec || sec <= 0) return '—';
  if (sec < 60) return Math.round(sec) + ' ثانیه';
  if (sec < 3600) return Math.round(sec / 60) + ' دقیقه';
  if (sec < 86400) return Math.round(sec / 3600) + ' ساعت';
  return Math.round(sec / 86400) + ' روز';
}

/* ---------- HUNTER API TEST ---------- */
async function testHunterAPIs() {
  toast('در حال تست API ها...');
  const box = $('#hunter_results');
  if (box) box.innerHTML = '<p style="color:#f97316;text-align:center;padding:20px">⏳ در حال تست اتصال API ها...</p>';
  try {
    const r = await fetch('/api/hunter/test', { method: 'POST', headers: {'Content-Type': 'application/json'} });
    const data = await r.json();
    const tests = data.tests || [];
    let html = '<div style="padding:16px">';
    html += '<h3 style="color:#f97316;margin-bottom:12px">🔍 نتایج تست API</h3>';
    for (const t of tests) {
      const statusIcon = t.status === 'ok' ? '✅' : t.status === 'empty' ? '⚠️' : '❌';
      const statusColor = t.status === 'ok' ? 'var(--success)' : t.status === 'empty' ? 'var(--warning)' : 'var(--danger)';
      html += `<div class="signal-card long" style="margin-bottom:8px;border-left:4px solid ${statusColor}">
        <div class="sym-row">
          <span class="sym" style="font-size:14px">${statusIcon} ${t.name}</span>
          <span class="exch" style="color:${statusColor}">${t.status}</span>
        </div>
        ${t.count !== undefined ? '<div style="font-size:12px;color:var(--text-dim);margin-top:4px">تعداد: ' + t.count + '</div>' : ''}
        ${t.error ? '<div style="font-size:12px;color:var(--danger);margin-top:4px">خطا: ' + t.error + '</div>' : ''}
        ${t.sample ? '<div style="font-size:11px;color:var(--text-dim);margin-top:4px">نمونه: ' + (t.sample.symbol || '') + ' $' + (t.sample.price_usd || 0) + ' liq=$' + (t.sample.liquidity_usd || 0) + '</div>' : ''}
      </div>`;
    }
    html += '</div>';
    if (box) box.innerHTML = html;
    toast('تست API تمام شد');
  } catch (e) {
    if (box) box.innerHTML = '<p style="color:var(--danger);text-align:center;padding:20px">❌ خطا در تست: ' + e.message + '</p>';
    toast('خطا در تست API');
  }
}

/* ---------- SUCCESS RATE (درصد موفقیت) ---------- */
const stratFa = {
  pre_pump: '💥 شکار قبل از پامپ', post_migration: '🚀 بعد از Migration',
  smart_money: '🐋 Smart Money', narrative: '📖 روایت‌ها',
  contract_safety: '🔒 امنیت قرارداد', whale_activity: '🐋 فعالیت نهنگ',
  liquidity_health: '💧 سلامت نقدینگی', holder_distribution: '👥 توزیع هولدرها',
  volume_profile: '📊 پروفایل حجم', momentum_breakout: '🚀 بریک‌اوت مومنتوم',
  all: '🎯 همه',
};
const stratColor = {
  pre_pump: '#ef4444', post_migration: '#34d399',
  smart_money: '#8b5cf6', narrative: '#f97316',
  contract_safety: '#06b6d4', whale_activity: '#a855f7',
  liquidity_health: '#3b82f6', holder_distribution: '#10b981',
  volume_profile: '#eab308', momentum_breakout: '#f43f5e',
  all: '#f97316',
};

function renderSuccessOverall(data) {
  const box = $('#success_overall');
  if (!box) return;
  const o = data.overall || {};
  const total = o.total || o.total_detections || 0;
  const checked = o.checked || 0;
  const winRate = o.win_rate || 0;
  const avgRoi = o.avg_roi_pct || o.avg_peak_roi_pct || 0;
  const winners = o.winners || 0;
  const losers = o.losers || 0;
  box.innerHTML = `
    <div class="card stat-card hunter-stat">
      <span class="card-label">🎯 کل شناسایی‌ها</span>
      <span class="card-value">${total}</span>
    </div>
    <div class="card stat-card hunter-stat">
      <span class="card-label">✅ بررسی‌شده</span>
      <span class="card-value">${checked}</span>
    </div>
    <div class="card stat-card hunter-stat">
      <span class="card-label">🏆 درصد موفقیت</span>
      <span class="card-value" style="color:${winRate >= 50 ? 'var(--success)' : winRate >= 30 ? 'var(--accent)' : 'var(--danger)'}">${winRate.toFixed(1)}%</span>
    </div>
    <div class="card stat-card hunter-stat">
      <span class="card-label">📈 برد (+20%)</span>
      <span class="card-value" style="color:var(--success)">${winners}</span>
    </div>
    <div class="card stat-card hunter-stat">
      <span class="card-label">📉 باخت</span>
      <span class="card-value" style="color:var(--danger)">${losers}</span>
    </div>
    <div class="card stat-card hunter-stat">
      <span class="card-label">💰 میانگین ROI</span>
      <span class="card-value" style="color:${avgRoi >= 0 ? 'var(--success)' : 'var(--danger)'}">${avgRoi >= 0 ? '+' : ''}${avgRoi.toFixed(1)}%</span>
    </div>
  `;
}

function renderSuccessByStrategy(data) {
  const box = $('#success_by_strategy');
  if (!box) return;
  const byStrat = data.by_strategy || {};
  let html = '';
  for (const strat of ['pre_pump', 'post_migration', 'smart_money', 'narrative']) {
    const s = byStrat[strat] || {};
    const wr = s.win_rate || 0;
    const total = s.total_detections || 0;
    const checked = s.checked || 0;
    const avgPeak = s.avg_peak_roi_pct || 0;
    const roi5m = s.avg_roi_5m || 0;
    const roi30m = s.avg_roi_30m || 0;
    const roi1h = s.avg_roi_1h || 0;
    const roi4h = s.avg_roi_4h || 0;
    const best = s.best_detection_symbol || '—';
    const bestRoi = s.best_detection_roi || 0;
    const worst = s.worst_detection_symbol || '—';
    const worstRoi = s.worst_detection_roi || 0;
    const color = stratColor[strat] || '#8e95ac';
    const wrColor = wr >= 50 ? 'var(--success)' : wr >= 30 ? 'var(--accent)' : 'var(--danger)';
    html += `
      <div class="success-strat-card" style="border-left: 4px solid ${color}">
        <h3 style="color:${color}">${stratFa[strat] || strat}</h3>
        <div class="success-metrics">
          <div class="success-metric">
            <span class="sm-label">شناسایی</span>
            <span class="sm-value">${total}</span>
          </div>
          <div class="success-metric">
            <span class="sm-label">بررسی‌شده</span>
            <span class="sm-value">${checked}</span>
          </div>
          <div class="success-metric">
            <span class="sm-label">Win Rate</span>
            <span class="sm-value" style="color:${wrColor}">${wr.toFixed(1)}%</span>
          </div>
          <div class="success-metric">
            <span class="sm-label">میانگین ROI</span>
            <span class="sm-value" style="color:${avgPeak >= 0 ? 'var(--success)' : 'var(--danger)'}">${avgPeak >= 0 ? '+' : ''}${avgPeak.toFixed(1)}%</span>
          </div>
        </div>
        <div class="success-roi-grid">
          <div class="success-roi-item"><span>۵ دقیقه</span><span style="color:${roi5m >= 0 ? 'var(--success)' : 'var(--danger)'}">${roi5m >= 0 ? '+' : ''}${roi5m.toFixed(1)}%</span></div>
          <div class="success-roi-item"><span>۳۰ دقیقه</span><span style="color:${roi30m >= 0 ? 'var(--success)' : 'var(--danger)'}">${roi30m >= 0 ? '+' : ''}${roi30m.toFixed(1)}%</span></div>
          <div class="success-roi-item"><span>۱ ساعت</span><span style="color:${roi1h >= 0 ? 'var(--success)' : 'var(--danger)'}">${roi1h >= 0 ? '+' : ''}${roi1h.toFixed(1)}%</span></div>
          <div class="success-roi-item"><span>۴ ساعت</span><span style="color:${roi4h >= 0 ? 'var(--success)' : 'var(--danger)'}">${roi4h >= 0 ? '+' : ''}${roi4h.toFixed(1)}%</span></div>
        </div>
        ${best !== '—' ? `<div class="success-record"><span class="pos">🏆 بهترین: ${best} (+${bestRoi.toFixed(0)}%)</span></div>` : ''}
        ${worst !== '—' ? `<div class="success-record"><span class="neg">💀 بدترین: ${worst} (${worstRoi.toFixed(0)}%)</span></div>` : ''}
      </div>
    `;
  }
  box.innerHTML = html || '<p style="color:#8e95ac">هنوز داده‌ای ثبت نشده. شکارچی بعد از شناسایی اولین توکن‌ها شروع به ردیابی می‌کند.</p>';
}

function renderSuccessDetections(detections) {
  const tb = $('#success_detections');
  if (!tb) return;
  if (!detections || !detections.length) {
    tb.innerHTML = '<tr><td colspan="10" style="color:#8e95ac;text-align:center">هنوز شناسایی ثبت نشده</td></tr>';
    return;
  }
  tb.innerHTML = detections.slice(0, 50).map(d => {
    const roi = (d.peak_roi_pct != null) ? d.peak_roi_pct : null;
    const roiStr = roi != null ? (roi >= 0 ? '+' : '') + roi.toFixed(1) + '%' : '—';
    const roiClass = roi != null ? (roi >= 20 ? 'pos' : roi >= 0 ? '' : 'neg') : '';
    const result = d.is_winner ? '🏆 برد' : d.is_rugpull ? '💀 راگ‌پول' : d.checked_at ? '⏰ بررسی‌شده' : '⏳ در انتظار';
    const resultClass = d.is_winner ? 'pos' : d.is_rugpull ? 'neg' : '';
    return `
      <tr>
        <td>${fmtTime(d.detected_at)}</td>
        <td><b>${d.symbol}</b> <small style="color:#8e95ac">${d.dex}</small></td>
        <td style="color:${stratColor[d.strategy] || '#8e95ac'}">${stratFa[d.strategy] || d.strategy}</td>
        <td>${(d.score * 100).toFixed(0)}%</td>
        <td>${fmtPrice(d.price_at_detection)}</td>
        <td>${fmtPrice(d.price_5m)}</td>
        <td>${fmtPrice(d.price_30m)}</td>
        <td>${fmtPrice(d.price_1h)}</td>
        <td class="${roiClass}"><b>${roiStr}</b></td>
        <td class="${resultClass}">${result}</td>
      </tr>
    `;
  }).join('');
}

async function loadSuccessRate() {
  try {
    const data = await json(`/api/hunter/success?min_score=${scoreFilterMin}&max_score=${scoreFilterMax}`);
    renderSuccessOverall(data);
    renderSuccessByStrategy(data);
  } catch (e) { console.error('loadSuccessRate:', e); }
  try {
    const { detections } = await json('/api/hunter/recent?limit=50');
    renderSuccessDetections(detections);
  } catch (e) { console.error('loadRecentDetections:', e); }
}

/* ---------- CHART (TradingView lightweight-charts) ---------- */
let lwcChart = null;
let candleSeries = null;
let volumeSeries = null;
let chartLiveInterval = null;
let signalMarkers = [];       // cached marker data for re-rendering
let signalLines = [];         // price lines on chart
let currentAnalysisSignalId = null;  // currently shown signal id
let chartPricePrecision = 6;  // dynamic precision for current chart

function calcPricePrecision(prices) {
  if (!prices || !prices.length) return 6;
  const min = Math.min(...prices);
  if (min >= 1000) return 2;
  if (min >= 1) return 4;
  if (min >= 0.01) return 6;
  if (min >= 0.0001) return 8;
  return 10;
}

function initChart(precision) {
  const container = document.getElementById('price_chart');
  if (!container) return;
  if (lwcChart) { lwcChart.remove(); lwcChart = null; }
  container.innerHTML = '';
  const p = precision || chartPricePrecision;
  lwcChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 420,
    layout: { background: { color: '#141822' }, textColor: '#e6e9f2' },
    grid: { vertLines: { color: '#1e2336' }, horzLines: { color: '#1e2336' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#2a2f42' },
    rightPriceScale: { borderColor: '#2a2f42' },
  });
  candleSeries = lwcChart.addCandlestickSeries({
    upColor: '#34d399', downColor: '#ef4444', borderUpColor: '#34d399',
    borderDownColor: '#ef4444', wickUpColor: '#34d399', wickDownColor: '#ef4444',
    priceFormat: { type: 'price', minMove: Math.pow(10, -p), precision: p },
  });
  volumeSeries = lwcChart.addHistogramSeries({
    color: '#38bdf8', priceFormat: { type: 'volume' },
    priceScaleId: '', scaleMargins: { top: 0.85, bottom: 0 },
  });
  new ResizeObserver(() => {
    if (lwcChart) lwcChart.applyOptions({ width: container.clientWidth });
  }).observe(container);
}

async function loadChart(exchange, symbol, tf) {
  // If DEX token — skip REST fetch, show only markers
  if (exchange === 'dex' || symbol.startsWith('DEX:')) {
    toast('سیگنال DEX — داده نمودار کندلی محدود است');
    return;
  }
  // Encode / → _ for FastAPI route compatibility
  const encodedSymbol = (symbol || '').replace(/\//g, '_');
  try {
    const { candles } = await json(`/api/chart/${exchange}/${encodedSymbol}/${tf}?limit=300`);
    if (!candles || !candles.length) { toast('داده‌ای برای این نماد نیست'); return; }

    // Auto-detect price precision from data
    const allPrices = candles.flatMap(c => [c.open, c.high, c.low, c.close]);
    chartPricePrecision = calcPricePrecision(allPrices);

    // Re-initialize chart — old lines get destroyed with old chart anyway
    initChart(chartPricePrecision);
    if (!candleSeries || !lwcChart) { toast('خطا در راه‌اندازی نمودار'); return; }

    const ohlc = candles.map(c => ({
      time: Math.floor(c.timestamp / 1000),
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    const vol = candles.map(c => ({
      time: Math.floor(c.timestamp / 1000),
      value: c.volume,
      color: c.close >= c.open ? 'rgba(52,211,153,0.4)' : 'rgba(239,68,68,0.4)',
    }));
    candleSeries.setData(ohlc);
    volumeSeries.setData(vol);
    lwcChart.timeScale().fitContent();

    // Reapply signal markers if any were cached
    if (signalMarkers.length > 0) {
      reapplySignalMarkers();
    }
    toast(`نمودار ${symbol} بارگذاری شد`);
  } catch (e) {
    console.error('loadChart error:', e);
    toast('خطا در بارگذاری نمودار — ' + (e.message || 'خطای ناشناخته'));
  }
}

function reapplySignalMarkers() {
  // No need to remove old lines — initChart() already destroys them
  signalLines = [];
  // Re-add from cached marker data
  for (const m of signalMarkers) {
    const line = candleSeries.createPriceLine({
      price: m.price, color: m.color, lineWidth: 2,
      lineStyle: m.dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true, title: m.label,
    });
    signalLines.push(line);
  }
}

/* ---------- SIGNAL ANALYSIS ON CHART ---------- */
async function showSignalOnChart(signalId) {
  // Switch to chart tab
  $$('.tab-btn').forEach(b => b.classList.remove('active'));
  $$('.tab-panel').forEach(p => p.classList.remove('active'));
  $('.tab-btn[data-tab="chart"]').classList.add('active');
  $('#tab-chart').classList.add('active');

  // Stop live chart if running
  if (chartLiveInterval) {
    clearInterval(chartLiveInterval);
    chartLiveInterval = null;
    $('#chart_live').textContent = '🔴 زنده';
    $('#chart_live').classList.remove('active');
  }

  toast('در حال بارگذاری تحلیل سیگنال...');
  try {
    const r = await fetch(`/api/signal/${signalId}/chart`);
    if (!r.ok) { toast('سیگنال پیدا نشد'); return; }
    const data = await r.json();
    const sig = data.signal;
    const markers = data.markers;
    const explanations = data.strategy_explanations || [];
    const candlesByTf = data.candles_by_tf || {};

    currentAnalysisSignalId = signalId;

    // Determine chart params
    const isDex = sig.symbol.startsWith('DEX:');
    const exchange = sig.exchange || 'binance';
    // Resolve symbol: DEX keeps as-is, CEX converts _ to / for display
    const rawSymbol = isDex ? sig.symbol : (sig.symbol.includes('/') ? sig.symbol : sig.symbol.replace('_', '/'));
    const chartTf = '5m';

    // Set chart controls — just update exchange; symbol updated below
    if ($('#chart_exchange')) $('#chart_exchange').value = exchange;
    if ($('#chart_symbol')) $('#chart_symbol').value = rawSymbol;
    if ($('#chart_tf')) $('#chart_tf').value = chartTf;

    // Load chart with candle data
    let candles;
    if (isDex) {
      toast('سیگنال DEX — داده نمودار کندلی محدود است. مارکرها نمایش داده شد.');
      candles = [];
    } else {
      candles = candlesByTf[chartTf] || [];
    }

    if (candles.length > 0) {
      initChart();
      const ohlc = candles.map(c => ({
        time: Math.floor(c.timestamp / 1000),
        open: c.open, high: c.high, low: c.low, close: c.close,
      }));
      const vol = candles.map(c => ({
        time: Math.floor(c.timestamp / 1000),
        value: c.volume,
        color: c.close >= c.open ? 'rgba(52,211,153,0.4)' : 'rgba(239,68,68,0.4)',
      }));
      candleSeries.setData(ohlc);
      volumeSeries.setData(vol);
      lwcChart.timeScale().fitContent();
    } else {
      initChart();
    }

    // initChart already destroyed old chart — just reset caches
    signalMarkers = [];

    // Add entry line
    const entryPrice = markers.entry || sig.entry;
    if (entryPrice && entryPrice > 0) {
      const line = candleSeries.createPriceLine({
        price: entryPrice, color: '#38bdf8', lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true, title: '🔵 ورود',
      });
      signalLines.push(line);
      signalMarkers.push({ price: entryPrice, color: '#38bdf8', dashed: false, label: '🔵 ورود' });
    }
    // Add SL line
    const slPrice = markers.stop_loss || sig.stop_loss;
    if (slPrice && slPrice > 0) {
      const line = candleSeries.createPriceLine({
        price: slPrice, color: '#ff5a6e', lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: '❌ SL',
      });
      signalLines.push(line);
      signalMarkers.push({ price: slPrice, color: '#ff5a6e', dashed: true, label: '❌ SL' });
    }
    // Add TP line
    const tpPrice = markers.take_profit || sig.take_profit;
    if (tpPrice && tpPrice > 0) {
      const line = candleSeries.createPriceLine({
        price: tpPrice, color: '#34d399', lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: '✅ TP',
      });
      signalLines.push(line);
      signalMarkers.push({ price: tpPrice, color: '#34d399', dashed: true, label: '✅ TP' });
    }
    // Add exit-price line — where the trade ACTUALLY closed, if resolved
    if (markers.exit_price && markers.exit_price > 0) {
      const isWin = (markers.pnl_pct || 0) >= 0;
      const exitColor = isWin ? '#22c55e' : '#ef4444';
      const line = candleSeries.createPriceLine({
        price: markers.exit_price, color: exitColor, lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true, title: '🏁 خروج',
      });
      signalLines.push(line);
      signalMarkers.push({ price: markers.exit_price, color: exitColor, dashed: true, label: '🏁 خروج' });
    }

    // ─── Entry / exit MARKERS on the timeline (arrow at entry candle, circle
    // at exit candle) — price lines above only show WHERE the level is, not
    // WHEN entry/exit happened. Mirrors showScalpTradeOnChart's pattern. ───
    const side = sig.side;
    const entryTime = Math.floor(markers.entry_time || markers.created_at || sig.created_at || 0);
    const chartMarkers = [];
    if (entryTime > 0) {
      chartMarkers.push({
        time: entryTime, position: side === 'long' ? 'belowBar' : 'aboveBar',
        color: side === 'long' ? '#22c55e' : '#ef4444',
        shape: side === 'long' ? 'arrowUp' : 'arrowDown',
        text: `🕐 ورود (${side === 'long' ? 'LONG' : 'SHORT'})`,
      });
    }
    const statusFa3 = { tp: 'TP', tp1: 'TP1', tp2: 'TP2', tp3: 'TP3', sl: 'SL', sl_risk_free: 'SL (ریسک‌فری)', timeout: 'تایم‌اوت' };
    const sigStatus = sig.status || 'open';
    // Prefer the REAL exit time/price from the linked paper trade (now
    // returned by the backend) — only fall back to approximating with the
    // last candle's time if no real trade record exists for some reason.
    if (markers.exit_time) {
      const exitTime = Math.floor(markers.exit_time);
      if (exitTime > entryTime) {
        const isWin = (markers.pnl_pct || 0) >= 0;
        chartMarkers.push({
          time: exitTime, position: side === 'long' ? 'aboveBar' : 'belowBar',
          color: isWin ? '#22c55e' : '#ef4444', shape: 'circle',
          text: `🏁 خروج (${statusFa3[markers.close_reason] || markers.close_reason || ''}${markers.pnl_pct != null ? ' ' + (markers.pnl_pct >= 0 ? '+' : '') + markers.pnl_pct.toFixed(2) + '%' : ''})`,
        });
      }
    } else if (['tp', 'tp1', 'tp2', 'tp3', 'sl', 'sl_risk_free', 'timeout'].includes(sigStatus) && candles.length) {
      const lastCandleTime = Math.floor(candles[candles.length - 1].timestamp / 1000);
      if (lastCandleTime > entryTime) {
        const isWin = sigStatus.startsWith('tp');
        chartMarkers.push({
          time: lastCandleTime, position: side === 'long' ? 'aboveBar' : 'belowBar',
          color: isWin ? '#22c55e' : '#ef4444', shape: 'circle',
          text: `🏁 خروج (${statusFa3[sigStatus] || sigStatus})`,
        });
      }
    }
    chartMarkers.sort((a, b) => a.time - b.time);
    try { candleSeries.setMarkers(chartMarkers); } catch (e) { console.warn('setMarkers error:', e); }

    // Show analysis panel + overlay on chart
    renderSignalAnalysis(sig, explanations, markers);
    renderChartOverlay(sig, explanations, markers);

  } catch (e) {
    toast('خطا در بارگذاری تحلیل سیگنال');
    console.error('showSignalOnChart error:', e);
  }
}

function renderSignalAnalysis(sig, explanations, markers) {
  const panel = $('#signal_analysis_panel');
  const content = $('#sa_content');
  if (!panel || !content) return;
  panel.classList.remove('hidden');

  const sideFa = sig.side === 'long' ? '🟢 خرید (LONG)' : '🔴 فروش (SHORT)';
  const isWinner = sig.status === 'tp';
  const isLoser = sig.status === 'sl';
  const statusFa = { open: '🟢 فعال', tp: '✅ سود', sl: '❌ ضرر', trailing: '🔄 تریلینگ', closed: '⏰ بسته', no_position: '⏭️ رد شد (ظرفیت پر)' };
  const statusColor = isWinner ? 'var(--success)' : isLoser ? 'var(--danger)' : 'var(--accent)';

  // TF breakdown bars
  const tfBreakdown = sig.tf_breakdown || {};
  const tfBars = Object.entries(tfBreakdown)
    .filter(([, v]) => v > 0)
    .map(([tf, v]) => `
      <div class="sa-tf-bar">
        <span class="sa-tf-label">${tf}</span>
        <div class="sa-tf-track"><div class="sa-tf-fill" style="width:${Math.round(v * 100)}%"></div></div>
        <span class="sa-tf-val">${v.toFixed(2)}</span>
      </div>
    `).join('');

  // Strategy hit cards
  const hitCards = explanations.map(h => `
    <div class="sa-hit-card">
      <div class="sa-hit-header">
        <span class="sa-hit-name">${h.name_fa}</span>
        <span class="sa-hit-tf">⏱️ ${h.timeframe}</span>
        <span class="sa-hit-score" style="color:${h.score >= 0.6 ? 'var(--success)' : 'var(--accent)'}">${(h.score * 100).toFixed(0)}%</span>
      </div>
      <div class="sa-hit-desc">${h.description || 'بدون توضیح'}</div>
      <div class="sa-hit-detail">
        <span>${h.side_fa}</span>
        ${h.detail ? '<span style="color:var(--text-dim)">📊 ' + h.detail + '</span>' : ''}
        <span style="color:var(--text-dim)">وزن: ${h.weight.toFixed(1)}</span>
      </div>
    </div>
  `).join('');

  // Risk/reward calculation
  const rr = markers.atr > 0
    ? (Math.abs(markers.take_profit - markers.entry) / Math.abs(markers.entry - markers.stop_loss)).toFixed(2)
    : '—';

  const displayName = sig.base || (sig.symbol.startsWith('DEX:') ? sig.symbol.split(':').pop().slice(0, 8) : sig.symbol);

  content.innerHTML = `
    <div class="sa-main-info">
      <div class="sa-info-row">
        <span class="sa-label">نماد</span>
        <span class="sa-value">${displayName}</span>
      </div>
      <div class="sa-info-row">
        <span class="sa-label">جهت</span>
        <span class="sa-value" style="color:${sig.side === 'long' ? 'var(--success)' : 'var(--danger)'}">${sideFa}</span>
      </div>
      <div class="sa-info-row">
        <span class="sa-label">امتیاز</span>
        <span class="sa-value" style="color:${sig.score >= 0.75 ? 'var(--success)' : 'var(--accent)'}">${(sig.score * 100).toFixed(0)}%</span>
      </div>
      <div class="sa-info-row">
        <span class="sa-label">وضعیت</span>
        <span class="sa-value" style="color:${statusColor}">${statusFa[sig.status] || sig.status}</span>
      </div>
    </div>

    <div class="sa-levels">
      <div class="sa-level sa-entry"><span>🔵 ورود</span><b>${fmtPrice(markers.entry)}</b></div>
      <div class="sa-level sa-tp"><span>✅ حد سود</span><b>${fmtPrice(markers.take_profit)}</b></div>
      <div class="sa-level sa-sl"><span>❌ حد ضرر</span><b>${fmtPrice(markers.stop_loss)}</b></div>
      <div class="sa-level"><span>📊 ATR</span><b>${fmtPrice(markers.atr)}</b></div>
      <div class="sa-level"><span>⚖️ R/R</span><b>${rr}</b></div>
    </div>

    ${tfBars ? '<div class="sa-section-title">📊 امتیاز به تفکیک تایم‌فریم</div><div class="sa-tf-breakdown">' + tfBars + '</div>' : ''}

    <div class="sa-section-title">🔍 استراتژی‌های فعال‌شده (${explanations.length} تا)</div>
    <div class="sa-hits-grid">${hitCards || '<p style="color:#8e95ac">هیچ استراتژی فعال نیست</p>'}</div>

    ${sig.rationale ? '<div class="sa-section-title">📝 توضیح کلی</div><div class="sa-rationale">' + sig.rationale + '</div>' : ''}
  `;
}

function closeSignalAnalysis() {
  const panel = $('#signal_analysis_panel');
  if (panel) panel.classList.add('hidden');
  const overlay = $('#chart_signal_overlay');
  if (overlay) overlay.classList.add('hidden');
  signalMarkers = [];
  currentAnalysisSignalId = null;
}

/* ---------- CHART SIGNAL OVERLAY — دلیل سیگنال روی چارت ---------- */
const strategyIcons = {
  'new_listing': '🆕', 'volume_spike': '🔊', 'orderbook_imbalance': '⚖️',
  'liquidity_grab': '🌊', 'momentum_ignition': '🔥', 'rsi_divergence': '📊',
  'bb_breakout': '🎯', 'funding_oi_spike': '💸', 'social_momentum': '📱',
  'ema_cross': '📈', 'adx_trend': '📊', 'squeeze_momentum': '💎',
  'vwap': '📐', 'macd_crossover': '📊', 'stoch_rsi': '📊',
  'obv_divergence': '📊', 'sr_bounce': '🎯', 'volume_trend': '📊',
};

function renderChartOverlay(sig, explanations, markers) {
  const overlay = $('#chart_signal_overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');

  const sideFa = sig.side === 'long' ? '🟢 خرید' : '🔴 فروش';
  const sideColor = sig.side === 'long' ? 'var(--success)' : 'var(--danger)';
  const scorePct = Math.round((sig.score || 0) * 100);

  // Header: symbol, side, score
  const displayName = sig.base || (sig.symbol || '').replace('DEX:', '').split(':').pop().slice(0, 12);
  const header = `
    <span class="csd-symbol">📋 ${displayName}</span>
    <span class="csd-side" style="color:${sideColor}">${sideFa}</span>
    <span class="csd-score">امتیاز: ${scorePct}%</span>
  `;

  // Strategy pills — one pill per strategy hit
  const pills = explanations.map(h => {
    const icon = strategyIcons[h.name] || '📌';
    const sc = h.score >= 0.6 ? 'var(--success)' : h.score >= 0.4 ? 'var(--accent)' : 'var(--warning)';
    return `
      <div class="csd-strat-pill" title="${h.description || ''}">
        <span>${icon}</span>
        <span class="csd-strat-name">${h.name_fa}</span>
        <span class="csd-strat-tf">${h.timeframe}</span>
        <span class="csd-strat-score" style="color:${sc}">${(h.score * 100).toFixed(0)}%</span>
        ${h.description ? '<span class="csd-strat-desc">' + h.description + '</span>' : ''}
      </div>
    `;
  }).join('');

  // Levels row
  const levels = `
    <div class="csd-levels">
      <div class="csd-level" style="color:#38bdf8"><span>🔵 ورود:</span><b>${fmtPrice(markers.entry)}</b></div>
      <div class="csd-level" style="color:#34d399"><span>✅ TP:</span><b>${fmtPrice(markers.take_profit)}</b></div>
      <div class="csd-level" style="color:#ff5a6e"><span>❌ SL:</span><b>${fmtPrice(markers.stop_loss)}</b></div>
      <div class="csd-level" style="color:var(--text-dim)"><span>📊 ATR:</span><b>${fmtPrice(markers.atr)}</b></div>
    </div>
  `;

  $('#csd_header').innerHTML = header;
  $('#csd_strategies').innerHTML = pills + levels;
}

// Chart control events — encode / → _  for FastAPI route compatibility
function chartSymbolEncoded() {
  return $('#chart_symbol').value.replace(/\//g, '_');
}

$('#chart_load').addEventListener('click', () =>
  loadChart($('#chart_exchange').value, chartSymbolEncoded(), $('#chart_tf').value));

$('#chart_exchange').addEventListener('change', () =>
  loadChart($('#chart_exchange').value, chartSymbolEncoded(), $('#chart_tf').value));

$('#chart_symbol').addEventListener('change', () =>
  loadChart($('#chart_exchange').value, chartSymbolEncoded(), $('#chart_tf').value));

$('#chart_tf').addEventListener('change', () =>
  loadChart($('#chart_exchange').value, chartSymbolEncoded(), $('#chart_tf').value));

// Live chart auto-refresh
$('#chart_live').addEventListener('click', () => {
  if (chartLiveInterval) {
    clearInterval(chartLiveInterval);
    chartLiveInterval = null;
    $('#chart_live').textContent = '🔴 زنده';
    $('#chart_live').classList.remove('active');
    toast('نمودار زنده متوقف شد');
    return;
  }
  const doRefresh = () => loadChart($('#chart_exchange').value, chartSymbolEncoded(), $('#chart_tf').value);
  doRefresh();
  chartLiveInterval = setInterval(doRefresh, 5000);
  $('#chart_live').textContent = '⏹️ توقف';
  $('#chart_live').classList.add('active');
  toast('نمودار زنده فعال شد (هر ۵ ثانیه)');
});

/* ---------- SCALPING (اسکلپ) ---------- */
let scalpFilter = 'all';
let scalpCache = [];

const scalpStrategyFa = {
  scalp_vwap_rejection: '📐 واپس ریجکشن',
  scalp_rsi_extreme: '📊 RSI اکستریم',
  scalp_momentum_burst: '🔥 انفجار مومنتوم',
  scalp_stoch_extreme: '📊 استوکاستیک اکستریم',
  scalp_ema_ribbon: '📈 ریبون EMA',
  scalp_bb_touch: '🎯 لمس بولینگر',
  scalp_volume_climax: '🔊 کلایمکس حجم',
  scalp_order_flow: '⚖️ جریان سفارش',
  scalp_squeeze_release: '💎 ریلیز Squeeze',
  scalp_engulfing: '🕯️ انگالفینگ',
  scalp_micromap: '🎯 MicroMap',
  scalp_pro_btb: '🔄 PRO BTB',
  scalp_sp2l: '📈 SP2L',
};

function setScalpFilter(f) {
  scalpFilter = f;
  document.querySelectorAll('.scalp-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  _paginationState['scalp_history'] = 0; // reset to first page on filter change
  renderScalpSignals();
  // Re-render history with same filter
  if (window._allScalpSignals) {
    renderScalpHistory(window._allScalpSignals);
  }
}

function renderScalpSignalCard(s) {
  const sideFa = s.side === 'long' ? '🟢 خرید (LONG)' : '🔴 فروش (SHORT)';
  const methods = [...new Set(s.hits.map(h => h.name))];
  const statusFa = { open: '🟢 فعال', tp: '✅ سود', sl: '❌ ضرر', trailing: '🔄 تریلینگ', closed: '⏰ بسته', no_position: '⏭️ رد شد (ظرفیت پر)' };
  const statusClass = { open: 'status-active', tp: 'status-tp', sl: 'status-sl', trailing: 'status-trailing', closed: 'status-closed' };
  const displayName = s.base || s.symbol;
  const sigStatus = s.status || 'open';

  // Extract setup name and leverage from rationale
  const rationaleText = s.rationale || '';
  let setupName = '';
  let leverage = '';
  const setupMatch = rationaleText.match(/ستاپ:\s*([\w_]+)/);
  const levMatch = rationaleText.match(/اهرم:\s*(\d+)x/);
  if (setupMatch) setupName = setupMatch[1];
  if (levMatch) leverage = levMatch[1] + 'x';

  // Setup display names
  const setupFa = {
    'scalp_micromap': '🎯 MicroMap',
    'scalp_pro_btb': '🔄 PRO BTB',
    'scalp_sp2l': '📈 SP2L',
    'scalp_vwap_rejection': 'VWAP',
    'scalp_momentum_burst': 'مومنتوم',
    'scalp_rsi_extreme': 'RSI',
    'scalp_stoch_extreme': 'استوکاستیک',
    'scalp_ema_ribbon': 'EMA',
    'scalp_bb_touch': 'بولینگر',
    'scalp_volume_climax': 'حجم',
    'scalp_squeeze_release': 'Squeeze',
    'scalp_engulfing': 'انگالفینگ',
    'scalp_order_flow': 'جریان سفارش',
  };

  const tfBreakdown = s.confluence_tf_breakdown || {};
  const tfPills = Object.entries(tfBreakdown)
    .filter(([, v]) => v > 0)
    .map(([tf, v]) => {
      const pct = Math.round(v * 100);
      const color = pct >= 70 ? 'var(--success)' : pct >= 50 ? 'var(--accent)' : 'var(--warning)';
      return `<span class="pill tf-pill" style="border-color:${color};color:${color};font-weight:700">${tf}: ${pct}%</span>`;
    })
    .join('');

  const setupBadge = setupName ? `<span class="pill" style="background:rgba(245,158,11,0.15);color:#f59e0b;font-weight:bold">${setupFa[setupName] || setupName}</span>` : '';
  const leverageBadge = leverage ? `<span class="pill" style="background:rgba(168,85,247,0.15);color:#a855f7;font-weight:bold">⚡ ${leverage}</span>` : '';

  return `
    <div class="signal-card ${s.side} ${statusClass[sigStatus] || ''} scalp-card" style="cursor:pointer;border-left:4px solid #f59e0b" data-setup="${setupName}" onclick="showSignalOnChart('${s.id}')">
      <div class="sym-row">
        <span class="sym">${displayName}</span>
        ${setupBadge}${leverageBadge}
        <span class="sig-badge ${statusClass[sigStatus] || ''}">${statusFa[sigStatus] || sigStatus}</span>
      </div>
      <div class="pills">
        ${methods.map(m => `<span class="pill scalp-pill">${scalpStrategyFa[m] || m}</span>`).join('')}
      </div>
      ${tfPills ? '<div class="pills tf-pills">' + tfPills + '</div>' : ''}
      <div class="side ${s.side}">${sideFa}</div>
      <div class="scorebar"><div style="width:${Math.round(s.score * 100)}%"></div></div>
      <div>امتیاز: <b>${s.score.toFixed(2)}</b></div>
      <div class="levels">
        <div class="level">ورود<b>${fmtPrice(s.entry)}</b></div>
        <div class="level">TP<b>${fmtPrice(s.take_profit)}</b></div>
        <div class="level">SL<b>${fmtPrice(s.stop_loss)}</b></div>
        <div class="level" style="color:#38bdf8;background:rgba(56,189,248,0.1)">💲 فعلی<b>${livePrices[s.symbol] ? fmtPrice(livePrices[s.symbol]) : '...'}</b></div>
      </div>
      <div class="levels" style="margin-top:4px">
        <div class="level" style="color:#a1a8c3">🕐 زمان ورود<b style="font-size:11px">${fmtTime(s.entry_time || s.created_at)}</b></div>
        ${s.exit_time ? `<div class="level" style="color:${(s.pnl_pct||0) >= 0 ? 'var(--success)' : 'var(--danger)'}">🏁 زمان خروج<b style="font-size:11px">${fmtTime(s.exit_time)}</b></div>` : ''}
      </div>
      <div class="rationale">${s.rationale || ''}</div>
    </div>
  `;
}

function renderScalpSignals() {
  const box = $('#scalp_signals_live');
  if (!box) return;
  // Use ALL signals when setup filter is active (not just cache of 30)
  let source = scalpFilter.startsWith('setup_') ? (window._allScalpSignals || scalpCache) : scalpCache;
  let filtered = source;
  // Status filter
  if (scalpFilter === 'open' || scalpFilter === 'tp' || scalpFilter === 'sl') {
    filtered = filtered.filter(s => {
      const st = s.status || 'open';
      if (scalpFilter === 'tp') return st === 'tp' || st === 'tp1' || st === 'tp2' || st === 'tp3';
      if (scalpFilter === 'sl') return st === 'sl' || st === 'sl_risk_free';
      return st === scalpFilter;
    });
  }
  // Setup filter — check BOTH rationale AND hits
  if (scalpFilter.startsWith('setup_')) {
    const setupKey = 'scalp_' + scalpFilter.replace('setup_', '');
    filtered = filtered.filter(s => {
      const rat = s.rationale || '';
      const hitNames = (s.hits || []).map(h => h.name || '');
      return rat.includes(setupKey) || hitNames.includes(setupKey);
    });
  }
  box.innerHTML = filtered.map(renderScalpSignalCard).join('') || '<p style="color:#8e95ac">سیگنالی با این فیلتر پیدا نشد.</p>';
  $('#scalp_count').textContent = scalpCache.length;
}

function changeScalpHistoryPage(newPage) {
  _paginationState['scalp_history'] = newPage;
  renderScalpHistory(window._allScalpSignalsForHistory || []);
}

function renderScalpHistory(signals) {
  const tb = $('#scalp_history');
  const pagerEl = $('#scalp_history_pager');
  if (!tb) return;
  window._allScalpSignalsForHistory = signals; // cache for pagination re-render
  const statusFa = { open: '🟢 فعال', tp: '✅ سود', tp1: '✅ TP1', tp2: '✅ TP2', sl: '❌ ضرر', sl_risk_free: '🛡️ ریسک‌فری', trailing: '🔄 تریلینگ', closed: '⏰ بسته', timeout: '⏰ تایم‌اوت', no_position: '⏭️ رد شد' };

  // Apply setup filter to history too
  let filtered = signals;
  if (scalpFilter.startsWith('setup_')) {
    const setupKey = 'scalp_' + scalpFilter.replace('setup_', '');
    filtered = signals.filter(s => {
      const rat = s.rationale || '';
      const hitNames = (s.hits || []).map(h => h.name || '');
      return rat.includes(setupKey) || hitNames.includes(setupKey);
    });
  }
  const { pageItems, page, totalPages } = paginate(filtered, 'scalp_history', 15);
  tb.innerHTML = pageItems.map(s => {
    const curPrice = livePrices[s.symbol] ? fmtPrice(livePrices[s.symbol]) : '...';
    const curColor = livePrices[s.symbol] ? (livePrices[s.symbol] > s.entry ? (s.side === 'long' ? 'var(--success)' : 'var(--danger)') : (s.side === 'long' ? 'var(--danger)' : 'var(--success)')) : '';
    const st = s.status || 'open';
    const exitCell = s.exit_time
      ? `${fmtTime(s.exit_time)}${s.pnl_pct != null ? ` <span style="color:${s.pnl_pct >= 0 ? 'var(--success)' : 'var(--danger)'}">(${s.pnl_pct >= 0 ? '+' : ''}${s.pnl_pct.toFixed(2)}%)</span>` : ''}`
      : '—';
    return `
    <tr class="clickable-row" style="cursor:pointer" onclick="showScalpTradeOnChart('${s.id}')" title="کلیک کنید تا نمودار این معامله نمایش داده شود">
      <td>${fmtTime(s.entry_time || s.created_at)}</td><td>${s.exchange}</td>
      <td>${s.base || s.symbol}</td>
      <td class="${s.side}">${s.side.toUpperCase()}</td>
      <td>${s.score.toFixed(2)}</td>
      <td>${fmtPrice(s.entry)}</td><td>${fmtPrice(s.take_profit)}</td><td>${fmtPrice(s.stop_loss)}</td>
      <td style="color:${curColor};font-weight:bold">${curPrice}</td>
      <td>${exitCell}</td>
      <td><span class="sig-badge-inline status-${st}">${statusFa[st] || st}</span></td>
      <td>${[...new Set(s.hits.map(h => h.name))].join(', ')}</td>
    </tr>`;
  }).join('');
  if (pagerEl) pagerEl.innerHTML = renderPaginationControls('scalp_history', totalPages, page, 'changeScalpHistoryPage');
}

/* ═══ Click a closed/open scalp trade row → show entry/exit/SL/TP on chart ═══ */
let scalpTradeChart = null;
let scalpTradeCandleSeries = null;
let scalpTradePriceLines = [];
let _scalpChartCurrentSignalId = null;
let _scalpChartCurrentTf = '5m';

function switchScalpChartTf(tf) {
  _scalpChartCurrentTf = tf;
  document.querySelectorAll('.scalp-tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf === tf));
  if (_scalpChartCurrentSignalId) {
    showScalpTradeOnChart(_scalpChartCurrentSignalId, tf);
  }
}

async function showScalpTradeOnChart(signalId, timeframe) {
  const tf = timeframe || _scalpChartCurrentTf || '5m';
  _scalpChartCurrentSignalId = signalId;
  _scalpChartCurrentTf = tf;
  const titleEl = $('#scalp_chart_title');
  const detailEl = $('#scalp_chart_detail');
  const container = document.getElementById('scalp_chart_container');
  const tfSwitcher = document.getElementById('scalp_chart_tf_switcher');
  if (!container) return;
  if (titleEl) titleEl.textContent = '⏳ در حال بارگذاری نمودار...';
  if (tfSwitcher) tfSwitcher.style.display = 'flex';
  document.querySelectorAll('.scalp-tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf === tf));

  // Highlight the clicked row
  document.querySelectorAll('#scalp_history tr').forEach(r => { r.style.background = ''; });
  document.querySelectorAll('#scalp_history tr').forEach(r => {
    if (r.getAttribute('onclick') === `showScalpTradeOnChart('${signalId}')`) {
      r.style.background = 'rgba(245,158,11,0.12)';
    }
  });

  try {
    const resp = await fetch(`/api/scalping/trade/${signalId}/chart?timeframe=${tf}`);
    if (!resp.ok) { toast('اطلاعات این معامله پیدا نشد'); return; }
    const data = await resp.json();
    const sig = data.signal || {};
    const trade = data.trade || null;
    const m = data.markers || {};
    const candles = data.candles || [];

    const symLabel = sig.base || sig.symbol || '';
    if (titleEl) {
      titleEl.textContent = `📈 ${symLabel} — ${sig.side === 'long' ? '🟢 خرید' : '🔴 فروش'} (${data.timeframe || '5m'})`;
    }

    if (scalpTradeChart) { scalpTradeChart.remove(); scalpTradeChart = null; }
    container.innerHTML = '';

    if (!candles.length) {
      container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted)">داده کندلی موجود نیست (احتمالا سیگنال DEX است)</div>';
      if (detailEl) detailEl.textContent = '';
      return;
    }

    scalpTradeChart = LightweightCharts.createChart(container, {
      width: container.clientWidth, height: 420,
      layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
      grid: { vertLines: { color: 'rgba(48,54,61,0.5)' }, horzLines: { color: 'rgba(48,54,61,0.5)' } },
      crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: '#30363d' },
    });
    scalpTradeCandleSeries = scalpTradeChart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444', borderUpColor: '#22c55e',
      borderDownColor: '#ef4444', wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    const volSeries = scalpTradeChart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
    scalpTradeChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    const ohlc = candles.map(c => ({
      time: Math.floor(c.timestamp / 1000), open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    const vol = candles.map(c => ({
      time: Math.floor(c.timestamp / 1000), value: c.volume,
      color: c.close >= c.open ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)',
    }));
    scalpTradeCandleSeries.setData(ohlc);
    volSeries.setData(vol);

    // Price lines: entry / SL / TP / TP2
    scalpTradePriceLines.forEach(pl => { try { scalpTradeCandleSeries.removePriceLine(pl); } catch(e) {} });
    scalpTradePriceLines = [];
    if (m.entry) scalpTradePriceLines.push(scalpTradeCandleSeries.createPriceLine({ price: m.entry, color: '#58a6ff', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: '🔵 ورود' }));
    if (m.take_profit) scalpTradePriceLines.push(scalpTradeCandleSeries.createPriceLine({ price: m.take_profit, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '✅ TP1' }));
    if (m.tp2) scalpTradePriceLines.push(scalpTradeCandleSeries.createPriceLine({ price: m.tp2, color: '#a855f7', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP2' }));
    if (m.stop_loss) scalpTradePriceLines.push(scalpTradeCandleSeries.createPriceLine({ price: m.stop_loss, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '❌ SL' }));
    if (m.exit_price && m.exit_time) {
      const exitColor = (m.pnl_pct || 0) >= 0 ? '#22c55e' : '#ef4444';
      scalpTradePriceLines.push(scalpTradeCandleSeries.createPriceLine({ price: m.exit_price, color: exitColor, lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: '🏁 خروج' }));
    }

    // Markers: entry arrow (always) + exit circle (if closed)
    const entryTime = Math.floor((m.entry_time || sig.created_at || 0));
    const markers = [{
      time: entryTime, position: sig.side === 'long' ? 'belowBar' : 'aboveBar',
      color: sig.side === 'long' ? '#22c55e' : '#ef4444',
      shape: sig.side === 'long' ? 'arrowUp' : 'arrowDown',
      text: `🕐 ورود (${sig.side === 'long' ? 'LONG' : 'SHORT'})`,
    }];
    if (m.exit_time) {
      const exitTime = Math.floor(m.exit_time);
      if (exitTime !== entryTime) {
        markers.push({
          time: exitTime, position: sig.side === 'long' ? 'aboveBar' : 'belowBar',
          color: (m.pnl_pct || 0) >= 0 ? '#22c55e' : '#ef4444', shape: 'circle',
          text: `🏁 خروج (${m.close_reason || ''} ${m.pnl_pct != null ? (m.pnl_pct >= 0 ? '+' : '') + m.pnl_pct.toFixed(2) + '%' : ''})`,
        });
      }
    }
    markers.sort((a, b) => a.time - b.time);
    try { scalpTradeCandleSeries.setMarkers(markers); } catch(e) { console.warn('setMarkers error:', e); }

    // Zoom around the trade window
    const tfSeconds = { '1m':60, '5m':300, '15m':900, '1h':3600 };
    const step = tfSeconds[data.timeframe] || 300;
    try {
      scalpTradeChart.timeScale().setVisibleRange({
        from: entryTime - step * 15,
        to: (m.exit_time ? Math.floor(m.exit_time) : entryTime) + step * 15,
      });
    } catch(e) { scalpTradeChart.timeScale().fitContent(); }

    if (detailEl) {
      const statusFa2 = { open: '🟢 فعال', tp1: '✅ TP1', tp: '✅ سود', sl: '❌ ضرر', sl_risk_free: '🛡️ ریسک‌فری', timeout: '⏰ تایم‌اوت' };
      detailEl.innerHTML = `
        زمان ورود: <b>${fmtTime(m.entry_time || sig.created_at)}</b>
        ${m.exit_time ? ` | زمان خروج: <b>${fmtTime(m.exit_time)}</b>` : ' | هنوز باز است'}
        ${m.close_reason ? ` | نتیجه: <b>${statusFa2[m.close_reason] || m.close_reason}</b>` : ''}
        ${m.pnl_pct != null ? ` | سود/ضرر: <b style="color:${m.pnl_pct >= 0 ? 'var(--success)' : 'var(--danger)'}">${m.pnl_pct >= 0 ? '+' : ''}${m.pnl_pct.toFixed(2)}%</b>` : ''}
      `;
    }

    // Scroll chart into view
    container.scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (e) {
    console.error('showScalpTradeOnChart error:', e);
    toast('خطا در بارگذاری نمودار معامله');
  }
}

function renderScalpWinRates(wr) {
  const box = $('#scalp_win_rates');
  if (!box || !wr) return;

  const periods = [
    { key: 'last_hour', label: '⏰ آخرین ساعت', icon: '⏰' },
    { key: 'last_4h', label: '🕓 آخرین ۴ ساعت', icon: '🕓' },
    { key: 'today', label: '📅 امروز', icon: '📅' },
    { key: 'all', label: '🎯 کل', icon: '🎯' },
  ];

  box.innerHTML = periods.map(p => {
    const d = wr[p.key] || {};
    const wrVal = d.win_rate || 0;
    const wrColor = wrVal >= 60 ? 'var(--success)' : wrVal >= 40 ? 'var(--accent)' : 'var(--danger)';
    const pnlVal = d.avg_pnl_pct || 0;
    const pnlColor = pnlVal >= 0 ? 'var(--success)' : 'var(--danger)';
    const detail = d.total > 0
      ? `${d.wins} برد / ${d.losses} باخت${d.risk_free ? ' / ' + d.risk_free + ' 🛡️ ریسک‌فری' : ''} از ${d.total} ترید`
      : 'هنوز تریدی ثبت نشده';
    return `
      <div class="pos-winrate-card">
        <div class="wr-period">${p.label}</div>
        <div class="wr-value" style="color:${d.total > 0 ? wrColor : 'var(--text-dim)'}">${d.total > 0 ? wrVal.toFixed(1) + '%' : '—'}</div>
        <div class="wr-detail">${detail}</div>
        <div class="wr-pnl" style="color:${pnlVal >= 0 ? pnlColor : 'var(--danger)'}">
          ${d.total > 0 ? 'میانگین: ' + (pnlVal >= 0 ? '+' : '') + pnlVal.toFixed(2) + '%' : ''}
          ${(d.total_pnl_usdt && d.total_pnl_usdt !== 0) ? ' | ' + (d.total_pnl_usdt >= 0 ? '+' : '') + fmtN(d.total_pnl_usdt, 2) + ' USDT' : ''}
        </div>
      </div>
    `;
  }).join('');
}

function renderScalpStats(data) {
  const box = $('#scalp_stats');
  if (!box) return;

  const setupNames = {
    'micromap': '🎯 MicroMap', 'pro_btb': '🔄 PRO BTB', 'sp2l': '📈 SP2L',
    'vwap': '📐 VWAP', 'momentum': '🔥 مومنتوم', 'squeeze': '💎 Squeeze',
    'bb_touch': '🎯 بولینگر', 'engulfing': '🕯️ انگالفینگ', 'other': '📊 سایر',
  };

  let setupHtml = '';
  if (data.setup_stats && Object.keys(data.setup_stats).length > 0) {
    setupHtml = `
      <div style="margin-top:12px">
        <h4 style="margin:0 0 8px 0;font-size:13px">📊 وین ریت هر ستاپ</h4>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px">
          ${Object.entries(data.setup_stats).map(([name, st]) => {
            const wrColor = st.win_rate >= 60 ? 'var(--success)' : st.win_rate >= 40 ? 'var(--accent)' : 'var(--danger)';
            const pnlColor = st.avg_pnl >= 0 ? 'var(--success)' : 'var(--danger)';
            return `<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px;text-align:center">
              <div style="font-size:12px;font-weight:bold;margin-bottom:4px">${setupNames[name] || name}</div>
              <div style="font-size:20px;font-weight:bold;color:${wrColor}">${st.win_rate}%</div>
              <div style="font-size:11px;color:var(--text-muted)">${st.wins}W / ${st.losses}L (${st.total} ترید)</div>
              <div style="font-size:11px;color:${pnlColor}">میانگین: ${st.avg_pnl >= 0 ? '+' : ''}${st.avg_pnl}%</div>
            </div>`;
          }).join('')}
        </div>
      </div>`;

    // Show last closed trades per setup
    const allTrades = Object.values(data.setup_stats).flatMap(st => st.trades || []);
    if (allTrades.length > 0) {
      setupHtml += `
        <div style="margin-top:12px">
          <h4 style="margin:0 0 8px 0;font-size:13px">📋 آخرین معاملات بسته‌شده</h4>
          <div style="overflow-x:auto;max-height:200px;overflow-y:auto">
            <table class="data-table" style="font-size:11px">
              <thead><tr><th>سیگنال</th><th>ستاپ</th><th>سود/ضرر</th><th>نتیجه</th></tr></thead>
              <tbody>
                ${allTrades.slice(-15).reverse().map(t => {
                  const pnlC = t.pnl_pct >= 0 ? 'var(--success)' : 'var(--danger)';
                  const icon = t.pnl_pct >= 0 ? '✅' : '❌';
                  const clickable = !!t.signal_id;
                  const rowAttrs = clickable
                    ? `style="cursor:pointer" onclick="showScalpTradeOnChart('${t.signal_id}')" title="کلیک کنید تا نمودار این معامله نمایش داده شود"`
                    : '';
                  return `<tr ${rowAttrs}>
                    <td>${t.signal_id || '—'}</td>
                    <td>${setupNames[t.setup] || t.setup}</td>
                    <td style="color:${pnlC};font-weight:bold">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct?.toFixed(2) || 0}%</td>
                    <td>${icon} ${t.reason || ''}</td>
                  </tr>`;
                }).join('')}
              </tbody>
            </table>
          </div>
        </div>`;
    }
  }

  box.innerHTML = `
    <div class="hunter-summary-cards">
      <div class="card stat-card hunter-stat">
        <span class="card-label">⚡ کل سیگنال‌ها</span>
        <span class="card-value">${data.total_signals || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">🟢 فعال</span>
        <span class="card-value" style="color:var(--accent)">${data.active || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">✅ سود</span>
        <span class="card-value" style="color:var(--success)">${data.tp || 0}</span>
      </div>
      <div class="card stat-card hunter-stat">
        <span class="card-label">❌ ضرر</span>
        <span class="card-value" style="color:var(--danger)">${data.sl || 0}</span>
      </div>
    </div>
    ${setupHtml}
  `;
}

async function loadScalping() {
  try {
    const { signals } = await json('/api/scalping/signals?limit=200');
    window._allScalpSignals = signals;  // Store all for filtering
    scalpCache = signals.slice(0, 30);
    renderScalpSignals();
    renderScalpHistory(signals);
    // Fetch live prices for scalp symbols
    fetchPricesForSignals(signals);
  } catch (e) { console.error('loadScalping:', e); }
  try {
    const { win_rates } = await json('/api/scalping/win-rates');
    renderScalpWinRates(win_rates);
  } catch (e) { console.error('loadScalpWinRates:', e); }
  try {
    const stats = await json('/api/scalping/stats');
    renderScalpStats(stats);
  } catch (e) { console.error('loadScalpStats:', e); }
}

async function testScalping() {
  toast('در حال تست موتور اسکلپ...');
  try {
    const r = await fetch('/api/scalping/test', { method: 'POST' });
    const data = await r.json();
    if (data.ok && data.signal) {
      toast(`✅ سیگنال تولید شد: ${data.signal.symbol} ${data.signal.side}`);
    } else {
      toast(data.msg || 'سیگنالی تولید نشد');
    }
  } catch (e) {
    toast('خطا در تست اسکلپ');
  }
}

/* ---------- BACKTEST ---------- */
let equityChart = null;
$('#bt_run').addEventListener('click', async () => {
  toast('در حال اجرای بک‌تست...');
  try {
    const r = await fetch('/api/backtest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        exchange: $('#bt_exchange').value,
        symbol: $('#bt_symbol').value,
      })
    });
    const bt = await r.json();
    if (!r.ok) { toast('خطای بک‌تست: ' + (bt.detail || 'unknown')); return; }
    $('#bt_summary').innerHTML = `
      <div class="metric"><div class="l">تعداد تریدها</div><div class="v">${bt.n_trades}</div></div>
      <div class="metric"><div class="l">Win Rate</div><div class="v">${bt.win_rate}%</div></div>
      <div class="metric"><div class="l">Profit Factor</div><div class="v">${bt.profit_factor}</div></div>
      <div class="metric"><div class="l">بازده کل</div><div class="v ${bt.total_return_pct >= 0 ? 'pos' : 'neg'}">${bt.total_return_pct}%</div></div>
      <div class="metric"><div class="l">Max Drawdown</div><div class="v neg">${bt.max_drawdown_pct}%</div></div>
      <div class="metric"><div class="l">Sharpe</div><div class="v">${bt.sharpe}</div></div>
      <div class="metric"><div class="l">Sortino</div><div class="v">${bt.sortino}</div></div>
      <div class="metric"><div class="l">Calmar</div><div class="v">${bt.calmar}</div></div>
      <div class="metric"><div class="l">میانگین PnL</div><div class="v">${bt.avg_pnl_pct}%</div></div>
      <div class="metric"><div class="l">میانگین برد</div><div class="v pos">${bt.avg_win_pct}%</div></div>
      <div class="metric"><div class="l">میانگین باخت</div><div class="v neg">${bt.avg_loss_pct}%</div></div>
      <div class="metric"><div class="l">حداکثر برد متوالی</div><div class="v">${bt.max_consecutive_wins}</div></div>
      <div class="metric"><div class="l">حداکثر باخت متوالی</div><div class="v neg">${bt.max_consecutive_losses}</div></div>
      <div class="metric"><div class="l">میانگین مدت نگهداری</div><div class="v">${bt.avg_holding_bars} کندل</div></div>
      <div class="metric"><div class="l">Expectation</div><div class="v ${bt.expectation >= 0 ? 'pos' : 'neg'}">${(bt.expectation * 100).toFixed(3)}%</div></div>
      <div class="metric"><div class="l">Recovery Factor</div><div class="v">${bt.recovery_factor}</div></div>
      <div class="metric"><div class="l">Monte Carlo 5%</div><div class="v neg">${bt.monte_carlo_5pct}%</div></div>
      <div class="metric"><div class="l">Monte Carlo 95%</div><div class="v pos">${bt.monte_carlo_95pct}%</div></div>
    `;
    // equity curve with drawdown overlay
    const eqLabels = bt.equity_curve.map(p => new Date(p.t * 1000).toISOString());
    const eqValues = bt.equity_curve.map(p => p.v);
    const ddValues = (bt.drawdown_curve || bt.equity_curve).map(p => p.d != null ? p.d : 0);
    if (equityChart) equityChart.destroy();
    equityChart = new Chart($('#equity_chart'), {
      type: 'line',
      data: { labels: eqLabels, datasets: [
        {
          label: 'منحنی سرمایه (USDT)', data: eqValues,
          borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,.15)',
          tension: .2, fill: true, pointRadius: 0, yAxisID: 'y',
        },
        {
          label: 'Drawdown %', data: ddValues,
          borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,.1)',
          tension: .2, fill: true, pointRadius: 0, yAxisID: 'y1',
          borderDash: [4, 2],
        }
      ]},
      options: {
        plugins: { legend: { labels: { color: '#e6e9f2' } } },
        scales: {
          x: { display: false },
          y: { ticks: { color: '#8e95ac' }, position: 'right', title: { display: true, text: 'USDT', color: '#8e95ac' } },
          y1: { ticks: { color: '#ef4444' }, position: 'left', title: { display: true, text: 'DD%', color: '#ef4444' },
                 grid: { drawOnChartArea: false } },
        }
      }
    });
    // trades table
    $('#bt_trades').innerHTML = (bt.trades || []).map(t => `
      <tr>
        <td>${fmtTime(t.opened_at)}</td>
        <td class="${t.side}">${t.side.toUpperCase()}</td>
        <td>${fmtPrice(t.entry)}</td>
        <td>${t.exit_price ? fmtPrice(t.exit_price) : '—'}</td>
        <td class="${(t.pnl_usdt || 0) >= 0 ? 'pos' : 'neg'}">${fmtN(t.pnl_usdt, 2)}</td>
        <td class="${(t.pnl_pct || 0) >= 0 ? 'pos' : 'neg'}">${t.pnl_pct}%</td>
        <td>${t.close_reason || '—'}</td>
      </tr>`).join('');
    toast('بک‌تست تمام شد ✅');
  } catch (e) { toast('خطای شبکه'); console.error(e); }
});

/* ---------- ASSISTANT ---------- */
const aLog = $('#assistant_log');
function pushAssistantMsg(kind, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + (kind === 'user' ? 'user' : 'bot');
  // Sanitize HTML: escape & < > to prevent XSS
  const safe = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  div.innerHTML = safe + `<div class="meta">${new Date().toLocaleTimeString('fa-IR')}</div>`;
  aLog.appendChild(div);
  aLog.scrollTop = aLog.scrollHeight;
}

async function askAssistant(text) {
  pushAssistantMsg('user', text);
  try {
    const r = await fetch('/api/assistant', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text })
    });
    const { text: ans } = await r.json();
    pushAssistantMsg('bot', ans);
  } catch (e) { pushAssistantMsg('bot', 'خطا در ارتباط با دستیار'); }
}
$('#assistant_send').addEventListener('click', () => {
  const t = $('#assistant_input').value;
  if (!t.trim()) return;
  askAssistant(t);
  $('#assistant_input').value = '';
});
$('#assistant_input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#assistant_send').click();
});
$$('.chip').forEach(c => c.addEventListener('click', () => askAssistant(c.dataset.q)));

async function loadAssistantLog() {
  try {
    const { log } = await json('/api/assistant/log?limit=50');
    aLog.innerHTML = '';
    (log || []).forEach(m => pushAssistantMsg(m.kind === 'assistant' ? 'bot' : m.kind, m.text));
  } catch (e) { /* ignore */ }
}

/* ---------- WEBSOCKET ---------- */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws`);
  } catch (e) { setTimeout(connectWS, 5000); return; }
  ws.onopen = () => {
    $('#ws_status').textContent = '✅'; $('#ws_status').className = 'card-value dot';
    toast('اتصال زنده برقرار شد');
  };
  ws.onclose = () => {
    $('#ws_status').textContent = '🔴'; $('#ws_status').className = 'card-value dot off';
    setTimeout(connectWS, 4000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === 'signal' || d.type === 'signal_opened' || d.type === 'scalp_signal') {
      const s = d.type === 'signal_opened' ? d.signal : (d.data || d.signal);
      if (s) {
        if (s.id && s.id.startsWith('SCP_')) {
          // Scalping signal
          scalpCache.unshift(s);
          if (scalpCache.length > 30) scalpCache.pop();
          renderScalpSignals();
          toast(`⚡ سیگنال اسکلپ: ${s.symbol} ${s.side}`);
        } else {
          addLiveSignal(s);
          toast(`سیگنال جدید: ${s.symbol} ${s.side}`);
        }
        refreshState();
      }
    } else if (d.type === 'positions') {
      loadTrades(); loadPosWinRates(); refreshState();
    } else if (d.type === 'universe') {
      stateCache.universe = d.data; renderUniverse();
    } else if (d.type === 'meme_hunter') {
      hunterCache.summary = d.data?.summary || {};
      hunterCache.hits = d.data?.hits || {};
      renderHunterSummary(hunterCache.summary);
      renderHunterResults();
      renderDailyPicks(d.data?.daily_picks || []);
      const total = hunterCache.summary.total_unique || 0;
      if (total > 0) toast(`🎯 شکارچی: ${total} فرصت میم‌کوین شناسایی شد`);
    } else if (d.type === 'live_prices') {
      livePrices = d.data || {};
      // Update header live prices
      if (livePrices['BTC/USDT']) {
        const el = document.getElementById('live_btc');
        if (el) el.textContent = '$' + Number(livePrices['BTC/USDT']).toLocaleString('en-US', {maximumFractionDigits: 0});
      }
      if (livePrices['ETH/USDT']) {
        const el = document.getElementById('live_eth');
        if (el) el.textContent = '$' + Number(livePrices['ETH/USDT']).toLocaleString('en-US', {maximumFractionDigits: 1});
      }
      // Update positions table with live prices if visible
      if ($('#tab-positions')?.classList.contains('active')) {
        loadTrades();
      }
    } else if (d.type === 'ping') { /* keepalive */ }
  };
}

/* ---------- SETTINGS ---------- */
async function loadSettings() {
  try {
    const st = await json('/api/settings');
    // API Keys
    if (st.binance_key) $('#set_binance_key').value = st.binance_key;
    if (st.binance_secret) $('#set_binance_secret').value = st.binance_secret;
    if (st.bybit_key) $('#set_bybit_key').value = st.bybit_key;
    if (st.bybit_secret) $('#set_bybit_secret').value = st.bybit_secret;
    // Telegram
    $('#set_tg_enabled').checked = st.telegram_enabled || false;
    if (st.tg_token) $('#set_tg_token').value = st.tg_token;
    if (st.tg_chat) $('#set_tg_chat').value = st.tg_chat;
    // Risk
    $('#set_risk_pct').value = st.risk_per_trade_pct || 1.0;
    $('#set_initial_balance').value = st.initial_balance || 10000;
    $('#set_max_positions').value = st.max_positions || 8;
    $('#set_daily_loss').value = st.daily_loss_pct || 5.0;
    $('#set_sl_mult').value = st.sl_mult || 1.5;
    $('#set_tp_mult').value = st.tp_mult || 3.0;
    $('#set_trail_mult').value = st.trail_mult || 2.0;
    // Strategies
    $('#set_min_score').value = st.min_signal_score || 0.55;
    $('#set_st_new_listing').checked = st.st_new_listing !== false;
    $('#set_st_volume').checked = st.st_volume !== false;
    $('#set_st_orderbook').checked = st.st_orderbook !== false;
    $('#set_st_liquidity').checked = st.st_liquidity !== false;
    $('#set_st_momentum').checked = st.st_momentum !== false;
    $('#set_st_rsi').checked = st.st_rsi !== false;
    $('#set_st_bb').checked = st.st_bb !== false;
    $('#set_st_funding').checked = st.st_funding === true;
    $('#set_st_social').checked = st.st_social === true;
    $('#set_st_ema').checked = st.st_ema !== false;
    $('#set_st_adx').checked = st.st_adx !== false;
    $('#set_st_squeeze').checked = st.st_squeeze !== false;
    $('#set_st_vwap').checked = st.st_vwap !== false;
    $('#set_st_macd').checked = st.st_macd !== false;
    $('#set_st_stoch_rsi').checked = st.st_stoch_rsi !== false;
    $('#set_st_obv').checked = st.st_obv !== false;
    $('#set_st_sr').checked = st.st_sr !== false;
    $('#set_st_vol_trend').checked = st.st_vol_trend !== false;
    // DEX
    $('#set_dex_enabled').checked = st.dex_enabled !== false;
    $('#set_dex_min_liq').value = st.dex_min_liquidity || 10000;
    $('#set_dex_max_age').value = st.dex_max_age || 24;
    // LLM
    $('#set_llm_enabled').checked = st.llm_enabled === true;
    if (st.llm_model) $('#set_llm_model').value = st.llm_model;
    if (st.llm_key) $('#set_llm_key').value = st.llm_key;
    if (st.llm_url) $('#set_llm_url').value = st.llm_url;
  } catch (e) { console.error('loadSettings error:', e); }
}

async function saveSettings() {
  const data = {
    binance_key: $('#set_binance_key').value,
    binance_secret: $('#set_binance_secret').value,
    bybit_key: $('#set_bybit_key').value,
    bybit_secret: $('#set_bybit_secret').value,
    telegram_enabled: $('#set_tg_enabled').checked,
    tg_token: $('#set_tg_token').value,
    tg_chat: $('#set_tg_chat').value,
    risk_per_trade_pct: parseFloat($('#set_risk_pct').value),
    initial_balance: parseFloat($('#set_initial_balance').value),
    max_positions: parseInt($('#set_max_positions').value),
    daily_loss_pct: parseFloat($('#set_daily_loss').value),
    sl_mult: parseFloat($('#set_sl_mult').value),
    tp_mult: parseFloat($('#set_tp_mult').value),
    trail_mult: parseFloat($('#set_trail_mult').value),
    min_signal_score: parseFloat($('#set_min_score').value),
    st_new_listing: $('#set_st_new_listing').checked,
    st_volume: $('#set_st_volume').checked,
    st_orderbook: $('#set_st_orderbook').checked,
    st_liquidity: $('#set_st_liquidity').checked,
    st_momentum: $('#set_st_momentum').checked,
    st_rsi: $('#set_st_rsi').checked,
    st_bb: $('#set_st_bb').checked,
    st_funding: $('#set_st_funding').checked,
    st_social: $('#set_st_social').checked,
    st_ema: $('#set_st_ema').checked,
    st_adx: $('#set_st_adx').checked,
    st_squeeze: $('#set_st_squeeze').checked,
    st_vwap: $('#set_st_vwap').checked,
    st_macd: $('#set_st_macd').checked,
    st_stoch_rsi: $('#set_st_stoch_rsi').checked,
    st_obv: $('#set_st_obv').checked,
    st_sr: $('#set_st_sr').checked,
    st_vol_trend: $('#set_st_vol_trend').checked,
    dex_enabled: $('#set_dex_enabled').checked,
    dex_min_liquidity: parseFloat($('#set_dex_min_liq').value),
    dex_max_age: parseInt($('#set_dex_max_age').value),
    llm_enabled: $('#set_llm_enabled').checked,
    llm_model: $('#set_llm_model').value,
    llm_key: $('#set_llm_key').value,
    llm_url: $('#set_llm_url').value,
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    const res = await r.json();
    const el = $('#settings_status');
    if (res.ok) {
      el.className = 'settings-status success';
      el.textContent = '✅ تنظیمات ذخیره شد! برای اعمال تغییرات، سرویس را ریستارت کنید.';
    } else {
      el.className = 'settings-status error';
      el.textContent = '❌ خطا: ' + (res.error || 'ناشناخته');
    }
    setTimeout(() => el.className = 'settings-status', 5000);
  } catch (e) {
    const el = $('#settings_status');
    el.className = 'settings-status error';
    el.textContent = '❌ خطا در ارتباط با سرور';
  }
}

async function testTelegram() {
  try {
    const r = await fetch('/api/test.telegram', { method: 'POST' });
    const res = await r.json();
    const el = $('#settings_status');
    el.className = res.ok ? 'settings-status success' : 'settings-status error';
    el.textContent = res.ok ? '✅ پیام تست تلگرام ارسال شد!' : '❌ خطا: ' + (res.error || 'اتصال برقرار نشد');
  } catch (e) {
    $('#settings_status').className = 'settings-status error';
    $('#settings_status').textContent = '❌ خطا در ارتباط با سرور';
  }
}

async function testExchange() {
  try {
    const r = await fetch('/api/test.exchange', { method: 'POST' });
    const res = await r.json();
    const el = $('#settings_status');
    el.className = res.ok ? 'settings-status success' : 'settings-status error';
    el.textContent = res.ok ? '✅ اتصال صرافی برقرار است!' : '❌ خطا: ' + (res.error || 'اتصال برقرار نشد');
  } catch (e) {
    $('#settings_status').className = 'settings-status error';
    $('#settings_status').textContent = '❌ خطا در ارتباط با سرور';
  }
}

/* ---------- HELP / TIPS ---------- */
function showTipOfDay() {
  const tips = [
    { title: '💡 نکته: امتیاز سیگنال', text: 'هرچه امتیاز سیگنال بالاتر باشد (نزدیک ۱)، قدرت سیگنال بیشتر است. امتیاز بالای ۰.۷ معمولاً قوی در نظر گرفته می‌شود.' },
    { title: '💡 نکته: حد ضرر (SL)', text: 'حد ضرر به‌صورت خودکار روی ۱.۵ برابر ATR تنظیم شده است. اگر قیمت به SL برسد، پوزیشن بسته می‌شود تا از ضرر بیشتر جلوگیری شود.' },
    { title: '💡 نکته: تریلینگ استاپ', text: 'وقتی قیمت ۲ برابر ATR از نقطه ورود فاصله بگیرد، حد ضرر به‌صورت خودکار حرکت می‌کند تا سود قفل شود.' },
    { title: '💡 نکته: بک‌تست', text: 'قبل از فعال‌سازی استراتژی، حتماً آن را روی داده‌های تاریخی بک‌تست کنید. ترید واقعی با پول متفاوت از بک‌تست است.' },
    { title: '💡 نکته: ریسک روزانه', text: 'اگر ضرر روزانه از ۵٪ موجودی بیشتر شود، معاملات جدید مسدود می‌شوند تا فردا.' },
    { title: '💡 نکته: تایم‌فریم', text: 'سیگنال‌ها از ۴ تایم‌فریم (۱ دقیقه، ۵ دقیقه، ۱۵ دقیقه، ۱ ساعت) ترکیب می‌شوند. تایم‌فریم ۱ دقیقه بیشترین وزن را دارد.' },
    { title: '💡 نکته: DEX', text: 'توکن‌های DEX (مثل Pump.fun) ریسک بالاتری دارند ولی پتانسیل سود بیشتری هم دارند. همیشه نقدینگی و سن توکن را بررسی کنید.' },
    { title: '💡 نکته: شکارچی میم‌کوین', text: 'شکارچی ۴ استراتژی دارد: شکار قبل از پامپ (بیشترین سود/ریسک)، بعد از Migration (تعادل ریسک/سود)، Smart Money (دنبال کردن کیف پول‌های هوشمند)، و روایت‌ها (همسویی با ترندها). امتیاز بالای ۷۰٪ معمولاً قوی است.' },
  ];
  const dayIdx = new Date().getDate() % tips.length;
  const tip = tips[dayIdx];
  const el = document.getElementById('tip_of_day');
  if (el) el.innerHTML = `<h4>${tip.title}</h4><p>${tip.text}</p>`;
}

/* ---------- INIT ---------- */
(async function init() {
  await refreshState();
  await loadSignals();
  await loadScalping();
  await loadHunterResults();
  await loadPosWinRates();
  loadMarketSentiment();  // async, non-blocking
  connectWS();
  showTipOfDay();
  setInterval(refreshState, 15000);
  setInterval(loadSignals, 45000);
  setInterval(loadScalping, 30000);
  setInterval(loadHunterResults, 30000);  // refresh hunter every 30s
  setInterval(loadPosWinRates, 60000);    // refresh win rates every 60s
  setInterval(loadMarketSentiment, 300000); // refresh sentiment every 5 min
  setInterval(loadLitSignals, 30000);      // refresh LIT signals every 30s
  loadLitSignals();  // initial load
})();

// ═══════════════════════════════════════════════════════════════════════════
// LIT Strategy — Live Signals + Backtest + Educational Chart
// ═══════════════════════════════════════════════════════════════════════════

let litChart = null;
let litCandleSeries = null;
let litVolumeSeries = null;

// ─── نام‌های نمایشی استراتژی‌های LIT ───
// این‌ها دقیقاً همان ۳ ستاپی هستند که موتور LIT واقعاً تولید می‌کند
// (strategies/lit_patterns.py -> SetupType). چند نام قدیمی/عمومی هم برای
// سازگاری با نسخه‌های قبلی نگه داشته شده‌اند.
const LIT_STRATEGY_NAMES = {
  'sweep_reversal':          { fa: '🌊 شکار و برگشت', full: 'Sweep Reversal' },
  'inducement_continuation': { fa: '🎯 تله و ادامه', full: 'Inducement Continuation' },
  'range_expansion':         { fa: '📐 محدوده به انبساط', full: 'Range Expansion' },
  // نام‌های قدیمی — فقط برای نمایش صحیح سیگنال‌های ثبت‌شده قبلی
  'liquidity_sweep': { fa: '🔥 جاروب نقدینگی', full: 'Liquidity Sweep' },
  'fvg':             { fa: '📦 شکاف منصفانه', full: 'Fair Value Gap' },
  'order_block':     { fa: '🧱 بلاک سفارش', full: 'Order Block' },
  'power_of_three':  { fa: '⚡ سه‌گانه قدرت', full: 'Power of Three' },
  'vector_bos':      { fa: '🎯 شکست ساختار', full: 'Vector BOS' },
  'fvg_retest':      { fa: '📦 ریتست FVG', full: 'FVG Retest' },
  'displacement_entry': { fa: '💥 ورود با جابجایی', full: 'Displacement Entry' },
  'lit_structure':   { fa: '📊 ساختار LIT', full: 'LIT Structure' },
};

// ─── محتوای آموزشی — دقیقاً منطبق با منطق واقعی موتور LIT ───
// هر بخش گام‌به‌گام همان مراحلی را توضیح می‌دهد که کد در
// strategies/lit_structure.py / lit_liquidity.py / lit_patterns.py طی
// می‌کند، تا کاربر بفهمد چرا یک سیگنال خاص صادر شده.
const LIT_STRATEGY_EDUCATION = {
  'sweep_reversal': {
    title: '🌊 شکار و برگشت (Sweep-Reversal)',
    what: 'رایج‌ترین و قوی‌ترین ستاپ LIT. وقتی قیمت به یک سطح نقدینگی (سقف/کف قبلی یا سقف‌ها/کف‌های برابر که استاپ‌لاس زیادی پشتشان جمع شده) می‌رسد، آن را «جاروب» می‌کند و بعد به‌شدت برمی‌گردد — این برگشت نشانه ورود پول هوشمند در جهت مخالف است.',
    how: [
      '۱) یک سطح نقدینگی (سقف/کف قبلی یا چند سقف/کف نزدیک به هم) شناسایی می‌شود',
      '۲) قیمت با یک سایه بلند از آن سطح عبور می‌کند (Sweep) — ولی کندل بعدی دوباره به همان سمت برنمی‌گردد (یعنی برگشت واقعی است، نه یک شکست موقت)',
      '۳) بعد از این جاروب، یک تغییر ساختار (CHoCH) یا شکست ساختار (BOS) در جهت مخالف تأیید می‌شود',
      '۴) یک کندل قوی (Displacement) با بدنه بزرگ در جهت برگشت شکل می‌گیرد',
      '۵) اگر تمام این شرایط هم‌زمان برقرار باشند و امتیاز کلی از حد آستانه بگذرد، سیگنال صادر می‌شود',
    ],
    entry: 'نزدیک قیمت فعلی بعد از تأیید کندل جهنده (Displacement)',
    sl: 'پشت نقطه‌ای که قیمت جاروب کرد (فراتر از سایه Sweep)',
    tp: 'TP1: حداقل ۱.۵ برابر ریسک | TP2: نزدیک‌ترین سطح نقدینگی مخالف (هدف واقعی بازار)',
    tip: 'هرچه سطح جاروب‌شده قوی‌تر باشد (چند بار لمس شده) و حجم معامله بالاتر باشد، اعتماد سیگنال بیشتر است.',
  },
  'inducement_continuation': {
    title: '🎯 تله و ادامه (Inducement-Continuation)',
    what: 'وقتی روند تایم‌فریم بالا (۱ساعته/۴ساعته) مشخص و قوی است، بازار گاهی یک عقب‌نشینی کوچک (Pullback) می‌کند که معامله‌گران مبتدی را به گرفتن پوزیشن مخالف روند «تله» می‌زند، سپس دوباره در جهت اصلی روند ادامه می‌دهد.',
    how: [
      '۱) روند تایم‌فریم بالا (HTF) باید صعودی یا نزولی باشد — این ستاپ در بازار خنثی (Range) فعال نمی‌شود',
      '۲) در طول عقب‌نشینی، یک نقدینگی داخلی (سقف/کف کوچک) جاروب می‌شود',
      '۳) بعد از جاروب، یک شکست ساختار (BOS) هم‌جهت با روند اصلی تأیید می‌شود',
      '۴) یک کندل قوی در جهت روند اصلی شکل می‌گیرد',
      '۵) ورود در جهت روند اصلی، نه در جهت عقب‌نشینی',
    ],
    entry: 'نزدیک قیمت فعلی، پس از تأیید BOS هم‌جهت با روند اصلی',
    sl: 'پشت نقدینگی داخلی که جاروب شد',
    tp: 'TP1: حداقل ۱.۵ برابر ریسک | TP2: نزدیک‌ترین نقدینگی خارجی در جهت روند اصلی',
    tip: 'این ستاپ فقط هم‌جهت با روند بزرگ‌تر معامله می‌کند — امن‌تر از شکار و برگشت است چون خلاف روند اصلی وارد نمی‌شود.',
  },
  'range_expansion': {
    title: '📐 محدوده به انبساط (Range-Expansion)',
    what: 'وقتی قیمت مدتی در یک محدوده فشرده (رنج) نوسان کرده (نه صعودی نه نزولی)، معمولاً یک طرف رنج جاروب می‌شود و بعد قیمت با یک حرکت انبساطی قوی از رنج خارج می‌شود.',
    how: [
      '۱) یک محدوده فشرده (رنج کمتر از ۴ برابر ATR) در ۳۰ کندل اخیر شناسایی می‌شود',
      '۲) یک طرف این رنج (سقف یا کف) جاروب می‌شود',
      '۳) یک کندل جهنده (Displacement) در جهت مخالف طرف جاروب‌شده ظاهر می‌شود',
      '۴) ورود در جهت انبساط (خروج از رنج)، معمولاً روی جهش سریع قیمت',
    ],
    entry: 'نزدیک قیمت فعلی، بلافاصله پس از کندل انبساطی',
    sl: 'پشت طرف رنج که جاروب شد',
    tp: 'TP1: حداقل ۱.۵ برابر ریسک | TP2: نزدیک‌ترین نقدینگی در جهت انبساط',
    tip: 'این ستاپ معمولاً کمی ریسک‌پذیرتر است چون شرط تأیید ساختاری (BOS/CHoCH) کمتری نسبت به دو ستاپ دیگر دارد — به همین دلیل هم‌راستایی با روند تایم‌فریم بالا اهمیت بیشتری پیدا می‌کند.',
  },
  // نام‌های قدیمی — برای سیگنال‌های قدیمی که هنوز در دیتابیس‌اند
  'liquidity_sweep': {
    title: '🔥 جاروب نقدینگی (نام قدیمی — مشابه «شکار و برگشت»)',
    what: 'نام قبلی همان ستاپ Sweep-Reversal. توضیحات کامل را در کارت «شکار و برگشت» ببینید.',
    how: ['به کارت «🌊 شکار و برگشت» مراجعه کنید'],
    entry: '—', sl: '—', tp: '—',
    tip: 'این نام‌گذاری در نسخه‌های قدیمی‌تر استفاده می‌شد.',
  },
};

// ═══ LIT State ═══
let litLiveSignals = [];
let litLiveChart = null;
let litLiveCandleSeries = null;
let litLiveVolumeSeries = null;

// ═══ Subtab switching ═══
function litSwitchSubtab(subtab) {
  document.querySelectorAll('.lit-subtab-btn').forEach(btn => {
    btn.style.borderBottom = btn.dataset.litSubtab === subtab ? '2px solid var(--accent)' : '2px solid transparent';
    btn.classList.toggle('active', btn.dataset.litSubtab === subtab);
  });
  document.querySelectorAll('.lit-subtab-panel').forEach(p => p.style.display = 'none');
  const panel = document.getElementById('lit_sub_' + subtab);
  if (panel) panel.style.display = 'block';
  if (subtab === 'edu') renderLitEduCards();
  if (subtab === 'live') {
    setTimeout(() => { if (litLiveChart) litLiveChart.applyOptions({ width: document.getElementById('lit_live_chart_container')?.clientWidth || 600 }); }, 50);
  }
}

function litClearAll() {
  litLiveSignals = [];
  renderLitLiveList();
  document.getElementById('lit_live_detail').innerHTML = '<p style="color:var(--text-muted);font-size:13px;text-align:center;padding:20px 0">پاک شد</p>';
  if (litLiveChart) { litLiveChart.remove(); litLiveChart = null; }
  document.getElementById('lit_live_chart_title').textContent = '📈 روی یک سیگنال کلیک کنید';
}

// ═══ Live Signals ═══
async function loadLitSignals() {
  try {
    const resp = await fetch('/api/lit/signals?days=10');
    const data = await resp.json();
    litLiveSignals = data.signals || [];
    renderLitLiveList();
    renderLitLiveMetrics();
  } catch (e) {
    console.error('LIT signals error:', e);
  }
  try {
    const { win_rates } = await json('/api/lit/win-rates');
    renderLitWinRates(win_rates);
  } catch (e) {
    console.error('LIT win-rates error:', e);
  }
}

function renderLitWinRates(wr) {
  const box = $('#lit_win_rates');
  if (!box || !wr) return;
  const periods = [
    { key: 'last_hour', label: '⏰ آخرین ساعت' },
    { key: 'last_4h', label: '🕓 آخرین ۴ ساعت' },
    { key: 'today', label: '📅 امروز' },
    { key: 'all', label: '🎯 کل' },
  ];
  box.innerHTML = periods.map(p => {
    const d = wr[p.key] || {};
    const wrVal = d.win_rate || 0;
    const wrColor = wrVal >= 60 ? 'var(--success)' : wrVal >= 40 ? 'var(--accent)' : 'var(--danger)';
    const detail = d.total > 0
      ? `${d.wins} برد / ${d.losses} باخت از ${d.total} سیگنال بسته‌شده`
      : 'هنوز سیگنال بسته‌شده‌ای ثبت نشده';
    return `
      <div class="pos-winrate-card">
        <div class="wr-period">${p.label}</div>
        <div class="wr-value" style="color:${d.total > 0 ? wrColor : 'var(--text-dim)'}">${d.total > 0 ? wrVal.toFixed(1) + '%' : '—'}</div>
        <div class="wr-detail">${detail}</div>
      </div>
    `;
  }).join('');
}

function renderLitLiveMetrics() {
  const s = litLiveSignals;
  document.getElementById('lit_live_count').textContent = s.length;
  document.getElementById('lit_live_long').textContent = s.filter(x => x.side === 'long').length;
  document.getElementById('lit_live_short').textContent = s.filter(x => x.side === 'short').length;
  document.getElementById('lit_live_symbols').textContent = new Set(s.map(x => x.symbol)).size;
  const best = s.length ? Math.max(...s.map(x => x.score || 0)).toFixed(2) : '—';
  document.getElementById('lit_live_best').textContent = best;
}

function changeLitLiveListPage(newPage) {
  _paginationState['lit_live_signals'] = newPage;
  renderLitLiveList();
}

function renderLitLiveList() {
  const tbody = document.getElementById('lit_live_signals');
  const pagerEl = document.getElementById('lit_live_signals_pager');
  if (!tbody) return;
  const filter = document.getElementById('lit_live_filter')?.value || 'all';
  const filtered = filter === 'all' ? litLiveSignals : litLiveSignals.filter(s => s.side === filter);

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text-muted)">هنوز سیگنالی ثبت نشده</td></tr>';
    if (pagerEl) pagerEl.innerHTML = '';
    return;
  }

  const { pageItems, page, totalPages } = paginate(filtered, 'lit_live_signals', 15);

  tbody.innerHTML = pageItems.map((s) => {
    const sideFa = s.side === 'long' ? '🟢' : '🔴';
    const strat = LIT_STRATEGY_NAMES[s.strategy]?.fa || s.strategy || 'LIT';
    const tf = s.timeframe || '15m';
    const status = s.status || 'open';
    const statusFa = { open: '🟢 فعال', tp: '✅ سود', tp1: '✅ TP1', tp2: '✅ TP2', tp3: '✅ TP3', sl: '❌ ضرر', sl_risk_free: '🛡️ ریسک‌فری', win: '✅ سود', loss: '❌ ضرر', expired: '⏰ منقضی', timeout: '⏰ تایم‌اوت', no_position: '⏭️ رد شد' }[status] || status;
    const statusTitle = {
      open: 'این سیگنال هنوز باز است و در حال ردیابی قیمت زنده است',
      tp: 'قیمت به هدف سود رسید و معامله با سود بسته شد',
      tp1: 'قیمت به هدف اول سود (TP1) رسید و معامله بسته شد',
      tp2: 'قیمت به هدف دوم سود (TP2) رسید و معامله بسته شد',
      tp3: 'قیمت به هدف سوم سود (TP3) رسید و معامله بسته شد',
      sl: 'قیمت به حد ضرر رسید و معامله با ضرر بسته شد',
      sl_risk_free: 'معامله در حالت ریسک‌فری با ضرر صفر بسته شد',
      timeout: 'معامله به دلیل گذشت زمان زیاد بدون رسیدن به TP/SL بسته شد',
      no_position: 'تحلیل این سیگنال درست بود، اما چون تعداد پوزیشن‌های باز LIT به سقف مجاز رسیده بود، معامله واقعی برایش باز نشد',
    }[status] || '';
    const time = s.created_at ? new Date(s.created_at * 1000).toLocaleString('fa-IR', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }) : '—';
    const score = s.score ? s.score.toFixed(2) : '—';
    const scoreColor = s.score >= 0.8 ? 'var(--success)' : s.score >= 0.7 ? 'var(--info)' : 'var(--text-muted)';
    return `<tr data-sid="${s.id}" style="cursor:pointer" onclick="showLitSignalOnChart('${s.id}')">
      <td style="font-size:11px">${time}</td>
      <td><b>${s.symbol}</b></td>
      <td>${sideFa} ${s.side}</td>
      <td style="font-size:11px">${strat} <span style="color:var(--text-muted)">(${tf})</span></td>
      <td>${fmtPrice(s.entry)}</td>
      <td style="color:var(--success);font-size:11px">${fmtPrice(s.take_profit)}</td>
      <td style="color:var(--danger);font-size:11px">${fmtPrice(s.stop_loss)}</td>
      <td><b style="color:${scoreColor}">${score}</b></td>
      <td style="font-size:11px" title="${statusTitle}">${statusFa}</td>
    </tr>`;
  }).join('');
  if (pagerEl) pagerEl.innerHTML = renderPaginationControls('lit_live_signals', totalPages, page, 'changeLitLiveListPage');
}

// ═══ Show live signal on chart ═══
let _litChartCurrentSignalId = null;
let _litChartCurrentTf = '15m';

function switchLitChartTf(tf) {
  _litChartCurrentTf = tf;
  document.querySelectorAll('.lit-tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf === tf));
  if (_litChartCurrentSignalId) {
    showLitSignalOnChart(_litChartCurrentSignalId, tf);
  }
}

async function showLitSignalOnChart(signalId, timeframe) {
  const sig = litLiveSignals.find(s => s.id === signalId);
  if (!sig) return;
  const tf = timeframe || _litChartCurrentTf || '15m';
  _litChartCurrentSignalId = signalId;
  _litChartCurrentTf = tf;

  // Highlight row
  document.querySelectorAll('#lit_live_signals tr').forEach(r => { r.style.background = ''; r.style.borderRight = ''; });
  const row = document.querySelector(`#lit_live_signals tr[data-sid="${signalId}"]`);
  if (row) { row.style.background = 'rgba(99,102,241,0.12)'; row.style.borderRight = '3px solid var(--accent)'; }

  // Show detail panel
  renderLitSignalDetail(sig);

  // Fetch candles
  const container = document.getElementById('lit_live_chart_container');
  document.getElementById('lit_live_chart_title').textContent = `📈 ${sig.symbol} — ${LIT_STRATEGY_NAMES[sig.strategy]?.fa || sig.strategy || 'LIT'} (${tf})`;
  const tfSwitcher = document.getElementById('lit_live_tf_switcher');
  if (tfSwitcher) tfSwitcher.style.display = 'flex';
  document.querySelectorAll('.lit-tf-btn').forEach(b => b.classList.toggle('active', b.dataset.tf === tf));

  try {
    const resp = await fetch(`/api/lit/candles/${sig.symbol.replace('/', '-')}?timeframe=${tf}&limit=200`);
    const data = await resp.json();

    if (litLiveChart) { litLiveChart.remove(); litLiveChart = null; }
    container.innerHTML = '';

    if (!data.candles || data.candles.length === 0) {
      container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted)">داده کندلی موجود نیست</div>';
      return;
    }

    litLiveChart = LightweightCharts.createChart(container, {
      width: container.clientWidth, height: 440,
      layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
      grid: { vertLines: { color: 'rgba(48,54,61,0.5)' }, horzLines: { color: 'rgba(48,54,61,0.5)' } },
      crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: '#30363d' },
    });

    litLiveCandleSeries = litLiveChart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444', borderUpColor: '#22c55e',
      borderDownColor: '#ef4444', wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    litLiveVolumeSeries = litLiveChart.addHistogramSeries({
      priceFormat: { type: 'volume' }, priceScaleId: 'vol',
    });
    litLiveChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    const candleData = data.candles.map(c => ({
      time: Math.floor(c.time / 1000), open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    const volData = (data.volumes || []).map(v => ({
      time: Math.floor(v.time / 1000), value: v.value,
      color: v.color || 'rgba(100,100,100,0.3)',
    }));

    litLiveCandleSeries.setData(candleData);
    litLiveVolumeSeries.setData(volData);

    // Price lines: entry, TP, SL, TP2
    litLiveCandleSeries.createPriceLine({ price: sig.entry, color: '#58a6ff', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: 'ورود' });
    litLiveCandleSeries.createPriceLine({ price: sig.take_profit, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP1' });
    if (sig.tp2) litLiveCandleSeries.createPriceLine({ price: sig.tp2, color: '#a855f7', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP2' });
    litLiveCandleSeries.createPriceLine({ price: sig.stop_loss, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'SL' });

    // ─── Draw LIT Annotations (FVG, OB, Liquidity) — CLEAN version ───
    try {
      const annResp = await fetch(`/api/lit/analyze/${sig.symbol.replace('/', '-')}?timeframe=${tf}`);
      const annData = await annResp.json();
      
      // Draw the FULL SPACE of each zone (top + bottom boundary lines)
      // instead of a single midpoint line — lightweight-charts v4 has no
      // native rectangle primitive, so two matching-color boundary lines
      // is the clearest way to convey "this whole price range is the zone"
      // rather than implying it's just one flat level.
      const fvgZones = (annData.fvg_zones || sig.fvg_zones || []).slice(-2);
      fvgZones.forEach(fvg => {
        if (fvg.top && fvg.bottom && Math.abs(fvg.top - fvg.bottom) > 0) {
          const fvgColor = fvg.direction === 'bullish' ? '#8b5cf6' : '#ec4899';
          const arrow = fvg.direction === 'bullish' ? '↑' : '↓';
          litLiveCandleSeries.createPriceLine({
            price: fvg.top, color: fvgColor, lineWidth: 1, lineStyle: 2,
            axisLabelVisible: true, title: `FVG${arrow} بالا`,
          });
          litLiveCandleSeries.createPriceLine({
            price: fvg.bottom, color: fvgColor, lineWidth: 1, lineStyle: 2,
            axisLabelVisible: true, title: `FVG${arrow} پایین`,
          });
        }
      });

      // Draw max 2 Order Block zones (full space, top+bottom)
      const obZones = (annData.order_blocks || sig.order_blocks || []).slice(-2);
      obZones.forEach(ob => {
        if (ob.top && ob.bottom) {
          const obColor = ob.direction === 'bullish' ? '#06b6d4' : '#f97316';
          const arrow = ob.direction === 'bullish' ? '↑' : '↓';
          litLiveCandleSeries.createPriceLine({
            price: ob.top, color: obColor, lineWidth: 1, lineStyle: 1,
            axisLabelVisible: true, title: `OB${arrow} بالا`,
          });
          litLiveCandleSeries.createPriceLine({
            price: ob.bottom, color: obColor, lineWidth: 1, lineStyle: 1,
            axisLabelVisible: true, title: `OB${arrow} پایین`,
          });
        }
      });

      // Draw max 2 nearest liquidity levels (1 buy-side, 1 sell-side)
      const liqLevels = annData.liquidity_levels || sig.liquidity_levels || [];
      const buyLiq = liqLevels.find(l => l.side === 'buy_side');
      const sellLiq = liqLevels.find(l => l.side === 'sell_side');
      if (buyLiq && buyLiq.price > 0) {
        litLiveCandleSeries.createPriceLine({
          price: buyLiq.price, color: '#ef444460', lineWidth: 1, lineStyle: 3,
          axisLabelVisible: false, title: `▲ BSL (${buyLiq.kind || ''})`,
        });
      }
      if (sellLiq && sellLiq.price > 0) {
        litLiveCandleSeries.createPriceLine({
          price: sellLiq.price, color: '#22c55e60', lineWidth: 1, lineStyle: 3,
          axisLabelVisible: false, title: `▼ SSL (${sellLiq.kind || ''})`,
        });
      }
    } catch(annErr) { console.debug('LIT annotations:', annErr); }

    // Marker at signal time + structural markers (CHoCH, BOS, Sweep)
    const sigTime = Math.floor(sig.created_at);
    const markers = [{
      time: sigTime, position: sig.side === 'long' ? 'belowBar' : 'aboveBar',
      color: sig.side === 'long' ? '#22c55e' : '#ef4444',
      shape: sig.side === 'long' ? 'arrowUp' : 'arrowDown',
      text: `${sig.side === 'long' ? '🟢 LONG' : '🔴 SHORT'} (${sig.score?.toFixed(2) || ''})`,
    }];
    
    // Add structural markers from annotations data
    try {
      const annResp2 = await fetch(`/api/lit/analyze/${sig.symbol.replace('/', '-')}?timeframe=${tf}`);
      const annData2 = await annResp2.json();
      const structure = annData2.structure_data || {};
      const reasons = annData2.reasons || sig.reasons || [];
      
      // If we have CHOCH/BOS info, add marker
      if (structure.choch && structure.choch.index) {
        // Approximate time from index
        const approxTime = sigTime - (200 - structure.choch.index) * (tf === '15m' ? 900 : 300);
        markers.push({
          time: Math.floor(approxTime / 1000) || sigTime - 3600,
          position: 'aboveBar', color: '#a855f7', shape: 'circle',
          text: structure.choch.kind?.includes('bullish') ? '↑ CHoCH' : '↓ CHoCH',
        });
      }
    } catch(e) {}

    // Exit marker — once the outcome is resolved from real candle history
    // (see server-side _resolve_lit_outcomes), draw a circle at the exit
    // candle so it's visually clear WHEN/WHERE the position actually closed.
    const litStatusFa = { tp: 'TP', tp1: 'TP1', tp2: 'TP2', tp3: 'TP3', sl: 'SL', sl_risk_free: 'SL (ریسک‌فری)', timeout: 'تایم‌اوت' };
    let exitTime = null;
    if (sig.exit_time) {
      exitTime = Math.floor(sig.exit_time);
    }
    if (exitTime && exitTime !== sigTime) {
      const isWin = (sig.status || '').startsWith('tp');
      markers.push({
        time: exitTime, position: sig.side === 'long' ? 'aboveBar' : 'belowBar',
        color: isWin ? '#22c55e' : '#ef4444', shape: 'circle',
        text: `🏁 خروج (${litStatusFa[sig.status] || sig.status})`,
      });
    }

    try { litLiveCandleSeries.setMarkers(markers.sort((a,b) => a.time - b.time)); } catch(e) {}

    // Zoom to signal area — extend to cover the exit if the trade is resolved
    const tfSeconds = { '1m':60, '5m':300, '15m':900, '1h':3600, '4h':14400, '1d':86400 };
    const ct = tfSeconds[tf] || 900;
    litLiveChart.timeScale().setVisibleRange({
      from: sigTime - ct * 20,
      to: (exitTime ? Math.max(exitTime, sigTime) : sigTime) + ct * 10,
    });
  } catch (e) {
    container.innerHTML = `<div style="color:var(--danger);padding:20px">خطا: ${e.message}</div>`;
  }
}

function renderLitSignalDetail(sig) {
  const container = document.getElementById('lit_live_detail');
  if (!container) return;
  const strat = LIT_STRATEGY_NAMES[sig.strategy] || { fa: sig.strategy };
  const edu = LIT_STRATEGY_EDUCATION[sig.strategy] || {};
  const sideFa = sig.side === 'long' ? '🟢 Long (خرید)' : '🔴 Short (فروش)';
  const time = sig.created_at ? new Date(sig.created_at * 1000).toLocaleString('fa-IR') : '—';
  const scoreColor = sig.score >= 0.8 ? 'var(--success)' : 'var(--info)';
  const statusFaDetail = { open: '🟢 فعال', tp: '✅ سود', tp1: '✅ TP1', tp2: '✅ TP2', tp3: '✅ TP3', sl: '❌ ضرر', sl_risk_free: '🛡️ ریسک‌فری', timeout: '⏰ تایم‌اوت', no_position: '⏭️ رد شد' };
  const statusExplainDetail = {
    open: 'این سیگنال هنوز باز است و در حال ردیابی قیمت زنده است.',
    tp: 'قیمت به هدف سود رسید و معامله با سود بسته شد.', tp1: 'قیمت به TP1 رسید و معامله با سود بسته شد.',
    tp2: 'قیمت به TP2 رسید و معامله با سود بسته شد.', tp3: 'قیمت به TP3 رسید و معامله با سود بسته شد.',
    sl: 'قیمت به حد ضرر رسید و معامله با ضرر بسته شد.', sl_risk_free: 'معامله در حالت ریسک‌فری بسته شد.',
    timeout: 'معامله بدون رسیدن به TP/SL و به دلیل گذشت زمان بسته شد.',
    no_position: 'تحلیل این سیگنال درست بود، اما چون تعداد پوزیشن‌های باز LIT به سقف مجاز رسیده بود، معامله واقعی برایش باز نشد — این محدودیت مدیریت ریسک عمدی است، نه اشتباه تحلیل.',
  };
  const sigStatus = sig.status || 'open';

  let html = `
    <div style="background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.15);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-size:12px;color:var(--text-muted)">${sig.symbol} | ${time}</div>
      <div style="font-size:16px;font-weight:bold;margin-top:4px">${sideFa}</div>
      <div style="font-size:13px;margin-top:2px">${strat.fa} | امتیاز: <b style="color:${scoreColor}">${sig.score?.toFixed(2)}</b></div>
      <div style="font-size:12px;margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.08)">
        وضعیت: <b>${statusFaDetail[sigStatus] || sigStatus}</b>
        <div style="color:var(--text-muted);margin-top:2px">${statusExplainDetail[sigStatus] || ''}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:10px">
      <div style="background:rgba(88,166,255,0.08);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">📍 ورود</div>
        <div style="font-weight:bold">${fmtPrice(sig.entry)}</div>
      </div>
      <div style="background:rgba(239,68,68,0.08);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">🛑 SL</div>
        <div style="font-weight:bold;color:var(--danger)">${fmtPrice(sig.stop_loss)}</div>
      </div>
      <div style="background:rgba(34,197,94,0.08);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">🎯 TP1</div>
        <div style="font-weight:bold;color:var(--success)">${fmtPrice(sig.take_profit)}</div>
      </div>
      <div style="background:rgba(168,85,247,0.08);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">🎯 TP2</div>
        <div style="font-weight:bold;color:#a855f7">${sig.tp2 ? fmtPrice(sig.tp2) : '—'}</div>
      </div>
    </div>`;

  if (sig.rationale) {
    html += `<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">📝 تحلیل ورود</div>
      <div style="font-size:13px;line-height:1.8">${sig.rationale}</div>
    </div>`;
  }

  if (edu.title) {
    html += `<div style="background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.15);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-weight:bold;margin-bottom:6px">${edu.title}</div>
      <div style="font-size:12px;line-height:1.8;margin-bottom:8px">${edu.what}</div>
      <div style="font-size:12px;font-weight:bold;margin-bottom:4px">نحوه شناسایی:</div>
      <ol style="font-size:12px;line-height:2;padding-right:16px;margin:0 0 8px 0">${edu.how.map(h => `<li>${h}</li>`).join('')}</ol>
      <div style="background:rgba(245,158,11,0.1);border-radius:4px;padding:6px;font-size:12px">💡 ${edu.tip}</div>
    </div>`;
  }

  container.innerHTML = html;
}

// ═══ Education cards ═══
const LIT_REAL_SETUPS = ['sweep_reversal', 'inducement_continuation', 'range_expansion'];

function renderLitEduCards() {
  const container = document.getElementById('lit_edu_cards');
  if (!container) return;
  // Only show the 3 setups the engine actually produces — the legacy
  // key(s) kept in LIT_STRATEGY_EDUCATION are for name-lookup fallback
  // only and would just confuse the education tab if shown as cards.
  container.innerHTML = LIT_REAL_SETUPS.map(key => [key, LIT_STRATEGY_EDUCATION[key]]).map(([key, edu]) => {
    return `<div class="settings-card" style="border-top:3px solid var(--accent)">
      <div style="font-size:16px;font-weight:bold;margin-bottom:8px">${edu.title}</div>
      <div style="font-size:13px;line-height:1.8;margin-bottom:12px;color:var(--text-muted)">${edu.what}</div>
      <div style="font-size:13px;font-weight:bold;margin-bottom:6px">نحوه شناسایی:</div>
      <ol style="font-size:12px;line-height:2.2;padding-right:16px;margin:0 0 12px 0">${edu.how.map(h => `<li>${h}</li>`).join('')}</ol>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px">
        <div style="background:rgba(88,166,255,0.08);border-radius:6px;padding:8px;text-align:center">
          <div style="font-size:10px;color:var(--text-muted)">📍 ورود</div>
          <div style="font-size:11px">${edu.entry}</div>
        </div>
        <div style="background:rgba(239,68,68,0.08);border-radius:6px;padding:8px;text-align:center">
          <div style="font-size:10px;color:var(--text-muted)">🛑 SL</div>
          <div style="font-size:11px">${edu.sl}</div>
        </div>
        <div style="background:rgba(34,197,94,0.08);border-radius:6px;padding:8px;text-align:center">
          <div style="font-size:10px;color:var(--text-muted)">🎯 TP</div>
          <div style="font-size:11px">${edu.tp}</div>
        </div>
      </div>
      <div style="background:rgba(245,158,11,0.1);border-radius:6px;padding:8px;font-size:12px">💡 ${edu.tip}</div>
    </div>`;
  }).join('');
}

// ═══ Backtest ═══
async function runLitBacktest() {
  const symbol = document.getElementById('lit_bt_symbol').value;
  const tf = document.getElementById('lit_bt_tf').value;
  const limit = document.getElementById('lit_bt_limit').value;
  const btn = document.getElementById('lit_bt_run');
  const loading = document.getElementById('lit_bt_loading');
  const statsEl = document.getElementById('lit_bt_stats');
  const chartSec = document.getElementById('lit_bt_chart_section');

  btn.disabled = true; btn.textContent = '⏳ در حال بک‌تست...';
  loading.style.display = 'block'; statsEl.style.display = 'none'; chartSec.style.display = 'none';

  try {
    const symPath = symbol.replace('/', '-');
    const resp = await fetch(`/api/lit/backtest/${symPath}?timeframe=${tf}&limit=${limit}`);
    const data = await resp.json();
    if (data.error) { alert('خطا: ' + data.error); return; }
    window._litBacktestData = data;
    window._litBacktestTF = tf;
    statsEl.style.display = 'block'; chartSec.style.display = 'block';
    renderLitStats(data);
    renderLitTradeList(data);
    requestAnimationFrame(() => renderLitChart(data, tf));
  } catch (e) {
    alert('خطا در بک‌تست: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '▶️ اجرای بک‌تست'; loading.style.display = 'none';
  }
}

function renderLitStats(data) {
  document.getElementById('lit_stat_wr').textContent = data.win_rate + '%';
  document.getElementById('lit_stat_wr').style.color = data.win_rate >= 50 ? 'var(--success)' : 'var(--danger)';
  document.getElementById('lit_stat_total').textContent = data.total_trades;
  const p = document.getElementById('lit_stat_pnl');
  p.textContent = (data.total_pnl_pct >= 0 ? '+' : '') + data.total_pnl_pct + '%';
  p.style.color = data.total_pnl_pct >= 0 ? 'var(--success)' : 'var(--danger)';
  const r = document.getElementById('lit_stat_avg_r');
  r.textContent = data.avg_r_multiple + 'R';
  r.style.color = data.avg_r_multiple >= 0 ? 'var(--success)' : 'var(--danger)';
  document.getElementById('lit_stat_best').textContent = '+' + data.best_trade + '%';
  document.getElementById('lit_stat_worst').textContent = data.worst_trade + '%';

  const stratEl = document.getElementById('lit_strategy_stats');
  if (data.strategy_stats && Object.keys(data.strategy_stats).length > 0) {
    let html = '<table class="table" style="font-size:12px"><thead><tr><th>استراتژی</th><th>تعداد</th><th>برد</th><th>Win Rate</th><th>میانگین سود</th><th>میانگین R</th></tr></thead><tbody>';
    for (const [name, st] of Object.entries(data.strategy_stats)) {
      const nameFa = LIT_STRATEGY_NAMES[name]?.fa || name;
      html += `<tr><td><b>${nameFa}</b></td><td>${st.total}</td><td>${st.wins}</td>
        <td style="color:${st.win_rate >= 50 ? 'var(--success)' : 'var(--danger)'}">${st.win_rate}%</td>
        <td style="color:${st.avg_pnl >= 0 ? 'var(--success)' : 'var(--danger)'}">${st.avg_pnl}%</td>
        <td style="color:${st.avg_r >= 0 ? 'var(--success)' : 'var(--danger)'}">${st.avg_r}R</td></tr>`;
    }
    html += '</tbody></table>';
    stratEl.innerHTML = html;
  }
}

function renderLitTradeList(data) {
  const tbody = document.getElementById('lit_bt_trades');
  if (!data.trades || data.trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--text-muted)">تریدی یافت نشد</td></tr>';
    return;
  }
  tbody.innerHTML = data.trades.map((tr, i) => {
    const sideFa = tr.side === 'long' ? '🟢' : '🔴';
    const pnlColor = tr.pnl_pct >= 0 ? 'var(--success)' : 'var(--danger)';
    const exitFa = { tp1:'✅ TP1', tp2:'✅ TP2', sl:'❌ SL', sl_risk_free:'🛡 ریسک‌فری', timeout:'⏰ تایم‌اوت', trailing:'🔄 Trail', backtest_end:'📊 پایان' }[tr.exit_reason] || tr.exit_reason;
    const stratFa = LIT_STRATEGY_NAMES[tr.strategy]?.fa || tr.strategy;
    const entryT = tr.entry_time ? new Date(tr.entry_time).toLocaleString('fa-IR', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }) : '—';
    const exitT = tr.exit_time ? new Date(tr.exit_time).toLocaleString('fa-IR', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }) : '—';
    return `<tr data-idx="${i}" style="cursor:pointer" onclick="showLitTradeDetail(${i});litShowTradeOnChart(${i})">
      <td>${i + 1}</td><td>${sideFa}</td><td style="font-size:11px">${stratFa}</td>
      <td style="font-size:10px;white-space:nowrap">${entryT}</td>
      <td>${fmtPrice(tr.entry_price)}</td>
      <td style="font-size:10px;white-space:nowrap">${exitT}</td>
      <td>${tr.exit_price ? fmtPrice(tr.exit_price) : '—'}</td>
      <td style="color:var(--danger);font-size:11px">${fmtPrice(tr.stop_loss)}</td>
      <td style="color:${pnlColor};font-weight:bold">${tr.pnl_pct >= 0 ? '+' : ''}${tr.pnl_pct}%</td>
      <td style="color:${pnlColor}">${tr.r_multiple}R</td><td style="font-size:11px">${exitFa}</td></tr>`;
  }).join('');
}

function showLitTradeDetail(idx) {
  const data = window._litBacktestData;
  if (!data || !data.trades[idx]) return;
  const tr = data.trades[idx];
  const title = document.getElementById('lit_edu_title');
  const content = document.getElementById('lit_edu_content');
  const sideFa = tr.side === 'long' ? '🟢 Long (خرید)' : '🔴 Short (فروش)';
  const stratFa = LIT_STRATEGY_NAMES[tr.strategy]?.fa || tr.strategy;
  const edu = LIT_STRATEGY_EDUCATION[tr.strategy] || {};
  const pnlColor = tr.pnl_pct >= 0 ? 'var(--success)' : 'var(--danger)';
  const exitFa = { tp1:'✅ TP1 زده شد', tp2:'✅ TP2 زده شد — سود کامل', sl:'❌ حد ضرر زده شد', sl_risk_free:'🛡 ریسک‌فری — ضرر صفر', timeout:'⏰ تایم‌اوت', backtest_end:'📊 پایان بک‌تست' }[tr.exit_reason] || tr.exit_reason;
  const candles_held = tr.exit_candle_idx && tr.entry_candle_idx ? tr.exit_candle_idx - tr.entry_candle_idx : '?';

  title.innerHTML = `📚 ترید #${idx + 1} — ${tr.symbol}`;
  let html = `
    <div style="background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.15);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-size:13px;color:var(--text-muted)">📍 ورود</div>
      <div style="font-size:18px;font-weight:bold">${fmtPrice(tr.entry_price)}</div>
      <div style="font-size:13px">${sideFa} — ${stratFa}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px">
      <div style="background:rgba(34,197,94,0.08);border:1px solid rgba(34,197,94,0.2);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">TP1</div>
        <div style="font-weight:bold;color:var(--success)">${fmtPrice(tr.take_profit_1)}</div></div>
      <div style="background:rgba(168,85,247,0.08);border:1px solid rgba(168,85,247,0.2);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">TP2</div>
        <div style="font-weight:bold;color:#a855f7">${fmtPrice(tr.take_profit_2)}</div></div>
      <div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:6px;padding:8px;text-align:center">
        <div style="font-size:10px;color:var(--text-muted)">SL</div>
        <div style="font-weight:bold;color:var(--danger)">${fmtPrice(tr.stop_loss)}</div></div>
    </div>
    <div style="background:${tr.pnl_pct >= 0 ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)'};border:1px solid ${tr.pnl_pct >= 0 ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'};border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between">
        <div><div style="font-size:12px;color:var(--text-muted)">نتیجه</div><div style="font-size:22px;font-weight:bold;color:${pnlColor}">${tr.pnl_pct >= 0 ? '+' : ''}${tr.pnl_pct}%</div></div>
        <div style="text-align:left"><div style="font-size:12px;color:var(--text-muted)">R-Multiple</div><div style="font-size:22px;font-weight:bold;color:${pnlColor}">${tr.r_multiple}R</div></div>
      </div>
      <div style="margin-top:8px;font-size:13px">${exitFa}</div>
      <div style="margin-top:4px;font-size:12px;color:var(--text-muted)">⏱️ مدت: ${candles_held} کندل</div>
    </div>`;

  if (edu.title) {
    html += `<div style="background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.15);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-weight:bold;margin-bottom:6px">${edu.title}</div>
      <div style="font-size:12px;line-height:1.8;margin-bottom:6px">${edu.what}</div>
      <ol style="font-size:12px;line-height:2;padding-right:16px;margin:0">${edu.how.map(h => `<li>${h}</li>`).join('')}</ol>
      <div style="margin-top:8px;font-size:12px"><b>ورود:</b> ${edu.entry} | <b>SL:</b> ${edu.sl} | <b>TP:</b> ${edu.tp}</div>
      <div style="margin-top:6px;background:rgba(245,158,11,0.1);border-radius:4px;padding:6px;font-size:12px">💡 ${edu.tip}</div>
    </div>`;
  }

  if (tr.entry_reasoning) {
    html += `<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">📝 دلیل ورود</div>
      <div style="font-size:13px;line-height:1.8">${tr.entry_reasoning}</div></div>`;
  }

  html += `<div style="background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.15);border-radius:8px;padding:12px">
    <div style="font-size:13px;font-weight:bold;margin-bottom:6px">💡 درس‌های این ترید</div>
    <ul style="font-size:12px;line-height:2;padding-right:16px;margin:0">
      ${tr.pnl_pct >= 0
        ? `<li style="color:var(--success)">✅ ترید سودده — استراتژی درست عمل کرد</li>`
        : `<li style="color:var(--danger)">❌ ترید ضررده — بازار برخلاف تحلیل حرکت کرد</li>
           <li>SL از قبل مشخص بود — ریسک مدیریت شد ✅</li>`}
      <li>⏱️ مدت ترید: ${candles_held} کندل</li>
      <li>📊 امتیاز سیگنال: ${tr.score || '—'}</li>
    </ul></div>`;

  content.innerHTML = html;

  // Highlight row
  document.querySelectorAll('#lit_bt_trades tr').forEach(r => { r.style.background = ''; r.style.borderRight = ''; });
  const selRow = document.querySelector(`#lit_bt_trades tr[data-idx="${idx}"]`);
  if (selRow) { selRow.style.background = 'rgba(99,102,241,0.12)'; selRow.style.borderRight = '3px solid var(--accent)'; }
}

// ═══ Backtest Chart ═══
let _litPriceLines = [];  // store price line objects for cleanup

function renderLitChart(data, tf) {
  const container = document.getElementById('lit_chart_container');
  if (!container) return;
  if (litChart) { litChart.remove(); litChart = null; }
  container.innerHTML = '';
  _litPriceLines = [];

  if (!data.candles || data.candles.length === 0) {
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted)">داده‌ای موجود نیست</div>';
    return;
  }

  const w = container.clientWidth || 600;
  litChart = LightweightCharts.createChart(container, {
    width: w, height: 520,
    layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
    grid: { vertLines: { color: 'rgba(48,54,61,0.5)' }, horzLines: { color: 'rgba(48,54,61,0.5)' } },
    crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: '#30363d' },
  });

  litCandleSeries = litChart.addCandlestickSeries({
    upColor: '#22c55e', downColor: '#ef4444', borderUpColor: '#22c55e',
    borderDownColor: '#ef4444', wickUpColor: '#22c55e', wickDownColor: '#ef4444',
  });

  litVolumeSeries = litChart.addHistogramSeries({
    priceFormat: { type: 'volume' }, priceScaleId: 'vol',
  });
  litChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  const candleData = data.candles.map(c => ({
    time: Math.floor(c.time / 1000), open: c.open, high: c.high, low: c.low, close: c.close,
  }));
  const volData = (data.volumes || []).map(v => ({
    time: Math.floor(v.time / 1000), value: v.value,
    color: v.color || 'rgba(100,100,100,0.3)',
  }));

  litCandleSeries.setData(candleData);
  litVolumeSeries.setData(volData);

  litChart.timeScale().fitContent();
  window._litCurrentTradeIdx = -1;
}

async function litShowTradeOnChart(idx) {
  const data = window._litBacktestData;
  const tf = window._litBacktestTF || '1h';
  const tr = data?.trades?.[idx];
  if (!tr) return;

  // If chart doesn't exist, try to render it first
  if (!litChart || !litCandleSeries) {
    if (data) renderLitChart(data, tf);
    if (!litChart || !litCandleSeries) return;
  }

  window._litCurrentTradeIdx = idx;

  // Remove old price lines by storing references
  _litPriceLines.forEach(pl => { try { litCandleSeries.removePriceLine(pl); } catch(e) {} });
  _litPriceLines = [];

  const entryTime = Math.floor(tr.entry_time / 1000);
  const exitTime = tr.exit_time ? Math.floor(tr.exit_time / 1000) : null;

  // Markers — must be sorted by time
  const markers = [{
    time: entryTime, position: tr.side === 'long' ? 'belowBar' : 'aboveBar',
    color: tr.side === 'long' ? '#22c55e' : '#ef4444',
    shape: tr.side === 'long' ? 'arrowUp' : 'arrowDown',
    text: 'ورود',
  }];
  if (exitTime && exitTime !== entryTime) {
    markers.push({
      time: exitTime, position: tr.side === 'long' ? 'aboveBar' : 'belowBar',
      color: tr.pnl_pct >= 0 ? '#22c55e' : '#ef4444', shape: 'circle',
      text: 'خروج (' + (tr.pnl_pct >= 0 ? '+' : '') + tr.pnl_pct + '%)',
    });
  }
  markers.sort((a, b) => a.time - b.time);
  try { litCandleSeries.setMarkers(markers); } catch(e) { console.warn('setMarkers error:', e); }

  // Price lines — store references for cleanup
  try {
    _litPriceLines.push(litCandleSeries.createPriceLine({ price: tr.entry_price, color: '#58a6ff', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: 'ورود' }));
    _litPriceLines.push(litCandleSeries.createPriceLine({ price: tr.take_profit_1, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP1' }));
    _litPriceLines.push(litCandleSeries.createPriceLine({ price: tr.take_profit_2, color: '#a855f7', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP2' }));
    _litPriceLines.push(litCandleSeries.createPriceLine({ price: tr.stop_loss, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'SL' }));
  } catch(e) { console.warn('createPriceLine error:', e); }

  // ─── Draw LIT Annotations on Backtest Chart (clean — max 6 lines) ───
  try {
    const symPath = (tr.symbol || data.symbol || '').replace('/', '-');
    const annResp = await fetch(`/api/lit/analyze/${symPath}?timeframe=${tf}`);
    const annData = await annResp.json();

    // Max 2 FVG zones — draw the FULL space (top + bottom), not just a
    // midpoint line, so the actual imbalance range is visible on the chart.
    const fvgZones = (annData.fvg_zones || []).slice(-2);
    fvgZones.forEach(fvg => {
      if (fvg.top && fvg.bottom) {
        const fvgColor = fvg.direction === 'bullish' ? '#8b5cf6' : '#ec4899';
        const arrow = fvg.direction === 'bullish' ? '↑' : '↓';
        _litPriceLines.push(litCandleSeries.createPriceLine({
          price: fvg.top, color: fvgColor, lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: `FVG${arrow} بالا`,
        }));
        _litPriceLines.push(litCandleSeries.createPriceLine({
          price: fvg.bottom, color: fvgColor, lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: `FVG${arrow} پایین`,
        }));
      }
    });

    // Max 1 OB zone (full space)
    const obZones = (annData.order_blocks || []).slice(-1);
    obZones.forEach(ob => {
      if (ob.top && ob.bottom) {
        const obColor = ob.direction === 'bullish' ? '#06b6d4' : '#f97316';
        const arrow = ob.direction === 'bullish' ? '↑' : '↓';
        _litPriceLines.push(litCandleSeries.createPriceLine({
          price: ob.top, color: obColor, lineWidth: 1, lineStyle: 1,
          axisLabelVisible: true, title: `OB${arrow} بالا`,
        }));
        _litPriceLines.push(litCandleSeries.createPriceLine({
          price: ob.bottom, color: obColor, lineWidth: 1, lineStyle: 1,
          axisLabelVisible: true, title: `OB${arrow} پایین`,
        }));
      }
    });

    // 1 buy-side + 1 sell-side liquidity
    const liqLevels = annData.liquidity_levels || [];
    const buyL = liqLevels.find(l => l.side === 'buy_side');
    const sellL = liqLevels.find(l => l.side === 'sell_side');
    if (buyL && buyL.price > 0) {
      _litPriceLines.push(litCandleSeries.createPriceLine({
        price: buyL.price, color: '#ef444450', lineWidth: 1, lineStyle: 3,
        axisLabelVisible: false, title: `▲ BSL`,
      }));
    }
    if (sellL && sellL.price > 0) {
      _litPriceLines.push(litCandleSeries.createPriceLine({
        price: sellL.price, color: '#22c55e50', lineWidth: 1, lineStyle: 3,
        axisLabelVisible: false, title: `▼ SSL`,
      }));
    }
  } catch(annErr) { console.debug('BT annotations:', annErr); }

  // Zoom to trade range — use actual timeframe
  const tfSeconds = { '1m':60, '5m':300, '15m':900, '1h':3600, '4h':14400, '1d':86400 };
  const ct = tfSeconds[tf] || 3600;
  try {
    litChart.timeScale().setVisibleRange({
      from: entryTime - ct * 5,
      to: (exitTime || entryTime) + ct * 8,
    });
  } catch(e) {
    console.warn('setVisibleRange error:', e);
    litChart.timeScale().fitContent();
  }
}
