/* AlcoSoft dashboard — live status */

function updateClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  el.textContent = '⏰ ' + new Date().toLocaleTimeString('en-IN', { hour12: false });
}

const fmt = (v) => (v !== null && v !== undefined ? '₹' + parseFloat(v).toFixed(2) : '—');

function pnlClass(v) {
  if (!v) return 'neutral';
  return parseFloat(v) >= 0 ? 'win' : 'loss';
}

function verdictClass(v) {
  if (!v) return '';
  v = v.toUpperCase();
  if (v === 'BUY' || v === 'APPROVE') return 'war-verdict-buy';
  if (v === 'AVOID' || v === 'REJECT') return 'war-verdict-avoid';
  return 'war-verdict-wait';
}

function agentEmoji(name) {
  const map = {
    'Technical Analyst': '📊',
    'Fundamental Analyst': '📰',
    'Risk Manager': '🛡️',
    'Mediator': '⚖️',
  };
  return map[name] || '🤖';
}

function timeStr(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

async function fetchAndRender() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    const modeBadge = document.getElementById('mode-badge');
    if (modeBadge) {
      modeBadge.textContent = data.trading_mode === 'LIVE' ? '🔴 LIVE' : '📋 PAPER';
      modeBadge.className = 'badge ' + (data.trading_mode === 'LIVE' ? 'badge-live' : 'badge-paper');
    }

    const stratBadge = document.getElementById('strategy-badge');
    if (stratBadge) stratBadge.textContent = '⚡ ' + (data.strategy || 'INTRADAY');

    const s = data.stats || {};
    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };

    set('stat-total', s.total_trades || 0);
    set('stat-winrate', (s.win_rate || 0) + '%');
    set('stat-wl', (s.winning_trades || 0) + '🏆 / ' + (s.losing_trades || 0) + '💔');

    const pnl = parseFloat(s.gross_pnl || 0);
    const pnlEl = document.getElementById('stat-pnl');
    if (pnlEl) {
      pnlEl.textContent = (pnl >= 0 ? '+' : '') + '₹' + pnl.toFixed(2);
      pnlEl.className = 'card-value ' + (pnl >= 0 ? 'win' : 'loss');
    }

    set('stat-open', (data.positions || []).length);
    set('stat-capital', '💵 Capital: ₹' + (data.capital || 10000).toLocaleString('en-IN'));

    const posBody = document.getElementById('positions-body');
    if (posBody) {
      if ((data.positions || []).length === 0) {
        posBody.innerHTML = '<tr><td colspan="6" class="empty">😴 No open positions right now</td></tr>';
      } else {
        posBody.innerHTML = data.positions.map((p) => `
          <tr>
            <td><strong>🏷️ ${p.symbol}</strong></td>
            <td class="mono col-qty">${p.quantity}</td>
            <td class="mono col-num">${fmt(p.entry_price)}</td>
            <td class="mono col-num loss">${fmt(p.stop_loss)}</td>
            <td class="cell-strategy" title="${p.strategy || ''}">${p.strategy || '—'}</td>
            <td class="col-time" style="color:var(--text2)">${timeStr(p.entry_time)}</td>
          </tr>`).join('');
      }
    }

    const briefing = data.briefing || {};
    const approved = briefing.approved_stocks || [];
    const watchlist = briefing.watchlist || [];
    const allStocks = [...approved, ...watchlist];
    const blist = document.getElementById('briefing-list');
    const bpulse = document.getElementById('briefing-pulse');

    if (blist) {
      if (allStocks.length === 0) {
        blist.innerHTML = '<div class="empty">🌅 Waiting for morning screener...</div>';
        if (bpulse) bpulse.style.display = 'none';
      } else {
        if (bpulse) bpulse.style.display = 'inline-block';
        blist.innerHTML = allStocks.map((s) => `
          <div class="stock-pill">
            <div>
              <div class="stock-name">📌 ${s.ticker}</div>
              <div class="stock-conf">${s.reason || ''}</div>
            </div>
            <div style="text-align:right">
              <div class="${s.direction === 'BUY_ONLY' ? 'direction-buy' : s.direction === 'WATCH' ? 'neutral' : 'direction-avoid'}">
                ${s.direction === 'BUY_ONLY' ? '🟢 BUY' : s.direction === 'WATCH' ? '👀 MATH' : '🔴 AVOID'}
              </div>
              ${s.confidence > 0 ? `<div class="stock-conf" style="margin-top:3px">🎯 ${s.confidence}%</div>` : ''}
            </div>
          </div>`).join('');
      }
    }

    const trBody = document.getElementById('trades-body');
    if (trBody) {
      if ((data.trades || []).length === 0) {
        trBody.innerHTML = '<tr><td colspan="6" class="empty">📭 No trades yet</td></tr>';
      } else {
        trBody.innerHTML = data.trades.map((t) => {
          const pnlVal = parseFloat(t.pnl || 0);
          const emoji = t.status === 'CLOSED' ? '✅' : t.status === 'STOPPED' ? '🛑' : '🟡';
          const st = (t.status || '').toUpperCase();
          const statusCls =
            st === 'CLOSED' ? 'status-closed' :
            st === 'STOPPED' ? 'status-stopped' : 'status-open';
          const pnlText = t.pnl != null
            ? (pnlVal >= 0 ? '+' : '') + '₹' + pnlVal.toFixed(2)
            : '—';
          return `<tr>
            <td><strong>${emoji} ${t.symbol}</strong></td>
            <td class="mono col-num">${fmt(t.entry_price)}</td>
            <td class="mono col-num">${t.exit_price ? fmt(t.exit_price) : '—'}</td>
            <td class="mono col-pnl ${pnlClass(t.pnl)}">${pnlText}</td>
            <td class="col-status"><span class="status-badge ${statusCls}">${st || '—'}</span></td>
            <td class="cell-strategy" title="${t.strategy || ''}">${t.strategy || '—'}</td>
          </tr>`;
        }).join('');
      }
    }

    const warLog = document.getElementById('war-log');
    if (warLog) {
      if ((data.war_log || []).length === 0) {
        warLog.innerHTML = '<div class="empty">🤫 War room hasn\'t spoken yet...</div>';
      } else {
        warLog.innerHTML = data.war_log.map((w) => {
          let reasons = [];
          try { reasons = JSON.parse(w.reasons || '[]'); } catch (_) {}
          return `<div class="war-entry">
            <div class="war-header">
              <span class="agent-name">${agentEmoji(w.agent)} ${w.agent}</span>
              <span class="${verdictClass(w.verdict)}">${w.verdict}</span>
            </div>
            <div style="font-size:0.73rem;color:var(--text2)">
              🎯 ${w.symbol} | R${w.round_number} | 💪 ${w.confidence}% | ⏰ ${timeStr(w.timestamp)}
            </div>
            ${reasons.length ? `<div class="war-reasons">💬 ${reasons.join(' · ')}</div>` : ''}
            ${w.concern ? `<div class="war-reasons">⚠️ ${w.concern}</div>` : ''}
          </div>`;
        }).join('');
      }
    }

    const ref = data.reflection || {};
    const refEmpty = document.getElementById('reflection-empty');
    const refContent = document.getElementById('reflection-content');
    if (ref.one_line_summary && refEmpty && refContent) {
      refEmpty.style.display = 'none';
      refContent.style.display = 'block';
      document.getElementById('ref-summary').textContent = ref.one_line_summary || '—';
      document.getElementById('ref-worked').textContent = ref.what_worked || '—';
      document.getElementById('ref-failed').textContent = ref.what_failed || '—';
      const gradeEl = document.getElementById('ref-grade');
      gradeEl.textContent = ref.overall_grade || '—';
      gradeEl.className =
        ref.overall_grade === 'A' ? 'grade-a' :
        ref.overall_grade === 'B' ? 'grade-b' :
        ref.overall_grade === 'C' ? 'grade-c' : 'grade-d';
      const adj = ref.tomorrow_adjustments || [];
      document.getElementById('ref-adjustments').innerHTML = adj.map((a) => `🔧 ${a}`).join('<br>');
    }

    if (typeof renderCharts === 'function' && data.charts) {
      renderCharts(data.charts);
    }
  } catch (e) {
    console.error('Dashboard fetch error:', e);
  }
}

updateClock();
setInterval(updateClock, 1000);
fetchAndRender();
setInterval(fetchAndRender, 5000);
