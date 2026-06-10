/* AlcoSoft — trading settings editor (redesigned) */

const GROUP_LABELS = {
  risk: '💰 Risk & exits (SL / TSL / targets / Margin)',
  strategy: '⚡ Strategy signals',
  strategy_sets: 'Strategy set toggles',
  screener: '🌅 Morning screener',
  market_data: '📡 Market data & candles',
  scheduling: '⏰ Scheduling',
};

const GROUP_DESCRIPTIONS = {
  risk: 'Trading risk controls including stop losses, targets, and margin settings.',
  strategy: 'Strategy-set mode, position limits, lookback, and loop timing used by the trading engine.',
  strategy_sets: 'Choose which BUY and SELL strategy sets are allowed to run. OFF means that set will not be used.',
  screener: 'Morning stock selection and cognition candidate settings.',
  market_data: 'Market data refresh rates and websocket health thresholds.',
  scheduling: 'Timing for cognition scans and strategy loop execution.',
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
  const panels = document.querySelectorAll('.settings-panel');
  
  // Clear all panels
  panels.forEach(p => p.innerHTML = '');

  const bySection = {};
  schema.forEach((f) => {
    bySection[f.section] = bySection[f.section] || [];
    bySection[f.section].push(f);
  });

  Object.keys(bySection).forEach((section) => {
    const panel = document.querySelector(`.settings-panel[data-panel="${section}"]`);
    if (!panel) return;

    // Add section label and description
    const header = document.createElement('div');
    header.className = 'settings-section';
    
    const label = document.createElement('span');
    label.className = 'settings-section-label';
    label.textContent = GROUP_LABELS[section] || section;
    
    const desc = document.createElement('div');
    desc.className = 'settings-section-desc';
    desc.textContent = GROUP_DESCRIPTIONS[section] || '';
    
    header.appendChild(label);
    header.appendChild(desc);
    panel.appendChild(header);

    // Group fields by pairs (for 2-column layout)
    const fields = bySection[section];
    const groups = [];
    let currentPair = [];
    
    for (let i = 0; i < fields.length; i++) {
      if (fields[i].type === 'bool') {
        // Push any pending pair first
        if (currentPair.length > 0) {
          groups.push(currentPair);
          currentPair = [];
        }
        // Boolean fields get their own full-width row
        groups.push([fields[i]]);
      } else {
        currentPair.push(fields[i]);
        if (currentPair.length === 2) {
          groups.push(currentPair);
          currentPair = [];
        }
      }
    }
    // Push any remaining unpaired field
    if (currentPair.length > 0) {
      groups.push(currentPair);
    }

    // Build field groups
    groups.forEach((group) => {
      const fieldGroup = document.createElement('div');
      fieldGroup.className = 'settings-field-group';
      
      group.forEach((field) => {
        const val = settings[section]?.[field.key];
        
        if (field.type === 'bool') {
          // Toggle field (full width)
          const div = document.createElement('div');
          div.className = 'settings-field-toggle';
          
          const info = document.createElement('div');
          info.className = 'settings-toggle-info';
          
          const label = document.createElement('div');
          label.className = 'settings-field-label';
          label.textContent = field.label;
          
          let hint = '';
          if (field.hint) {
            hint = `<div class="settings-field-hint">${field.hint}</div>`;
          }
          
          info.innerHTML = `${label.outerHTML}${hint}`;
          
          const toggle = document.createElement('div');
          toggle.className = `settings-toggle ${val ? 'on' : ''}`;
          toggle.id = `f-${field.section}-${field.key}`;
          toggle.setAttribute('data-value', val ? '1' : '0');
          
          toggle.addEventListener('click', () => {
            toggle.classList.toggle('on');
            toggle.setAttribute('data-value', toggle.classList.contains('on') ? '1' : '0');
          });
          
          div.appendChild(info);
          div.appendChild(toggle);
          fieldGroup.appendChild(div);
        } else {
          // Number/text field
          const div = document.createElement('div');
          div.className = 'settings-field';
          
          const inputType = 'number';
          const step = field.step || (field.type === 'int' ? 1 : 0.01);
          
          let fieldHtml = `
            <label class="settings-field-label" for="f-${field.section}-${field.key}">${field.label}</label>
            <input type="${inputType}" id="f-${field.section}-${field.key}"
              value="${displayValue(field, val)}"
              min="${field.min ?? ''}" max="${field.max ?? ''}" step="${step}"/>
          `;
          
          if (field.hint) {
            fieldHtml += `<div class="settings-field-hint">${field.hint}</div>`;
          }
          
          div.innerHTML = fieldHtml;
          fieldGroup.appendChild(div);
        }
      });
      
      panel.appendChild(fieldGroup);
    });
  });
}

function setupTabs() {
  const tabs = document.querySelectorAll('.settings-tab');
  const panels = document.querySelectorAll('.settings-panel');
  
  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      // Remove active from all
      tabs.forEach(t => t.classList.remove('active'));
      panels.forEach(p => p.classList.remove('active'));
      
      // Add active to clicked tab
      tab.classList.add('active');
      
      // Show corresponding panel
      const tabName = tab.getAttribute('data-tab');
      const panel = document.querySelector(`.settings-panel[data-panel="${tabName}"]`);
      if (panel) {
        panel.classList.add('active');
      }
    });
  });
}

async function loadSettings() {
  const res = await fetch('/api/settings');
  const data = await res.json();
  buildForm(data.schema, data.settings);
  setupTabs();
  
  // Update meta info
  const metaEl = document.querySelector('.guidance-item:last-child');
  if (metaEl && data.settings._meta?.updated_at) {
    metaEl.innerHTML = `
      <span class="guidance-icon">🕐</span>
      <div>
        <strong>Last Updated</strong>
        <p>${new Date(data.settings._meta.updated_at).toLocaleString()}</p>
      </div>
    `;
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
    
    // Handle toggle switches differently
    if (field.type === 'bool') {
      updates[field.section][field.key] = el.classList.contains('on');
    } else {
      updates[field.section][field.key] = parseInput(field, el.value);
    }
  });

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  const saveRes = await fetch('/api/settings', {
    method: 'POST',
    headers: { 
      'Content-Type': 'application/json',
      'X-CSRFToken': getCsrfToken()
    },
    body: JSON.stringify(updates),
  });
  
  const out = await saveRes.json();
  const toast = document.getElementById('toast');
  
  if (saveRes.ok) {
    toast.className = 'toast show success';
    toast.textContent = '✅ Settings saved! main.py picks up changes within ~5 seconds.';
    loadSettings();
    setTimeout(() => toast.classList.remove('show'), 4000);
  } else {
    toast.className = 'toast show error';
    toast.textContent = '❌ ' + (out.errors?.join(' ') || out.error || 'Save failed');
    setTimeout(() => toast.classList.remove('show'), 4000);
  }
}

document.getElementById('save-btn').addEventListener('click', saveSettings);
loadSettings();
