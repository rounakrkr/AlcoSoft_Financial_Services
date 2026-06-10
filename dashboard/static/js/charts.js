/* AlcoSoft — dashboard charts (Chart.js) */

let pnlChart = null;
let winChart = null;
let cumChart = null;

const CHART_COLORS = {
  green:  '#17a865',
  red:    '#e03660',
  accent: '#5b62f0',
  border: '#dde3f5',
  text:   '#6b728e',
};

function renderCharts(charts) {
  if (!charts || typeof Chart === 'undefined') return;

  renderPnlBars(charts);
  renderWinLoss(charts);
  renderCumulative(charts);
  renderSystemStatus(charts);
}

function renderPnlBars(charts) {
  const canvas = document.getElementById('chart-pnl-bars');
  if (!canvas) return;

  const labels = charts.trade_labels || [];
  const data = charts.trade_pnl || [];
  const colors = data.map((v) => (v >= 0 ? CHART_COLORS.green : CHART_COLORS.red));

  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: labels.length ? labels : ['No trades'],
      datasets: [{
        label: 'P&L ₹',
        data: data.length ? data : [0],
        backgroundColor: colors.length ? colors : [CHART_COLORS.border],
        borderRadius: 6,
      }],
    },
    options: chartOpts('Per-trade P&L'),
  });
}

function renderCumulative(charts) {
  const canvas = document.getElementById('chart-cumulative');
  if (!canvas) return;

  const labels = charts.trade_labels || [];
  const data = charts.cumulative_pnl || [];

  if (cumChart) cumChart.destroy();
  cumChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels: labels.length ? labels : ['—'],
      datasets: [{
        label: 'Cumulative ₹',
        data: data.length ? data : [0],
        borderColor: CHART_COLORS.accent,
        backgroundColor: 'rgba(91, 98, 240, 0.12)',
        fill: true,
        tension: 0.35,
        pointRadius: 3,
      }],
    },
    options: chartOpts('Running P&L'),
  });
}

function renderWinLoss(charts) {
  const canvas = document.getElementById('chart-winloss');
  if (!canvas) return;

  const wins = charts.wins || 0;
  const losses = charts.losses || 0;

  if (winChart) winChart.destroy();
  winChart = new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: ['Gross Profit ₹', 'Gross Loss ₹'],
      datasets: [{
        data: [wins, losses],
        backgroundColor: [CHART_COLORS.green, CHART_COLORS.red],
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: CHART_COLORS.text, boxWidth: 12 } },
        title: {
          display: true,
          text: 'Profit vs Loss',
          color: CHART_COLORS.text,
          font: { size: 11 },
        },
      },
    },
  });
}

function renderSystemStatus(charts) {
  const el = document.getElementById('system-status');
  if (!el) return;

  const cb = charts.circuit_breakers || {};
  const breakersHtml = Object.keys(cb).length
    ? Object.entries(cb).map(([name, info]) => {
        const st = (info.state || 'CLOSED').toUpperCase();
        const dot = st === 'OPEN' ? 'bad' : st === 'HALF_OPEN' ? 'warn' : 'ok';
        return `<div class="status-row">
          <span><span class="status-dot ${dot}"></span>${name}</span>
          <span style="color:var(--text2)">${st}${info.failures ? ` (${info.failures} err)` : ''}</span>
        </div>`;
      }).join('')
    : '<div class="empty" style="padding:0.5rem">Circuit status N/A</div>';

  const ov = charts.order_verify || {};
  const feed = charts.feed || {};

  el.innerHTML = `
    <div class="status-row">
      <span>📅 Market</span>
      <span style="color:var(--text2);text-align:right;max-width:55%">${charts.market_status || '—'}</span>
    </div>
    <div class="status-row">
      <span>📡 Live feed</span>
      <span style="color:var(--text2)">${feed.tick_total || 0} ticks · ${feed.subscribed || 0} symbols</span>
    </div>
    <div class="status-row">
      <span>✅ Orders verified</span>
      <span style="color:var(--text2)">${ov.verified || 0} / ${ov.total || 0}</span>
    </div>
    ${breakersHtml}
  `;
}

function chartOpts(title) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      title: {
        display: true,
        text: title,
        color: CHART_COLORS.text,
        font: { size: 11 },
      },
    },
    scales: {
      x: {
        ticks: { color: CHART_COLORS.text, maxRotation: 45, font: { size: 10 } },
        grid: { display: false },
      },
      y: {
        ticks: { color: CHART_COLORS.text, font: { size: 10 } },
        grid: { color: CHART_COLORS.border },
      },
    },
  };
}
