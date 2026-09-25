/* K-DayTrader 그래픽 앱 프런트엔드 (의존성 없음) */
'use strict';
const $ = id => document.getElementById(id);
const fmtWon = v => (v == null ? '-' : Math.round(v).toLocaleString('ko-KR'));
const signed = v => (v == null ? '-' : (v >= 0 ? '+' : '') + Math.round(v).toLocaleString('ko-KR'));
const cls = v => (v > 0 ? 'up' : v < 0 ? 'down' : '');
const pct = (v, d = 2) => (v == null || isNaN(v) ? '<span class="muted">-</span>' : `<span class="${cls(v)}">${v >= 0 ? '+' : ''}${(+v).toFixed(d)}%</span>`);
const num = (v, d = 0) => (v == null || isNaN(v) ? '-' : (+v).toFixed(d));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let d = {};
  try { d = await r.json(); } catch (e) { d = { error: '응답 파싱 실패' }; }
  if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = kind; el.textContent = msg; $('toast').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 6000 : 3500);
}
let modalCb = null;
function openModal(title, body, onOk) { $('modalTitle').textContent = title; $('modalBody').textContent = body; modalCb = onOk; $('modal').style.display = 'flex'; }
function closeModal() { $('modal').style.display = 'none'; modalCb = null; }
$('modalOk').onclick = () => { const cb = modalCb; closeModal(); if (cb) cb(); };
function toggleTheme() { const r = document.documentElement; const next = (r.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark' ? 'light' : 'dark'; r.setAttribute('data-theme', next); try { localStorage.setItem('theme', next); } catch (e) {} redrawAll(); }
try { const t = localStorage.getItem('theme'); if (t) document.documentElement.setAttribute('data-theme', t); } catch (e) {}
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ---------------- 라우팅 ---------------- */
let page = 'home';
document.querySelectorAll('.side nav button').forEach(b => b.addEventListener('click', () => showPage(b.dataset.page)));
function showPage(p) {
  page = p;
  document.querySelectorAll('.side nav button').forEach(b => b.classList.toggle('active', b.dataset.page === p));
  document.querySelectorAll('.page').forEach(s => s.classList.toggle('active', s.id === 'page-' + p));
  try { localStorage.setItem('page', p); } catch (e) {}
  if (p === 'settings') loadConfig();
  if (p === 'news') loadNews(false);
  if (p === 'chart') loadChart(true);
  if (p === 'backtest') pollBacktest();
  if (p === 'logs') pollLogs();
  if (p === 'dash' || p === 'reco') pollState();
  if (p === 'perf') loadPerf();
}

/* ---------------- 상태 폴링 ---------------- */
let status = {}, state = {}, lastStateAt = 0;
async function pollStatus() {
  try {
    status = await api('/api/status');
    $('ver').textContent = 'v' + status.version;
    $('clock').textContent = status.clock;
    const pill = $('enginePill');
    pill.className = 'engine-pill ' + (status.running ? 'on' : status.last_error ? 'err' : '');
    $('engineText').textContent = status.running ? `실행 중 · ${status.run_opts.feed}/${status.run_opts.mode}${status.run_opts.signal_only ? ' (시그널만)' : ''}` : '엔진 대기';
    $('stopBtn').style.display = status.running ? '' : 'none';
    const mc = $('marketChip');
    if (status.market_open) { mc.textContent = '정규장 진행 중'; mc.className = 'chip ok'; }
    else { const s = status.seconds_until_open; mc.textContent = `장 마감 · 개장까지 ${Math.floor(s / 3600)}시간 ${Math.floor((s % 3600) / 60)}분`; mc.className = 'chip'; }
    $('kisState').textContent = status.kis_configured ? '앱키 설정됨' : '미설정 (설정 탭에서 입력)';
    $('kisState').className = 'chip ' + (status.kis_configured ? 'ok' : 'warn');
    const al = $('homeAlert');
    if (status.last_error && !status.running) { al.style.display = ''; al.textContent = '마지막 오류: ' + status.last_error; } else al.style.display = 'none';
    $('sysinfo').textContent = `설정 파일: ${status.config_path} · 관심종목 ${status.watchlist_count}개`;
    const ca = $('cfgAlert'); if (status.config_error) { ca.style.display = ''; ca.textContent = '⚠ ' + status.config_error + ' — 기본 설정으로 실행 중입니다. 설정 탭에서 저장하면 파일이 다시 만들어집니다.'; } else ca.style.display = 'none';
    $('dashStatus').textContent = status.running ? status.engine_status : '엔진이 실행 중이 아닙니다. 홈에서 시작하세요.';
  } catch (e) { $('engineText').textContent = '서버 연결 끊김'; $('enginePill').className = 'engine-pill err'; }
}
async function pollState() {
  if (!status.running && !state.running) return;
  try { state = await api('/api/state'); lastStateAt = Date.now(); renderTop(); if (page === 'dash') renderDash(); if (page === 'reco') renderReco(); } catch (e) {}
}
function renderTop() {
  if (!state || state.equity == null) return;
  $('kEquity').textContent = fmtWon(state.equity) + '원';
  $('kRealized').innerHTML = `<span class="${cls(state.realized)}">${signed(state.realized)}</span>`;
  $('kUnreal').innerHTML = `<span class="${cls(state.unrealized)}">${signed(state.unrealized)}</span>`;
  $('kDay').innerHTML = pct(state.day_pct);
  $('kTrades').textContent = `${state.trades} (${state.wins}/${state.losses})`;
}

/* ---------------- 엔진 제어 ---------------- */
async function startEngine(opts) {
  try {
    const r = await api('/api/start', opts);
    toast(`엔진 시작: ${r.run.feed} / ${r.run.mode} · ${r.run.codes.length}종목`, 'ok');
    setTimeout(pollStatus, 300); showPage('dash');
  } catch (e) { toast(e.message, 'err'); }
}
async function startKis() {
  if (!status.kis_configured) { toast('먼저 설정 탭에서 한국투자증권 앱키·시크릿·계좌를 입력하세요.', 'err'); showPage('settings'); return; }
  const cfg = await api('/api/config').catch(() => ({}));
  const paper = !(cfg.kis && cfg.kis.paper === false);
  const go = () => startEngine({ feed: 'kis', mode: 'live', news: true });
  if (paper) go(); else openModal('실계좌 주문 확인', '실계좌로 실제 주문이 전송됩니다. 손실이 발생할 수 있습니다.\n계속할까요?', go);
}
async function stopEngine() { try { const r = await api('/api/stop', {}); if (r.ok) toast('엔진을 정지했습니다.'); else toast('정지 요청을 보냈지만 엔진이 아직 종료 중입니다. 잠시 후 상태를 확인하세요.', 'err'); state = {}; chartData = null; const sel = $('chartCode'); sel.innerHTML = ''; sel.dataset.codes = ''; pollStatus(); } catch (e) { toast(e.message, 'err'); } }
async function toggleAuto() { try { const r = await api('/api/toggle_auto', {}); toast('자동매매 ' + (r.auto_trade ? 'ON' : 'OFF')); pollState(); } catch (e) { toast(e.message, 'err'); } }
function closeAll() { openModal('전량 청산', '보유 중인 모든 포지션을 현재가로 청산합니다.', async () => { try { const r = await api('/api/close_all', {}); toast(`${r.closed}개 포지션 청산 요청`, 'ok'); } catch (e) { toast(e.message, 'err'); } }); }

/* ---------------- 연결 진단 ---------------- */
async function runDiag() {
  const btn = $('diagBtn'); btn.disabled = true; btn.textContent = '확인 중…';
  $('diagBody').innerHTML = '<tr><td colspan="4" class="muted">각 항목을 확인하는 중입니다 (최대 10초)…</td></tr>';
  try {
    const d = await api('/api/diagnose');
    $('diagBody').innerHTML = d.results.map(r => `<tr><td>${esc(r.label)}</td><td>${r.ok === null ? '<span class="chip">미설정</span>' : r.ok ? '<span class="chip ok">연결됨</span>' : '<span class="chip err">실패</span>'}</td><td>${r.ms ? r.ms + 'ms' : '-'}</td><td class="txt">${esc(r.detail)}</td></tr>`).join('');
    const h = $('diagHint'); if (d.hint) { h.style.display = ''; h.textContent = d.hint; } else h.style.display = 'none';
    toast(`연결 확인: 정상 ${d.ok} · 실패 ${d.fail}`, d.core_ok ? 'ok' : 'err');
  } catch (e) { $('diagBody').innerHTML = `<tr><td colspan="4" class="up">${esc(e.message)}</td></tr>`; }
  btn.disabled = false; btn.textContent = '🔌 지금 확인';
}

/* ---------------- 대시보드 ---------------- */
function renderDash() {
  const s = state; if (!s || !s.watchlist) return;
  const wc = $('warmChip');
  if (s.warming && s.warming.length) { wc.style.display = ''; const mn = Math.min(...s.warming.map(c => s.bars[c] || 0)); wc.textContent = `⏳ 캔들 누적 중 ${mn}/${s.min_bars} (${s.warming.length}종목) — 과거 분봉이 없으면 시그널까지 시간이 걸립니다`; wc.className = 'chip warn'; } else wc.style.display = 'none';
  const fc = $('feedChip'), fh = s.feed_health || {};
  if (fh.consecutive_failures >= 3) { fc.style.display = ''; fc.className = 'chip err'; fc.textContent = `시세 수신 실패 ${fh.consecutive_failures}회 연속 · ${fh.last_error || ''}`; }
  else if (fh.market_status) { fc.style.display = ''; fc.className = 'chip ' + (fh.market_status === 'OPEN' ? 'ok' : ''); fc.textContent = `시장 ${fh.market_status} · 마지막 틱 ${s.last_tick || '-'}`; }
  else fc.style.display = 'none';
  $('autoBtn').textContent = '자동매매 ' + (s.auto_trade ? 'ON' : 'OFF'); $('autoBtn').className = 'btn sm ' + (s.auto_trade ? 'on' : '');
  $('haltChip').style.display = s.halted ? '' : 'none'; $('haltChip').textContent = '⚠ ' + s.halted;
  $('wlCount').textContent = s.watchlist.length + '종목';
  $('wl').innerHTML = s.watchlist.map(r => {
    const sc = r.score || 0, bw = Math.min(100, Math.abs(sc)), col = sc >= 0 ? css('--up') : css('--down');
    const sig = r.has_position ? 'POS' : r.action, label = r.has_position ? '보유' + (r.action !== 'HOLD' ? ' ' + r.action : '') : r.action;
    return `<tr><td>${esc(r.name)}<div class="muted small">${r.code}</div></td><td>${fmtWon(r.price)}</td><td>${pct(r.change_pct)}</td><td>${r.vwap ? fmtWon(r.vwap) : '-'}</td><td>${num(r.rsi)}</td><td>${num(r.adx)}</td><td>${r.vol_ratio != null ? 'x' + num(r.vol_ratio, 1) : '-'}</td><td>${r.regime}</td><td>${num(r.beta, 2)}</td><td><span class="bar"><i style="width:${bw}%;background:${col};${sc < 0 ? 'right:0' : 'left:0'}"></i></span> <b class="${cls(sc)}">${sc >= 0 ? '+' : ''}${sc.toFixed(0)}</b></td><td><span class="sig ${sig}">${label}</span></td><td class="txt">${esc(r.reasons.join(', '))}</td></tr>`;
  }).join('');
  $('pos').innerHTML = s.positions.length ? s.positions.map(p => `<tr><td>${esc(p.name)}</td><td>${p.qty.toLocaleString()}</td><td>${fmtWon(p.avg_price)}</td><td>${fmtWon(p.price)}</td><td class="${cls(p.pnl)}">${signed(p.pnl)}</td><td>${pct(p.pnl_pct)}</td><td>${num(p.r, 1)}</td><td>${fmtWon(p.stop)}</td><td>${fmtWon(p.take_profit)}</td><td>${p.entry}</td></tr>`).join('') : '<tr><td colspan="10" class="muted">보유 없음</td></tr>';
  $('evlog').innerHTML = s.events.map(e => `<tr><td>${e.ts}</td><td>${esc(e.name)}</td><td><span class="sig ${e.side}">${e.side}</span></td><td>${e.qty.toLocaleString()}</td><td>${fmtWon(e.price)}</td><td class="${e.pnl == null ? '' : cls(e.pnl)}">${e.pnl == null ? '-' : signed(e.pnl)}</td><td class="txt">${esc(e.reason)}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">아직 체결 없음</td></tr>';
  $('idx').innerHTML = s.indices.map(i => `<div><b>${i.name}</b>${(+i.value).toLocaleString('ko-KR', { maximumFractionDigits: 2 })} ${pct(i.change_pct)} <span class="muted">10분 ${num(i.momentum, 2)}%</span></div>`).join('') || '<span class="muted">지수 데이터 대기 중</span>';
  $('sec').innerHTML = s.sectors.map(x => `<tr><td>${x.sector}</td><td>${pct(x.rs)}</td><td class="${cls(x.sentiment)}">${num(x.sentiment, 2)}</td><td>${x.count}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">데이터 없음</td></tr>';
  $('dashNews').innerHTML = s.news.map(n => `<li><time>${n.ts}</time><span class="sent ${cls(n.sentiment)}">${num(n.sentiment, 1)}</span><span>${n.tags.map(t => `<span class="tag">${esc(t)}</span>`).join(' ')} ${esc(n.title)}</span></li>`).join('') || '<li class="muted">뉴스 수집 대기 중</li>';
  $('kelly').textContent = 'x' + num(s.kelly_scale, 2); $('mktBias').innerHTML = `<span class="${cls(s.market_bias)}">${num(s.market_bias, 2)}</span>`;
  $('sigLogMini').innerHTML = (s.signal_log || []).slice(0, 12).map(e => `<tr><td>${e.ts}</td><td>${esc(e.name)}</td><td>${sigBadge(e.kind)}</td><td class="${cls(e.score)}">${e.score >= 0 ? '+' : ''}${e.score.toFixed(0)}</td><td>${fmtWon(e.price)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">아직 시그널 없음</td></tr>';
  drawLine($('eqCanvas'), s.equity_history.map(e => e[1]), { base: s.equity_history.length ? s.equity_history[0][1] : null, labels: s.equity_history.map(e => e[0]) });
}

/* ---------------- 추천 종목 ---------------- */
const KIND_CLS = { '매수': 'BUY', '매수대기': 'POS', '청산': 'SELL', '매도': 'SELL', '주의': 'POS', '관심': 'POS', '보유': 'POS' };
function sigBadge(kind) { return `<span class="sig ${KIND_CLS[kind] || 'HOLD'}">${esc(kind)}</span>`; }
function recoCard(r, kind) {
  const plan = r.suggest || {};
  const planHtml = kind === 'sell'
    ? (r.has_position ? `<b>보유 중 → 청산 검토</b>` : `신규 진입 금지<br>손절·관망 권장`)
    : `수량 <b>${plan.qty || 0}주</b> (위험 ${fmtWon(plan.risk_won)}원)<br>손절 <b class="down">${fmtWon(plan.stop)}</b> (-${num(plan.risk_pct, 2)}%)<br>목표 <b class="up">${fmtWon(plan.take_profit)}</b> (+${num(plan.reward_pct, 2)}%)`;
  return `<div class="reco ${kind}" onclick="openChart('${r.code}')" title="차트 보기"><div class="score ${cls(r.score)}">${r.score >= 0 ? '+' : ''}${r.score.toFixed(0)}<small>${esc(r.label)} · ${esc(r.strength)}</small></div><div class="body"><h4>${esc(r.name)}<span>${r.code}</span> <span class="${cls(r.change_pct)}" style="font-size:13px">${fmtWon(r.price)} (${r.change_pct >= 0 ? '+' : ''}${num(r.change_pct, 2)}%)</span> <span class="tag">${r.regime === 'trend' ? '추세장' : r.regime === 'range' ? '횡보장' : '-'}</span>${r.has_position ? ' <span class="tag">보유중</span>' : ''}</h4><div class="why">${esc(r.reasons.join(' · ')) || '-'}</div>${r.blocked ? `<div class="blocked">⏸ 진입 보류: ${esc(r.blocked)}</div>` : ''}</div><div class="plan">${planHtml}<br><span class="muted">${r.ts}</span></div></div>`;
}
function openChart(code) { showPage('chart'); const sel = $('chartCode'); setTimeout(() => { if ([...sel.options].some(o => o.value === code)) { sel.value = code; loadChart(true); } }, 200); }
function renderReco() {
  const s = state, rc = s && s.recommendations;
  $('recoEmpty').style.display = (s && s.running) ? 'none' : '';
  if (!rc) { $('recoBuy').innerHTML = $('recoWatch').innerHTML = $('recoSell').innerHTML = ''; $('recoMeta').textContent = '-'; return; }
  $('recoMeta').textContent = `유니버스 ${rc.universe}종목 · 매수 ${rc.buy.length} · 관심 ${rc.watch.length} · 매도/주의 ${rc.sell.length} · 임계값 +${rc.buy_threshold}/${rc.sell_threshold}`;
  $('recoBuy').innerHTML = rc.buy.map(r => recoCard(r, 'buy')).join('') || '<div class="reco-empty">현재 매수 조건을 만족하는 종목이 없습니다. (캔들이 누적되면 갱신됩니다)</div>';
  $('recoWatch').innerHTML = rc.watch.map(r => recoCard(r, 'watch')).join('') || '<div class="reco-empty">관심 종목 없음</div>';
  $('recoSell').innerHTML = rc.sell.map(r => recoCard(r, 'sell')).join('') || '<div class="reco-empty">매도/주의 종목 없음</div>';
  const ranked = (s.watchlist || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
  $('recoRank').innerHTML = ranked.map((r, i) => { const sc = r.score || 0; const lbl = sc >= rc.buy_threshold ? '매수' : sc <= rc.sell_threshold ? '매도' : sc >= rc.buy_threshold * 0.6 ? '관심' : '관망'; return `<tr style="cursor:pointer" onclick="openChart('${r.code}')"><td>${i + 1}</td><td>${esc(r.name)}<div class="muted small">${r.code}</div></td><td>${fmtWon(r.price)}</td><td>${pct(r.change_pct)}</td><td class="${cls(sc)}"><b>${sc >= 0 ? '+' : ''}${sc.toFixed(0)}</b></td><td>${sigBadge(r.has_position ? '보유' : lbl)}</td><td>${r.regime}</td><td>${num(r.rsi)}</td><td>${r.vol_ratio != null ? 'x' + num(r.vol_ratio, 1) : '-'}</td><td class="txt">${esc(r.reasons.join(', '))}</td></tr>`; }).join('') || '<tr><td colspan="10" class="muted">데이터 대기 중</td></tr>';
  $('sigLog').innerHTML = (s.signal_log || []).map(e => `<tr><td>${e.ts}</td><td>${esc(e.name)}<div class="muted small">${e.code}</div></td><td>${sigBadge(e.kind)}</td><td class="${cls(e.score)}">${e.score >= 0 ? '+' : ''}${e.score.toFixed(0)}</td><td>${fmtWon(e.price)}</td><td>${e.executed ? '<span class="chip ok">주문</span>' : '<span class="chip">알림</span>'}</td><td class="txt">${esc(e.reasons.join(', '))}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">아직 시그널이 없습니다.</td></tr>';
}

/* ---------------- 수익률 관리 ---------------- */
let perfData = null;
async function loadPerf() {
  const days = $('perfDays').value, sim = $('perfSource').value;
  $('perfMeta').textContent = '불러오는 중…';
  $('perfCsv').href = `/api/performance.csv?days=${days}&sim=${sim}`;
  try { perfData = await api(`/api/performance?days=${days}&sim=${sim}`); renderPerf(perfData); }
  catch (e) { $('perfMeta').textContent = e.message; }
}
function renderPerf(r) {
  const k = r.kpi;
  $('perfMeta').textContent = `${k.trading_days}거래일 · ${k.trades}건 · 출처: ${r.sources.length ? r.sources.join(', ') : '거래 기록 없음'}` + (r.include_sim ? '' : ' · 시뮬레이션 거래는 제외됨');
  const tiles = [['누적 손익', signed(k.total_pnl) + '원', cls(k.total_pnl)], ['수익률(초기자금 대비)', (k.return_pct >= 0 ? '+' : '') + k.return_pct + '%', cls(k.return_pct)], ['거래 수', k.trades, ''], ['승률', k.win_rate + '%', k.win_rate >= 50 ? 'up' : 'down'], ['손익비(PF)', k.profit_factor == null ? '-' : k.profit_factor, k.profit_factor >= 1 ? 'up' : 'down'], ['기대손익/거래', signed(k.expectancy), cls(k.expectancy)], ['평균 수익', signed(k.avg_win), 'up'], ['평균 손실', signed(k.avg_loss), 'down'], ['최대 낙폭(누적손익)', signed(k.max_drawdown), 'down'], ['낙폭 지속(일)', k.dd_days, ''], ['최대 연승 / 연패', `${k.best_streak} / ${k.worst_streak}`, ''], ['현재 연속', k.current_streak > 0 ? `${k.current_streak}연승` : k.current_streak < 0 ? `${-k.current_streak}연패` : '-', cls(k.current_streak)], ['일평균 손익', signed(k.pnl_per_day), cls(k.pnl_per_day)], ['수익 거래일', `${k.profitable_days}/${k.trading_days}`, ''], ['평균 보유(분)', k.avg_holding_min, ''], ['총 비용(수수료·세금)', fmtWon(k.fees), '']];
  $('perfKpi').innerHTML = tiles.map(([l, v, c]) => `<div class="metric"><div class="l">${l}</div><div class="v ${c}">${v}</div></div>`).join('');
  drawBars($('perfDaily'), r.daily.map(d => d.pnl), r.daily.map(d => d.date.slice(5)));
  drawLine($('perfCum'), r.daily.map(d => d.cum), { base: 0, fill: true });
  $('perfMonthly').innerHTML = r.monthly.map(m => `<tr><td>${m.month}</td><td class="${cls(m.pnl)}">${signed(m.pnl)}</td><td>${pct(m.ret_pct)}</td><td>${m.trades}</td><td>${m.win_rate}%</td><td>${m.days}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">기록 없음</td></tr>';
  $('perfCode').innerHTML = r.per_code.map(c => `<tr><td>${esc(c.name)}<div class="muted small">${c.code}</div></td><td class="${cls(c.pnl)}">${signed(c.pnl)}</td><td>${c.trades}</td><td>${c.win_rate}%</td><td class="${cls(c.avg)}">${signed(c.avg)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">기록 없음</td></tr>';
  $('perfReason').innerHTML = r.per_reason.map(c => `<tr><td>${esc(c.reason)}</td><td class="${cls(c.pnl)}">${signed(c.pnl)}</td><td>${c.trades}</td><td>${c.win_rate}%</td></tr>`).join('') || '<tr><td colspan="4" class="muted">기록 없음</td></tr>';
  drawBars($('perfHour'), r.per_hour.map(h => h.pnl), r.per_hour.map(h => h.hour));
  $('perfDailyTbl').innerHTML = r.daily.slice().reverse().map(d => `<tr><td>${d.date}</td><td class="${cls(d.pnl)}">${signed(d.pnl)}</td><td>${pct(d.ret_pct, 3)}</td><td class="${cls(d.cum)}">${signed(d.cum)}</td><td>${d.trades}</td><td>${d.win_rate}%</td><td>${fmtWon(d.fees)}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">기록 없음 — 거래가 체결되면 logs/trades_날짜.csv 에 자동 저장됩니다.</td></tr>';
  $('perfTradeCount').textContent = `(${r.trades.length}건 표시)`;
  $('perfTrades').innerHTML = r.trades.map(t => `<tr><td>${t.entry}</td><td>${t.exit}</td><td>${esc(t.name)}</td><td>${t.qty}</td><td>${fmtWon(t.entry_price)}</td><td>${fmtWon(t.exit_price)}</td><td class="${cls(t.pnl)}">${signed(t.pnl)}</td><td>${pct(t.pnl_pct)}</td><td>${t.minutes}</td><td class="txt">${esc(t.reason)}</td></tr>`).join('') || '<tr><td colspan="10" class="muted">거래 없음</td></tr>';
}
function drawBars(c, vals, labels) {
  const { ctx, w, h } = setupCanvas(c); ctx.clearRect(0, 0, w, h);
  if (!vals || !vals.length) { ctx.fillStyle = css('--muted'); ctx.font = '12px Noto Sans KR'; ctx.fillText('데이터 없음', 10, 20); return; }
  const mx = Math.max(...vals.map(v => Math.abs(v)), 1), pad = 8, L = 54, bottom = 18, n = vals.length;
  const y0 = pad + (h - pad - bottom) / 2, scale = (h - pad - bottom) / 2 / mx, bw = Math.max(2, (w - L - pad) / n * 0.7);
  ctx.strokeStyle = css('--line'); ctx.beginPath(); ctx.moveTo(L, y0); ctx.lineTo(w - pad, y0); ctx.stroke();
  ctx.fillStyle = css('--muted'); ctx.font = '10px JetBrains Mono'; ctx.fillText(signed(mx), 2, pad + 8); ctx.fillText(signed(-mx), 2, h - bottom); ctx.fillText('0', 2, y0 + 3);
  vals.forEach((v, i) => { const x = L + (i + 0.5) * (w - L - pad) / n - bw / 2, hh = Math.abs(v) * scale; ctx.fillStyle = v >= 0 ? css('--up') : css('--down'); ctx.fillRect(x, v >= 0 ? y0 - hh : y0, bw, Math.max(1, hh)); });
  const step = Math.max(1, Math.round(n / 10)); ctx.fillStyle = css('--muted'); for (let i = 0; i < n; i += step) ctx.fillText(labels[i], L + (i + 0.5) * (w - L - pad) / n - 14, h - 4);
}

/* ---------------- 캔버스 유틸 ---------------- */
function setupCanvas(c) { const dpr = window.devicePixelRatio || 1; const w = c.clientWidth, h = c.clientHeight; if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); } const ctx = c.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); return { ctx, w, h }; }
function drawLine(c, vals, opt = {}) {
  const { ctx, w, h } = setupCanvas(c); ctx.clearRect(0, 0, w, h);
  if (vals && vals.length === 1) vals = [opt.base == null ? vals[0] : opt.base, vals[0]];
  if (!vals || vals.length < 2) { ctx.fillStyle = css('--muted'); ctx.font = '12px Noto Sans KR'; ctx.fillText('데이터 대기 중', 10, 20); return; }
  const mn = Math.min(...vals), mx = Math.max(...vals), rng = (mx - mn) || 1, pad = 8, L = 54;
  const X = i => L + i / (vals.length - 1) * (w - L - pad), Y = v => h - pad - (v - mn) / rng * (h - 2 * pad);
  ctx.strokeStyle = css('--line'); ctx.lineWidth = 1; ctx.fillStyle = css('--muted'); ctx.font = '10px JetBrains Mono';
  for (let k = 0; k <= 3; k++) { const v = mn + rng * k / 3, y = Y(v); ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - pad, y); ctx.stroke(); ctx.fillText(opt.fmt ? opt.fmt(v) : Math.round(v).toLocaleString(), 2, y + 3); }
  const last = vals[vals.length - 1], base = opt.base == null ? vals[0] : opt.base;
  const color = opt.color || (last >= base ? css('--up') : css('--down'));
  ctx.beginPath(); vals.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
  if (opt.fill !== false) { ctx.lineTo(X(vals.length - 1), h - pad); ctx.lineTo(X(0), h - pad); ctx.closePath(); ctx.fillStyle = color + '22'; ctx.fill(); }
  if (opt.base != null) { const yb = Y(base); ctx.setLineDash([4, 4]); ctx.strokeStyle = css('--muted'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(L, yb); ctx.lineTo(w - pad, yb); ctx.stroke(); ctx.setLineDash([]); }
}

/* ---------------- 캔들 차트 ---------------- */
let chartData = null, chartTimer = null, hoverIdx = -1;
async function loadChart(force) {
  if (!status.running && !force) return;
  const sel = $('chartCode');
  if (state.watchlist) {
    const codes = state.watchlist.map(r => r.code).join(',');
    if (sel.dataset.codes !== codes) {
      const cur = sel.value; sel.innerHTML = state.watchlist.map(r => `<option value="${r.code}">${esc(r.name)} (${r.code})</option>`).join(''); sel.dataset.codes = codes;
      if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
    }
  }
  if (!sel.value) { drawEmpty($('mainChart'), '엔진을 시작하면 차트가 표시됩니다.'); return; }
  try { chartData = await api(`/api/chart?code=${sel.value}&n=${$('chartN').value}`); renderChart(); }
  catch (e) { drawEmpty($('mainChart'), e.message); }
}
function drawEmpty(c, msg) { const { ctx, w, h } = setupCanvas(c); ctx.clearRect(0, 0, w, h); ctx.fillStyle = css('--muted'); ctx.font = '14px Noto Sans KR'; ctx.textAlign = 'center'; ctx.fillText(msg, w / 2, h / 2); ctx.textAlign = 'left'; }
function renderChart() {
  const d = chartData; const c = $('mainChart'); if (!d || !d.bars || !Array.isArray(d.bars.t) || !d.bars.t.length || !Array.isArray(d.bars.close)) { drawEmpty(c, '데이터 없음 (캔들이 쌓이면 표시됩니다)'); return; }
  const b = d.bars, n = b.t.length, { ctx, w, h } = setupCanvas(c); ctx.clearRect(0, 0, w, h);
  const L = 8, R = 64, top = 10, panes = [{ y0: top, y1: h * 0.58 }, { y0: h * 0.60, y1: h * 0.70 }, { y0: h * 0.72, y1: h * 0.85 }, { y0: h * 0.87, y1: h - 18 }];
  const X = i => L + (i + 0.5) * (w - L - R) / n, bw = Math.max(1.5, (w - L - R) / n * 0.65);
  const up = css('--up'), down = css('--down'), line = css('--line'), muted = css('--muted');
  const grid = (p, mn, mx, fmt) => { ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.fillStyle = muted; ctx.font = '10px JetBrains Mono'; for (let k = 0; k <= 4; k++) { const v = mn + (mx - mn) * k / 4, y = p.y1 - (v - mn) / ((mx - mn) || 1) * (p.y1 - p.y0); ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke(); ctx.fillText(fmt(v), w - R + 4, y + 3); } };
  const Yf = (p, mn, mx) => v => p.y1 - (v - mn) / ((mx - mn) || 1) * (p.y1 - p.y0);
  // 가격 패널
  const ov = { ema: $('ovEma').checked, vwap: $('ovVwap').checked, bb: $('ovBb').checked, st: $('ovSt').checked };
  let lows = b.low.filter(v => v != null), highs = b.high.filter(v => v != null);
  if (ov.bb) { lows = lows.concat(b.bb_lower.filter(v => v != null)); highs = highs.concat(b.bb_upper.filter(v => v != null)); }
  if (d.position) { lows.push(d.position.stop); highs.push(d.position.take_profit); }
  let mn = Math.min(...lows), mx = Math.max(...highs); const padP = (mx - mn) * 0.05 || 1; mn -= padP; mx += padP;
  const P = panes[0], Y = Yf(P, mn, mx); grid(P, mn, mx, v => Math.round(v).toLocaleString());
  const plot = (arr, color, width = 1.2, dash) => { ctx.beginPath(); let started = false; arr.forEach((v, i) => { if (v == null) { started = false; return; } started ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); started = true; }); ctx.strokeStyle = color; ctx.lineWidth = width; if (dash) ctx.setLineDash(dash); ctx.stroke(); ctx.setLineDash([]); };
  if (ov.bb) { ctx.beginPath(); let s = false; b.bb_upper.forEach((v, i) => { if (v == null) return; s ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); s = true; }); for (let i = n - 1; i >= 0; i--) { if (b.bb_lower[i] != null) ctx.lineTo(X(i), Y(b.bb_lower[i])); } ctx.closePath(); ctx.fillStyle = '#6366f114'; ctx.fill(); plot(b.bb_upper, '#6366f1aa', 1); plot(b.bb_lower, '#6366f1aa', 1); }
  for (let i = 0; i < n; i++) { const o = b.open[i], cl = b.close[i], hi = b.high[i], lo = b.low[i]; if (o == null) continue; const col = cl >= o ? up : down; ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(X(i), Y(hi)); ctx.lineTo(X(i), Y(lo)); ctx.stroke(); const y1 = Y(Math.max(o, cl)), y2 = Y(Math.min(o, cl)); ctx.fillRect(X(i) - bw / 2, y1, bw, Math.max(1, y2 - y1)); }
  if (ov.ema) { plot(b.ema_fast, '#f59e0b', 1.3); plot(b.ema_slow, '#10b981', 1.3); }
  if (ov.vwap) plot(b.vwap, '#a855f7', 1.5, [5, 3]);
  if (ov.st) { ctx.lineWidth = 1.5; for (let i = 1; i < n; i++) { if (b.st_line[i] == null || b.st_line[i - 1] == null) continue; ctx.strokeStyle = b.st_dir[i] > 0 ? up : down; ctx.beginPath(); ctx.moveTo(X(i - 1), Y(b.st_line[i - 1])); ctx.lineTo(X(i), Y(b.st_line[i])); ctx.stroke(); } }
  if (d.position) { const hline = (v, color, label) => { const y = Y(v); ctx.setLineDash([6, 4]); ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = color; ctx.font = 'bold 10px JetBrains Mono'; ctx.fillText(label + ' ' + Math.round(v).toLocaleString(), L + 4, y - 3); }; hline(d.position.avg_price, '#f59e0b', '평단'); hline(d.position.stop, down, '손절'); hline(d.position.take_profit, up, '익절'); }
  // 매매 마커
  const tIndex = {}; b.t.forEach((t, i) => { tIndex[(b.key ? b.key[i] : (b.date[i] + ' ' + t))] = i; });
  (d.markers || []).forEach(m => { const i = tIndex[m.key || m.t]; if (i == null) return; const y = Y(m.price); ctx.fillStyle = m.side === 'BUY' ? up : down; ctx.beginPath(); if (m.side === 'BUY') { ctx.moveTo(X(i), y + 6); ctx.lineTo(X(i) - 6, y + 16); ctx.lineTo(X(i) + 6, y + 16); } else { ctx.moveTo(X(i), y - 6); ctx.lineTo(X(i) - 6, y - 16); ctx.lineTo(X(i) + 6, y - 16); } ctx.closePath(); ctx.fill(); });
  // 거래량
  const V = panes[1], vmax = Math.max(...b.volume.map(v => v || 0), 1), Yv = Yf(V, 0, vmax); grid(V, 0, vmax, v => (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(0) + 'K' : Math.round(v)));
  for (let i = 0; i < n; i++) { const v = b.volume[i] || 0; ctx.fillStyle = (b.close[i] >= b.open[i] ? up : down) + '99'; ctx.fillRect(X(i) - bw / 2, Yv(v), bw, V.y1 - Yv(v)); }
  // RSI
  const Rp = panes[2], Yr = Yf(Rp, 0, 100); grid(Rp, 0, 100, v => v.toFixed(0)); [30, 70].forEach(lv => { ctx.setLineDash([3, 3]); ctx.strokeStyle = muted; ctx.beginPath(); ctx.moveTo(L, Yr(lv)); ctx.lineTo(w - R, Yr(lv)); ctx.stroke(); ctx.setLineDash([]); });
  ctx.beginPath(); let s2 = false; b.rsi.forEach((v, i) => { if (v == null) return; s2 ? ctx.lineTo(X(i), Yr(v)) : ctx.moveTo(X(i), Yr(v)); s2 = true; }); ctx.strokeStyle = '#a855f7'; ctx.lineWidth = 1.3; ctx.stroke();
  ctx.fillStyle = muted; ctx.font = '10px Noto Sans KR'; ctx.fillText('RSI(14)', L + 4, Rp.y0 + 10);
  // MACD hist
  const M = panes[3], mvals = b.macd_hist.filter(v => v != null), mabs = Math.max(...mvals.map(Math.abs), 1e-9), Ym = Yf(M, -mabs, mabs); grid(M, -mabs, mabs, v => v.toFixed(0));
  for (let i = 0; i < n; i++) { const v = b.macd_hist[i]; if (v == null) continue; ctx.fillStyle = (v >= 0 ? up : down) + 'bb'; const y0 = Ym(0), y1 = Ym(v); ctx.fillRect(X(i) - bw / 2, Math.min(y0, y1), bw, Math.abs(y1 - y0) || 1); }
  ctx.fillStyle = muted; ctx.fillText('MACD 히스토그램', L + 4, M.y0 + 10);
  // 시간축
  ctx.fillStyle = muted; ctx.font = '10px JetBrains Mono'; const step = Math.max(1, Math.round(n / 8)); for (let i = 0; i < n; i += step) ctx.fillText((i > 0 && b.date[i] !== b.date[i - 1]) || (i === 0 && n > 1 && b.date[0] !== b.date[n - 1]) ? b.date[i] + ' ' + b.t[i] : b.t[i], X(i) - 14, h - 5);
  // 헤더
  ctx.fillStyle = css('--ink'); ctx.font = 'bold 13px Noto Sans KR'; ctx.fillText(`${d.name} (${d.code})  ${b.date[n - 1]}`, L + 4, top + 12);
  ctx.font = '11px Noto Sans KR'; let lx = L + 4, ly = top + 28; const legend = [['#f59e0b', 'EMA9', ov.ema], ['#10b981', 'EMA21', ov.ema], ['#a855f7', 'VWAP', ov.vwap], ['#6366f1', '볼린저', ov.bb]]; legend.forEach(([col, name, on]) => { if (!on) return; ctx.fillStyle = col; ctx.fillRect(lx, ly - 8, 10, 3); ctx.fillStyle = muted; ctx.fillText(name, lx + 13, ly - 3); lx += 60; });
  // 크로스헤어
  if (hoverIdx >= 0 && hoverIdx < n) { const i = hoverIdx; ctx.setLineDash([3, 3]); ctx.strokeStyle = muted; ctx.beginPath(); ctx.moveTo(X(i), top); ctx.lineTo(X(i), h - 18); ctx.stroke(); ctx.setLineDash([]); }
  const sig = d.signal; $('chartSig').textContent = sig ? `점수 ${sig.score >= 0 ? '+' : ''}${sig.score} · ${sig.action} · ${sig.regime === 'trend' ? '추세장' : sig.regime === 'range' ? '횡보장' : '-'}` : '-'; $('chartSig').className = 'chip ' + (sig && sig.action === 'BUY' ? 'err' : sig && sig.action === 'SELL' ? 'ok' : '');
  $('chartReasons').innerHTML = sig ? '<ul class="notes">' + sig.reasons.map(r => `<li>${esc(r)}</li>`).join('') + '</ul>' : '<span class="muted">시그널 계산 대기 중 (캔들 수 부족 시 시간이 걸립니다)</span>';
  c._geom = { X, n, L, R };
}
$('mainChart').addEventListener('mousemove', ev => {
  const c = $('mainChart'), g = c._geom, d = chartData; if (!g || !d) return;
  const rect = c.getBoundingClientRect(), x = ev.clientX - rect.left, i = Math.floor((x - g.L) / ((c.clientWidth - g.L - g.R) / g.n));
  if (i < 0 || i >= g.n) { $('chartTip').style.display = 'none'; hoverIdx = -1; renderChart(); return; }
  hoverIdx = i; renderChart(); const b = d.bars, tip = $('chartTip');
  tip.style.display = ''; tip.style.left = Math.min(x + 14, c.clientWidth - 200) + 'px'; tip.style.top = (ev.clientY - rect.top + 10) + 'px';
  tip.innerHTML = `<b>${b.date[i]} ${b.t[i]}</b><br>시 ${fmtWon(b.open[i])} 고 ${fmtWon(b.high[i])}<br>저 ${fmtWon(b.low[i])} 종 ${fmtWon(b.close[i])}<br>량 ${fmtWon(b.volume[i])} · RSI ${num(b.rsi[i])}<br>VWAP ${fmtWon(b.vwap[i])}`;
});
$('mainChart').addEventListener('mouseleave', () => { hoverIdx = -1; $('chartTip').style.display = 'none'; renderChart(); });
['ovEma', 'ovVwap', 'ovBb', 'ovSt'].forEach(id => $(id).addEventListener('change', renderChart));

/* ---------------- 백테스트 ---------------- */
let btTimer = null;
async function runBacktest() {
  try {
    await api('/api/backtest', { days: +$('btDays').value, codes: $('btCodes').value.trim() || null, feed: $('btFeed').value, seed: +$('btSeed').value });
    $('btRun').disabled = true; $('btResult').style.display = 'none'; pollBacktest();
  } catch (e) { toast(e.message, 'err'); }
}
async function pollBacktest() {
  clearTimeout(btTimer);
  try {
    const j = await api('/api/backtest');
    if (j.status === 'running') { $('btProgress').textContent = `실행 중… ${j.progress} (${j.elapsed}s)`; $('btRun').disabled = true; btTimer = setTimeout(pollBacktest, 800); return; }
    $('btRun').disabled = false;
    if (j.status === 'error') { $('btProgress').textContent = '오류: ' + j.error; return; }
    if (j.status === 'done' && j.result) { $('btProgress').textContent = `완료 (${j.elapsed}s) · ${j.result.params.feed} ${j.result.params.days}일 · ${j.result.params.codes.length}종목`; renderBacktest(j.result); }
    else $('btProgress').textContent = '설정 후 실행을 누르세요.';
  } catch (e) { $('btProgress').textContent = e.message; }
}
function renderBacktest(r) {
  $('btResult').style.display = '';
  const s = r.summary, keys = ['총수익률(%)', '거래횟수', '승률(%)', '손익비(PF)', '기대손익/거래', '최대낙폭(%)', '샤프(일간)', '소르티노', '칼마', '평균보유(분)', '평균수익', '평균손실'];
  $('btMetrics').innerHTML = keys.filter(k => k in s).map(k => { const v = s[k]; const colored = ['총수익률(%)', '기대손익/거래', '최대낙폭(%)', '샤프(일간)', '소르티노', '칼마'].includes(k); return `<div class="metric"><div class="l">${k}</div><div class="v ${colored ? cls(+v) : ''}">${typeof v === 'number' ? v.toLocaleString('ko-KR') : v}</div></div>`; }).join('');
  drawLine($('btEq'), r.equity.map(e => e[1]), { base: s['초기자금'] });
  drawLine($('btDd'), r.drawdown, { color: css('--down'), fmt: v => v.toFixed(1) + '%' });
  $('btPer').innerHTML = Object.entries(r.per_code).map(([c, d]) => `<tr><td>${esc(d.name)}</td><td>${d.trades}</td><td>${d.trades ? (d.wins / d.trades * 100).toFixed(0) : 0}%</td><td class="${cls(d.pnl)}">${signed(d.pnl)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">거래 없음</td></tr>';
  $('btTrades').innerHTML = r.trades.map(t => `<tr><td>${t.entry}</td><td>${t.exit}</td><td>${esc(t.name)}</td><td>${t.qty}</td><td>${fmtWon(t.entry_price)}</td><td>${fmtWon(t.exit_price)}</td><td class="${cls(t.pnl)}">${signed(t.pnl)}</td><td>${pct(t.pnl_pct)}</td><td>${t.minutes}</td><td class="txt">${esc(t.reason)}</td></tr>`).join('') || '<tr><td colspan="10" class="muted">거래 없음</td></tr>';
  window._bt = r;
}

/* ---------------- 뉴스 ---------------- */
async function loadNews(fetch) {
  $('newsMeta').textContent = fetch ? '수집 중… (RSS·지수 조회, 수 초 소요)' : '';
  try {
    const d = await api(`/api/news?fetch=${fetch ? 1 : 0}`);
    $('newsMeta').textContent = `뉴스 ${d.news_count}건 · 갱신 ${d.updated || '-'}` + (d.errors.length ? ` · 오류: ${d.errors.join(' / ')}` : '');
    $('newsIdx').innerHTML = d.indices.map(i => `<div><b>${i.name}</b>${(+i.value).toLocaleString('ko-KR', { maximumFractionDigits: 2 })} ${pct(i.change_pct)} <span class="muted">10분 ${num(i.momentum, 2)}% · ${i.ts}</span></div>`).join('') || '<span class="muted">지수 데이터 없음 — "지금 수집"을 누르세요 (인터넷 필요)</span>';
    $('newsMkt').innerHTML = `시장 편향 <b class="${cls(d.market_bias)}">${num(d.market_bias, 2)}</b> ${esc(d.market_text)}`;
    $('newsSec').innerHTML = d.sectors.map(x => `<tr><td>${x.sector}</td><td>${pct(x.rs)}</td><td class="${cls(x.sentiment)}">${num(x.sentiment, 2)}</td><td>${x.count}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">데이터 없음</td></tr>';
    $('newsAssess').innerHTML = d.assessments.map(a => `<tr><td>${esc(a.name)}<div class="muted small">${a.code}</div></td><td class="${cls(a.bias)}">${num(a.bias, 1)}</td><td class="${cls(a.market_bias)}">${num(a.market_bias, 2)}</td><td class="${cls(a.sector_bias)}">${num(a.sector_bias, 1)}</td><td class="${cls(a.news_bias)}">${num(a.news_bias, 1)}</td><td>${a.entry_block ? `<span class="chip err">${esc(a.entry_block)}</span>` : '-'}</td><td>${a.exit_hint ? `<span class="chip warn">${esc(a.exit_hint)}</span>` : '-'}</td><td class="txt">${esc(a.notes.join(' · '))}</td></tr>`).join('');
    $('newsList').innerHTML = d.news.map(n => `<li><time>${n.ts}</time><span class="sent ${cls(n.sentiment)}">${num(n.sentiment, 1)}</span><span>${n.sectors.map(t => `<span class="tag">${esc(t)}</span>`).join(' ')}${n.macro ? ' <span class="tag">거시</span>' : ''}${n.codes.map(t => ` <span class="tag">${esc(t)}</span>`).join('')} ${n.link ? `<a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)} <span class="muted small">${esc(n.source)}</span></span></li>`).join('') || '<li class="muted">수집된 뉴스가 없습니다. "지금 수집"을 누르세요.</li>';
  } catch (e) { $('newsMeta').textContent = e.message; }
}

/* ---------------- 설정 ---------------- */
let cfg = null;
const RISK_LABELS = { risk_per_trade: '1회 거래 최대 손실 (계좌 비율, 0.01=1%)', max_position_pct: '종목당 최대 비중 (0.3=30%)', max_positions: '최대 동시 보유 종목 수', atr_stop_mult: '손절 ATR 배수', min_stop_pct: '최소 손절폭 (0.006=0.6%)', max_stop_pct: '최대 손절폭', take_profit_r: '익절 R배수', trail_atr_mult: '트레일링 ATR 배수', breakeven_r: '본전 이동 R', partial_take_r: '부분 익절 R (0=끔)', partial_take_ratio: '부분 익절 비율', daily_loss_limit_pct: '일일 손실 한도 (0.03=3%)', daily_profit_lock_pct: '일일 목표 수익 (0=끔)', max_trades_per_day: '일일 최대 거래 수', entry_start: '진입 시작 시각 (HH:MM)', entry_end: '진입 종료 시각', force_close: '강제 청산 시각', cooldown_after_loss_min: '손절 후 재진입 대기(분)', max_holding_min: '최대 보유(분, 0=무제한)', kelly_enabled: '켈리 사이징 사용', kelly_lookback: '켈리 참조 거래 수', kelly_fraction: '켈리 분수 (0.5=하프)', kelly_min_scale: '켈리 최소 배율', kelly_max_scale: '켈리 최대 배율' };
const STRAT_LABELS = { buy_threshold: '매수 임계 점수', sell_threshold: '매도 임계 점수', adx_trend: '추세장 판정 ADX', require_above_vwap: '매수 시 VWAP 위 요구', min_vol_ratio: '최소 거래량 비율', max_rsi_entry: '진입 허용 최대 RSI', min_bb_width_pct: '최소 밴드폭(%)', orb_minutes: '시가범위(ORB) 분', ema_fast: 'EMA 단기', ema_slow: 'EMA 중기', ema_trend: 'EMA 장기', rsi: 'RSI 기간', bb: '볼린저 기간', bb_k: '볼린저 승수', atr: 'ATR 기간', st_period: '슈퍼트렌드 기간', st_mult: '슈퍼트렌드 승수', adx: 'ADX 기간', vol_n: '거래량 평균 기간', high_vol_threshold_add: '고변동성 임계 가산' };
function field(name, label, value, type = 'text', opts) {
  if (type === 'bool') return `<label>${label}<select data-k="${name}" data-type="bool"><option value="true" ${value ? 'selected' : ''}>사용</option><option value="false" ${!value ? 'selected' : ''}>사용 안 함</option></select></label>`;
  if (type === 'select') return `<label>${label}<select data-k="${name}">${opts.map(o => `<option value="${o[0]}" ${String(o[0]) === String(value) ? 'selected' : ''}>${o[1]}</option>`).join('')}</select></label>`;
  return `<label>${label}<input type="${type}" data-k="${name}" data-type="${type}" value="${esc(value == null ? '' : value)}" ${type === 'number' ? 'step="any"' : ''}></label>`;
}
async function loadConfig() {
  try { cfg = await api('/api/config'); } catch (e) { toast(e.message, 'err'); return; }
  $('cfgPath').textContent = '설정 파일: ' + (status.config_path || 'config.yaml');
  $('fGeneral').innerHTML = field('mode', '기본 모드', cfg.mode, 'select', [['paper', '페이퍼(가상 체결)'], ['live', '한국투자증권 주문']]) + field('feed', '기본 시세 피드', cfg.feed, 'select', [['sim', '시뮬레이션'], ['naver', '네이버 폴링'], ['kis', '한국투자증권 웹소켓']]) + field('initial_cash', '초기 자금(원, 페이퍼)', cfg.initial_cash, 'number') + field('interval_min', '시그널 캔들 주기(분)', cfg.interval_min, 'number') + field('auto_trade', '자동매매', cfg.auto_trade, 'bool') + field('screener.enabled', '거래량 상위 자동 편입', cfg.screener.enabled, 'bool') + field('screener.limit', '스크리너 종목 수', cfg.screener.limit, 'number') + field('sim.speed', '시뮬레이션 배속', cfg.sim.speed, 'number');
  const env = cfg._env || {};
  const envHint = k => (env[k] ? `<span class="hint">환경변수 ${esc(env[k])} 사용 중 — 비워 두면 그대로 유지됩니다</span>` : '');
  $('fKis').innerHTML = field('kis.app_key', '앱키 (App Key)', cfg.kis.app_key, 'password') + envHint('kis.app_key') + field('kis.app_secret', '앱 시크릿', cfg.kis.app_secret, 'password') + envHint('kis.app_secret') + field('kis.account', '계좌번호 (예: 12345678-01)', cfg.kis.account) + envHint('kis.account') + field('kis.paper', '서버', cfg.kis.paper, 'select', [['true', '모의투자 (권장)'], ['false', '실전 (실계좌 주문!)']]);
  const sch = cfg._schema || { risk: {}, strategy: {} };
  $('fRisk').innerHTML = Object.keys(RISK_LABELS).map(k => { const v = (cfg.risk || {})[k]; const def = sch.risk[k]; const isBool = typeof def === 'boolean'; const isTime = /^\d{1,2}:\d{2}$/.test(String(def)); return field('risk.' + k, RISK_LABELS[k], v == null ? def : v, isBool ? 'bool' : isTime ? 'text' : 'number'); }).join('');
  $('fStrategy').innerHTML = Object.keys(STRAT_LABELS).map(k => { const v = (cfg.strategy || {})[k]; const def = sch.strategy[k]; const isBool = typeof def === 'boolean'; return field('strategy.' + k, STRAT_LABELS[k], v == null ? def : v, isBool ? 'bool' : 'number'); }).join('');
  $('fQuant').innerHTML = field('quant.enabled', '퀀트 알파 사용', cfg.quant.enabled, 'bool') + field('quant.pairs_entry_z', '페어 진입 z-score', cfg.quant.pairs_entry_z == null ? 2.0 : cfg.quant.pairs_entry_z, 'number') + field('context.enabled', '거시·뉴스 컨텍스트 사용', cfg.context.enabled, 'bool') + field('context.stock_news', '종목별 구글뉴스 검색', cfg.context.stock_news == null ? true : cfg.context.stock_news, 'bool') + field('context.risk_off_index_drop_pct', '코스피 급락 진입 차단(%)', cfg.context.risk_off_index_drop_pct == null ? -1.5 : cfg.context.risk_off_index_drop_pct, 'number') + field('context.stock_news_block', '종목 악재 진입 차단 감성', cfg.context.stock_news_block == null ? -0.45 : cfg.context.stock_news_block, 'number') + field('rules.enabled', '규칙 엔진 사용', cfg.rules.enabled, 'bool') + field('rules.use_defaults', '기본 규칙 포함', cfg.rules.use_defaults, 'bool');
  $('fMisc').innerHTML = field('telegram.token', '텔레그램 봇 토큰', cfg.telegram.token, 'password') + envHint('telegram.token') + field('telegram.chat_id', '텔레그램 채팅 ID', cfg.telegram.chat_id) + envHint('telegram.chat_id') + field('naver.poll_interval', '네이버 폴링 주기(초)', cfg.naver.poll_interval, 'number') + field('log_dir', '로그 폴더', cfg.log_dir);
  const im = document.querySelector('#page-settings [data-k="interval_min"]'); if (im) { im.min = '1'; im.step = '1'; }
  renderWatch();
}
function renderWatch() { $('wlEdit').innerHTML = Object.entries(cfg.watchlist).map(([c, n]) => `<span class="wchip"><b>${c}</b> ${esc(n || '')}<button title="삭제" onclick="removeWatch('${c}')">✕</button></span>`).join('') || '<span class="muted">관심종목이 없습니다.</span>'; }
async function addWatch() {
  let code = $('wlAdd').value.trim(), name = $('wlAddName').value.trim();
  if (!/^\d{1,6}$/.test(code)) { toast('종목코드는 숫자 6자리입니다.', 'err'); return; }
  code = code.padStart(6, '0');
  if (!name) { try { const r = await api('/api/lookup?code=' + code); name = r.name; toast(`${name} ${fmtWon(r.price)}원`); } catch (e) { toast('종목명 자동 조회 실패 (인터넷 필요). 이름 없이 추가합니다.'); } }
  cfg.watchlist[code] = name; $('wlAdd').value = ''; $('wlAddName').value = ''; renderWatch();
}
function removeWatch(c) { delete cfg.watchlist[c]; renderWatch(); }
async function screenAdd() {
  toast('거래량 상위 조회 중…');
  try { const r = await api('/api/screen?source=naver&limit=15'); const n = Object.keys(r.result).length; Object.assign(cfg.watchlist, r.result); renderWatch(); toast(`${n}개 종목을 불러왔습니다. 저장을 누르세요.`, 'ok'); } catch (e) { toast(e.message, 'err'); }
}
function collectForm() {
  const out = JSON.parse(JSON.stringify(cfg)); delete out._schema; delete out._env;
  document.querySelectorAll('#page-settings [data-k]').forEach(el => {
    const path = el.dataset.k.split('.'); let v = el.value;
    if (el.dataset.type === 'bool' || el.tagName === 'SELECT' && (v === 'true' || v === 'false')) v = v === 'true';
    else if (el.dataset.type === 'number') { if (v === '') return; v = Number(v); if (isNaN(v)) return; }
    let o = out; for (let i = 0; i < path.length - 1; i++) { o[path[i]] = o[path[i]] || {}; o = o[path[i]]; }
    o[path[path.length - 1]] = v;
  });
  return out;
}
async function saveConfig() {
  try { cfg = await api('/api/config', collectForm()); toast('설정을 저장했습니다. 실행 중인 엔진에는 다음 시작부터 적용됩니다.', 'ok'); pollStatus(); renderWatch(); }
  catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 로그 ---------------- */
let logLast = 0, logTimer = null;
async function pollLogs() {
  clearTimeout(logTimer);
  try { const d = await api('/api/logs?since=' + logLast); if (d.records.length) { const box = $('logBox'); d.records.forEach(r => { const s = document.createElement('span'); s.className = r.level; s.textContent = `${r.ts} [${r.level}] ${r.name}: ${r.msg}\n`; box.appendChild(s); }); while (box.childNodes.length > 800) box.removeChild(box.firstChild); logLast = d.last_id; if ($('logFollow').checked) box.scrollTop = box.scrollHeight; } } catch (e) {}
  if (page === 'logs') logTimer = setTimeout(pollLogs, 1500);
}
function clearLogs() { $('logBox').innerHTML = ''; }

/* ---------------- 루프 ---------------- */
function redrawAll() { if (page === 'dash') renderDash(); if (page === 'reco') renderReco(); if (page === 'chart') renderChart(); if (page === 'backtest' && window._bt) renderBacktest(window._bt); if (page === 'perf' && perfData) renderPerf(perfData); }
window.addEventListener('resize', redrawAll);
(async function init() {
  await pollStatus();
  const saved = (() => { try { return localStorage.getItem('page'); } catch (e) { return null; } })();
  showPage(saved && document.getElementById('page-' + saved) ? saved : 'home');
  setInterval(pollStatus, 2000);
  setInterval(pollState, 1000);
  setInterval(() => { if (page === 'chart' && status.running) loadChart(false); }, 2000);
})();
