/* AlcoSoft — trading settings editor */

const GROUP_LABELS = {
  risk: '💰 Risk & exits (SL / TSL / targets)',
  strategy: '⚡ Strategy signals',
  screener: '🌅 Morning screener',
  market_data: '📡 Market data & candles',
  scheduling: '⏰ Scheduling',
};

function displayValue(field, value) {
  if (field.type === 'bool') return !!value;
  if (field.type === 'percent') return (parseFloat(value) * 100).toFixed(2);
  return value;
}

function parseInput(field, raw) {
  if (field.type === 'bool') return document.getElementById(`f-${field.section}-${field.key}`).checked;
  if (field.type === 'percent') return parseFloat(raw) / 100;
  if (field.type === 'int') return parseInt(raw, 10);
  return parseFloat(raw);
}

function buildForm(schema, settings) {
  const root = document.getElementById('settings-form');
  root.innerHTML = '';

  const bySection = {};
  schema.forEach((f) => {
    bySection[f.section] = bySection[f.section] || [];
    bySection[f.section].push(f);
  });

  Object.keys(bySection).forEach((section) => {
    const group = document.createElement('div');
    group.className = 'card settings-group';
    group.innerHTML = `<h2>${GROUP_LABELS[section] || section}</h2>`;

    const grid = document.createElement('div');
    grid.className = 'field-grid';

    bySection[section].forEach((field) => {
      const val = settings[section]?.[field.key];
      const div = document.createElement('div');
      div.className = 'field';

      if (field.type === 'bool') {
        div.className = 'field field-check';
        div.innerHTML = `
          <input type="checkbox" id="f-${field.section}-${field.key}" ${val ? 'checked' : ''}/>
          <label for="f-${field.section}-${field.key}">${field.label}</label>`;
      } else {
        const inputType = field.type === 'int' ? 'number' : 'number';
        const step = field.step || (field.type === 'int' ? 1 : 0.01);
        div.innerHTML = `
          <label for="f-${field.section}-${field.key}">${field.label}</label>
          <input type="${inputType}" id="f-${field.section}-${field.key}"
            value="${displayValue(field, val)}"
            min="${field.min ?? ''}" max="${field.max ?? ''}" step="${step}"/>
          ${field.hint ? `<div class="hint">${field.hint}</div>` : ''}`;
      }
      grid.appendChild(div);
    });

    group.appendChild(grid);
    root.appendChild(group);
  });
}

async function loadSettings() {
  const res = await fetch('/api/settings');
  const data = await res.json();
  buildForm(data.schema, data.settings);
  const meta = document.getElementById('settings-meta');
  if (meta && data.settings._meta) {
    meta.textContent = 'Last updated: ' + (data.settings._meta.updated_at || 'defaults');
  }
}

async function saveSettings(ev) {
  ev.preventDefault();
  const res = await fetch('/api/settings');
  const { schema, settings } = await res.json();
  const updates = {};

  schema.forEach((field) => {
    const el = document.getElementById(`f-${field.section}-${field.key}`);
    if (!el) return;
    updates[field.section] = updates[field.section] || {};
    updates[field.section][field.key] = parseInput(field, el.value);
  });

  const saveRes = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  const out = await saveRes.json();
  const toast = document.getElementById('toast');
  if (saveRes.ok) {
    toast.className = 'toast ok';
    toast.textContent = '✅ Saved! main.py picks up changes within ~5 seconds.';
    loadSettings();
  } else {
    toast.className = 'toast err';
    toast.textContent = '❌ ' + (out.errors?.join(' ') || out.error || 'Save failed');
  }
}

document.getElementById('save-btn').addEventListener('click', saveSettings);
loadSettings();
