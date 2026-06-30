/* AlcoSoft dashboard — live status */

function updateClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  el.textContent = '⏰ ' + new Date().toLocaleTimeString('en-IN', { hour12: false });
}

const fmt = (v) => {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? '₹' + n.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—';
};

function pnlClass(v) {
  if (!v) return 'neutral';
  return parseFloat(v) >= 0 ? 'win' : 'loss';
}

function verdictClass(v) {
  if (!v) return '';
  v = v.toUpperCase();
  if (v === 'BUY' || v === 'APPROVE') return 'agent-verdict-buy';
  if (v === 'AVOID' || v === 'REJECT') return 'agent-verdict-avoid';
  return 'agent-verdict-wait';
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

function exitSetName(notes) {
  const prefix = 'SELL_SET:';
  return notes && notes.startsWith(prefix) ? notes.slice(prefix.length) : '';
}

let lastPositionsCount = -1;
let lastTotalTrades = -1;

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

    const btnSquareoff = document.getElementById('squareoff-btn');
    const btnResume = document.getElementById('resume-trading-btn');
    const sessionState = data.trading_state && data.trading_state.state ? data.trading_state.state : '';
    if (sessionState === 'FLAT_LOCKED') {
      if (btnSquareoff) btnSquareoff.style.display = 'none';
      if (btnResume) btnResume.style.display = 'inline-block';
    } else {
      if (btnSquareoff) btnSquareoff.style.display = 'inline-block';
      if (btnResume) btnResume.style.display = 'none';
    }

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

    const currentPositionsCount = (data.positions || []).length;
    set('stat-open', currentPositionsCount);

    const currentTotalTrades = s.total_trades || 0;
    if (currentPositionsCount !== lastPositionsCount || currentTotalTrades !== lastTotalTrades) {
      lastPositionsCount = currentPositionsCount;
      lastTotalTrades = currentTotalTrades;
      updateMarginStatus();
    }
    const cap = data.capital_snapshot || {};
    set('cap-equity', fmt(cap.account_equity));
    set('cap-exposure', fmt(cap.gross_exposure));
    set('cap-blocked', fmt(cap.margin_blocked));
    set('cap-free', fmt(cap.free_margin));
    set('cap-power', fmt(cap.remaining_buying_power));

    set('live-start-eq', fmt(cap.starting_capital));
    set('live-closed-pnl', fmt(cap.closed_pnl));
    set('live-unrealized-pnl', fmt(cap.unrealized_pnl));
    set('live-trading-eq', fmt(cap.account_equity));

    // Margin updates are handled by updateMarginStatus() based on trade activity

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
          const exitSet = exitSetName(t.notes || '');
          const strategyTitle = exitSet ? `${t.strategy || ''} | Exit: ${exitSet}` : (t.strategy || '');
          const strategyHtml = `${t.strategy || '—'}${exitSet ? `<div class="strategy-sub">Exit: ${exitSet}</div>` : ''}`;
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
            <td class="cell-strategy" title="${strategyTitle}">${strategyHtml}</td>
          </tr>`;
        }).join('');
      }
    }

    const decisionLog = document.getElementById('agent-decisions');
    if (decisionLog) {
      if ((data.agent_decisions || []).length === 0) {
        decisionLog.innerHTML = '<div class="empty">Waiting for agent decisions...</div>';
      } else {
        decisionLog.innerHTML = data.agent_decisions.map((w) => {
          let reasons = [];
          try { reasons = JSON.parse(w.reasons || '[]'); } catch (_) {}
          return `<div class="agent-entry">
            <div class="agent-header">
              <span class="agent-name">${agentEmoji(w.agent)} ${w.agent}</span>
              <span class="${verdictClass(w.verdict)}">${w.verdict}</span>
            </div>
            <div style="font-size:0.73rem;color:var(--text2)">
              🎯 ${w.symbol} | R${w.round_number} | 💪 ${w.confidence}% | ⏰ ${timeStr(w.timestamp)}
            </div>
            ${reasons.length ? `<div class="agent-reasons">💬 ${reasons.join(' · ')}</div>` : ''}
            ${w.concern ? `<div class="agent-reasons">⚠️ ${w.concern}</div>` : ''}
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

// 🔥 NEW: Fetch and display margin status
async function updateMarginStatus() {
  try {
    const res = await fetch('/api/margin-status');
    const data = await res.json();
    
    const marginCard = document.getElementById('margin-card');
    if (!marginCard) return;
    
    // Show card only if margin is enabled
    if (!data.margin_enabled) {
      marginCard.style.display = 'none';
      return;
    }
    
    marginCard.style.display = 'block';
    
    const marginPct = document.getElementById('margin-pct');
    const marginDetails = document.getElementById('margin-details');
    
    if (marginPct) {
      const marginPctValue = Number(data.margin_utilization || 0);
      marginPct.textContent = marginPctValue.toFixed(1) + '%';
      // Color based on usage
      if (marginPctValue >= 80) {
        marginPct.style.color = '#ff4444'; // Red - dangerous
      } else if (marginPctValue >= 50) {
        marginPct.style.color = '#ffaa00'; // Orange - caution
      } else {
        marginPct.style.color = '#00dd88'; // Green - safe
      }
    }
    
    if (marginDetails) {
      const leverage = Number(data.margin_leverage || 1).toFixed(1);
      const deployed = fmt(data.gross_exposure);
      const remaining = fmt(data.remaining_buying_power);
      const isOverLeveraged = Number(data.margin_utilization || 0) > 100;
      const status = isOverLeveraged ? '⚠️ OVER-LEVERAGED' : '✅ Safe';
      
      let txt = `${leverage}x Leverage | Deployed: ${deployed} | Remaining: ${remaining} | ${status}`;
      if (data.forced_buy_enabled) {
        txt = '🔥 FORCED BUY ON | ' + txt;
      }
      marginDetails.textContent = txt;
      
      // Color the card border based on status
      if (isOverLeveraged) {
        marginCard.style.borderTop = '3px solid #ff4444';
      } else if (Number(data.margin_utilization || 0) > 50) {
        marginCard.style.borderTop = '3px solid #ffaa00';
      } else {
        marginCard.style.borderTop = '3px solid #00dd88';
      }
    }
  } catch (e) {
    console.error('Margin status fetch error:', e);
  }
}

function fmtPct(value) {
  return value !== null && value !== undefined ? `${parseFloat(value).toFixed(1)}%` : '—';
}

function renderAdaptiveAlert(item) {
  return `<div class="alert-item ${item.level}">
    ${item.message}
  </div>`;
}

function renderAdaptiveTableRow(cells) {
  return `<tr>${cells.map((cell) => `<td>${cell}</td>`).join('')}</tr>`;
}

async function fetchAdaptiveData() {
  try {
    const res = await fetch('/api/adaptive');
    const data = await res.json();
    if (!data.ok) return;

    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    set('adaptive-winrate', fmtPct(data.overall_win_rate));
    set('adaptive-market', `${data.config_summary.market_regime_multiplier || 1.0}x`);
    set('adaptive-changes', data.config_history.length || 0);
    set('adaptive-confidence', (data.average_confidence || 0).toFixed(2));

    // Determine if we have adaptive data
    const hasSignals = data.strategy_sets && data.strategy_sets.length > 0;
    const hasWindows = data.time_windows && data.time_windows.length > 0;
    const hasSymbols = data.symbols && data.symbols.length > 0;

    const signalsBody = document.getElementById('signals-body');
    if (signalsBody) {
      signalsBody.innerHTML = hasSignals
        ? data.strategy_sets.map((signal) => {
            const setName = signal.set_name || signal.signal_name || 'Unknown';
            const current = data.multiplier_history.find((row) => row.multiplier_type === 'signal' && row.multiplier_key === setName.toLowerCase().replace(/\s+/g, '_')) || {};
            return renderAdaptiveTableRow([
              `<strong>${setName}</strong><div class="strategy-sub">${signal.side || ''}</div>`,
              signal.total_trades || 0,
              fmtPct(signal.win_rate),
              signal.avg_rr?.toFixed(2) || '—',
              `${signal.avg_drawdown?.toFixed(1) || '—'}%`,
              current.multiplier_value?.toFixed(2) || '1.00',
              current.confidence_strength?.toFixed(2) || '0.00',
            ]);
          }).join('')
        : '<tr><td colspan="7" class="empty">⏳ Waiting for real trades... Strategy-set multipliers will appear after sets accumulate enough data (min 10 trades)</td></tr>';
    }

    const windowsBody = document.getElementById('windows-body');
    if (windowsBody) {
      windowsBody.innerHTML = hasWindows
        ? data.time_windows.map((window) => {
            const strength = window.win_rate >= 55 ? 'Strong' : window.win_rate < 50 ? 'Weak' : 'Neutral';
            return renderAdaptiveTableRow([
              window.time_window,
              window.trade_count || 0,
              fmtPct(window.win_rate),
              `₹${(window.avg_pnl || 0).toFixed(2)}`,
              fmtPct(window.failure_rate),
              strength,
            ]);
          }).join('')
        : '<tr><td colspan="6" class="empty">⏳ Adaptive system learning time windows... Execute trades in different time periods to build window analysis (min 20 trades per window)</td></tr>';
    }

    const symbolsBody = document.getElementById('symbols-body');
    if (symbolsBody) {
      symbolsBody.innerHTML = hasSymbols
        ? data.symbols.map((symbol) => {
            const current = data.multiplier_history.find((row) => row.multiplier_type === 'symbol_sl' && row.multiplier_key === symbol.symbol);
            return renderAdaptiveTableRow([
              symbol.symbol,
              symbol.volatility_profile || '—',
              fmtPct(symbol.sl_hit_freq),
              fmtPct(symbol.recovery_prob),
              `${symbol.avg_drawdown?.toFixed(1) || '—'}%`,
              current?.multiplier_value?.toFixed(2) || '1.00',
            ]);
          }).join('')
        : '<tr><td colspan="6" class="empty">⏳ Adaptive system learning symbol behavior... Trade multiple symbols to build behavior profiles (min 10 trades per symbol)</td></tr>';
    }

    const historyBody = document.getElementById('history-body');
    if (historyBody) {
      historyBody.innerHTML = data.change_history.length
        ? data.change_history.map((row) => renderAdaptiveTableRow([
            row.multiplier_type,
            row.multiplier_key,
            row.previous_value !== null ? row.previous_value.toFixed(2) : '—',
            row.new_value.toFixed(2),
            row.reason_source || 'adaptive_update',
            new Date(row.timestamp).toLocaleString('en-IN', { hour12: false }),
          ])).join('')
        : '<tr><td colspan="6" class="empty">📊 Multiplier history will appear here as the adaptive system updates multipliers based on trade outcomes</td></tr>';
    }

    const alertsList = document.getElementById('alerts-list');
    if (alertsList) {
      alertsList.innerHTML = data.alerts.length
        ? data.alerts.map((alert) => renderAdaptiveAlert(alert)).join('')
        : '<div class="empty">✅ No adaptive alerts — system is functioning normally</div>';
    }

    const reflection = data.reflection || {};
    if (reflection.one_line_summary) {
      const refEmpty = document.getElementById('reflection-empty');
      const refContent = document.getElementById('reflection-content');
      if (refEmpty && refContent) {
        refEmpty.style.display = 'none';
        refContent.style.display = 'block';
      }
      const refSummary = document.getElementById('ref-summary');
      if (refSummary) refSummary.textContent = reflection.one_line_summary || '—';
      const refWorked = document.getElementById('ref-worked');
      if (refWorked) refWorked.textContent = reflection.what_worked || '—';
      const refFailed = document.getElementById('ref-failed');
      if (refFailed) refFailed.textContent = reflection.what_failed || '—';
      const gradeEl = document.getElementById('ref-grade');
      if (gradeEl) {
        gradeEl.textContent = reflection.overall_grade || '—';
        gradeEl.className = reflection.overall_grade === 'A' ? 'grade-a' : reflection.overall_grade === 'B' ? 'grade-b' : reflection.overall_grade === 'C' ? 'grade-c' : 'grade-d';
      }
      const adjEl = document.getElementById('ref-adjustments');
      if (adjEl) {
        const adjustments = reflection.tomorrow_adjustments || [];
        adjEl.innerHTML = adjustments.length ? adjustments.map((a) => `🔧 ${a}`).join('<br>') : '—';
      }
    }
  } catch (e) {
    console.error('Adaptive dashboard fetch error:', e);
  }
}

function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}

// Emergency Square Off All
document.addEventListener('DOMContentLoaded', () => {
  const resumeBtn = document.getElementById('resume-trading-btn');
  if (resumeBtn) {
    resumeBtn.addEventListener('click', async () => {
      if (!confirm('Resume trading and allow new entries again?')) {
        return;
      }

      resumeBtn.disabled = true;
      resumeBtn.textContent = 'Resuming...';

      try {
        const res = await fetch('/api/trading-state', {
          method: 'POST',
          headers: { 
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
          },
          body: JSON.stringify({ action: 'resume', confirm_action: 'RESUME' }),
        });
        const data = await res.json();
        if (data.ok) {
          alert('Trading resumed. New entries are enabled.');
          await fetchAndRender();
        } else {
          alert(`Error: ${data.error}`);
        }
      } catch (e) {
        alert(`Network error: ${e.message}`);
      } finally {
        resumeBtn.disabled = false;
        resumeBtn.textContent = '▶ Resume Trading';
      }
    });
  }

  const btn = document.getElementById('squareoff-btn');
  if (btn) {
    btn.addEventListener('click', async () => {
      if (!confirm('EMERGENCY SQUARE OFF ALL POSITIONS?\n\nThis will immediately close every open position at market price.\n\nContinue?')) {
        return;
      }

      btn.disabled = true;
      btn.textContent = '⏳ Closing...';

      try {
        const res = await fetch('/api/emergency-squareoff', { 
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrfToken()
            },
            body: JSON.stringify({ confirm_action: 'SQUARE_OFF' })
        });
        const data = await res.json();
        
        // Debug log response structure
        console.log('Emergency squareoff API response:', data);
        console.log('  Response status:', res.status);
        console.log('  Data.ok:', data.ok);
        console.log('  Data.closed_count:', data.closed_count, '(type:', typeof data.closed_count + ')');
        console.log('  Data.failed_count:', data.failed_count, '(type:', typeof data.failed_count + ')');

        if (data.ok) {
          // Defensive fallback for undefined/null values
          const closed = (data.closed_count !== undefined && data.closed_count !== null) ? data.closed_count : 'unknown';
          const failed = (data.failed_count !== undefined && data.failed_count !== null) ? data.failed_count : 'unknown';
          
          console.log('Display values - Closed:', closed, 'Failed:', failed);
          alert(`Emergency squareoff complete!\n\nClosed: ${closed}\nFailed: ${failed}`);
          await fetchAndRender();
        } else {
          const errorMsg = data.error || 'Unknown error';
          console.error('Emergency squareoff error:', errorMsg);
          alert(`Error: ${errorMsg}`);
        }
      } catch (e) {
        console.error('Emergency squareoff fetch error:', e);
        alert(`Network error: ${e.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = '🚨 SQUARE OFF ALL';
      }
    });
  }
});

updateClock();
setInterval(updateClock, 1000);
fetchAndRender();
setInterval(fetchAndRender, 5000);
fetchAdaptiveData();
setInterval(fetchAdaptiveData, 10000);
