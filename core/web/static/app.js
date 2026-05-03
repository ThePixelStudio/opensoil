/* OpenSoil Web UI — vanilla JS, no framework */

// ── Sensor display config ─────────────────────────────────────────────────────
const SENSOR_META = {
  dht11_temp:     { label: 'Temperature', unit: '°C', color: '#fb923c', axis: 'y1', min: 0,  max: 50  },
  dht11_humidity: { label: 'Humidity',    unit: '%',  color: '#60a5fa', axis: 'y2', min: 0,  max: 100 },
  soil_resistive: { label: 'Soil',        unit: '%',  color: '#4ade80', axis: 'y2', min: 0,  max: 100 },
};

function sensorMeta(id) {
  if (SENSOR_META[id]) return SENSOR_META[id];
  // Generic fallback for unknown sensors
  return { label: id, unit: '', color: '#a78bfa', axis: 'y2', min: 0, max: 100 };
}

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  activeTab:  'dashboard',
  status:     null,
  profile:    null,
  sensors:    {},
  chartHours: 24,
  chart:      null,
};

// ── API helper ────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

// ── Format helpers ────────────────────────────────────────────────────────────
function timeAgo(sec) {
  if (sec < 60)   return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}
function fmtDateTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function cmdBadge(key, val) {
  const isOn  = val === 'on';
  const isPwm = typeof val === 'number';
  const cls   = isPwm ? 'cmd-pwm' : isOn ? 'cmd-on' : 'cmd-off';
  const label = isPwm ? `${key}: ${val}%` : `${key}: ${val}`;
  return `<span class="cmd-badge ${cls}">${label}</span>`;
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn')
    .forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-pane')
    .forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  state.activeTab = name;
  if (name === 'profile')   loadProfile();
  if (name === 'chat')      loadChat();
  if (name === 'decisions') loadDecisions();
  if (name === 'events')    loadEvents();
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const [status, sensors, profile, actuators] = await Promise.all([
      api('/api/status'),
      api('/api/sensors/latest'),
      api('/api/profile'),
      api('/api/actuators'),
    ]);
    state.status    = status;
    state.sensors   = sensors;
    state.profile   = profile;
    state.actuators = actuators;

    document.getElementById('box-id').textContent    = status.box_id;
    document.getElementById('day-badge').textContent  = `Day ${status.day}`;
    document.getElementById('phase-badge').textContent = status.phase;

    renderProfilePanel(status, profile, sensors);
    renderSensorCards(sensors, profile);
    renderActuators(actuators);
    renderLastDecision(status.last_decision);
    await loadChart(state.chartHours);
  } catch (e) {
    console.error('Dashboard load failed:', e);
  }
}

function renderProfilePanel(status, profile, sensors) {
  const el = document.getElementById('profile-panel');
  if (!profile || !profile.phases || profile.phases.length === 0) {
    el.innerHTML = '<div class="dim">No profile loaded</div>';
    return;
  }

  const phases = profile.phases;
  const day    = profile.current_day;

  // Compute timeline spans — cap at 90 days for display
  const displayMax = Math.min(profile.expected_duration_days || 90, 90);
  const segs = phases.map(p => {
    const lo  = p.days[0];
    const hi  = Math.min(p.days[1], displayMax);
    const pct = Math.max(((hi - lo) / displayMax) * 100, 4);
    const cls = p.status === 'active' ? 'phase-active'
              : p.status === 'completed' ? 'phase-done' : 'phase-upcoming';
    return `<div class="phase-seg ${cls}" style="flex:${pct}"
               title="${p.name}: Day ${p.days[0]}–${p.days[1] > 900 ? '∞' : p.days[1]}">
              <span class="phase-label">${p.name}</span>
            </div>`;
  }).join('');

  const markerPct = Math.min((day / displayMax) * 100, 99);

  // Active phase targets vs live sensors
  const activePhase = phases.find(p => p.status === 'active') || {};
  const targets     = activePhase.targets || {};
  const targetCards = Object.entries(targets).map(([sid, range]) => {
    const s    = sensors[sid];
    const val  = s ? s.value : null;
    const meta = sensorMeta(sid);
    const cls  = s ? (s.status === 'ok' ? '' : s.status === 'low' ? 'warn' : 'danger') : '';
    const icon = s ? (s.status === 'ok' ? '✅' : s.status === 'low' ? '⚠️' : '🔴') : '';
    return `<div class="target-card ${cls}">
      <div class="target-name">${meta.label}</div>
      <div class="target-range">${range[0]}–${range[1]} ${meta.unit}</div>
      <div class="target-val">${icon} ${val !== null ? val.toFixed(1) : '—'}</div>
    </div>`;
  }).join('');

  const nextPhase = phases.find(p => p.status === 'upcoming');
  const countdown = nextPhase && profile.days_until_next_phase != null
    ? `${profile.days_until_next_phase}d until ${nextPhase.name}` : '';

  el.innerHTML = `
    <div class="profile-header">
      <div>
        <span class="profile-name">🌿 ${profile.name}</span>
        ${profile.cultivar ? `<span class="profile-cultivar">· ${profile.cultivar}</span>` : ''}
      </div>
      <div class="profile-meta">${countdown}</div>
    </div>
    <div class="phase-timeline-wrap">
      <div class="phase-timeline">${segs}</div>
      <div class="phase-marker-row">
        <div class="phase-marker" style="left:${markerPct}%">
          <div class="phase-marker-line"></div>
          <div class="phase-marker-label">Day ${day}</div>
        </div>
      </div>
    </div>
    <div class="target-cards">${targetCards}</div>
    ${activePhase.notes ? `<div class="phase-note">📝 ${activePhase.notes}</div>` : ''}
  `;
}

function renderSensorCards(sensors, profile) {
  const el = document.getElementById('sensor-cards');
  if (!sensors || Object.keys(sensors).length === 0) {
    el.innerHTML = '<div class="card dim" style="grid-column:1/-1;padding:20px;text-align:center">No sensor data yet — is the ESP8266 connected?</div>';
    return;
  }

  el.innerHTML = Object.entries(sensors).map(([sid, s]) => {
    const meta     = sensorMeta(sid);
    const statusCls = s.status === 'ok' ? '' : s.status === 'low' ? 'warn' : 'danger';
    const icon     = s.status === 'ok' ? '' : s.status === 'low' ? '⚠️' : '🔴';
    const trendPct = s.trend;
    const trendCls = trendPct > 0 ? 'up' : trendPct < 0 ? 'down' : '';
    const trendStr = trendPct !== 0
      ? `${trendPct > 0 ? '↑' : '↓'} ${Math.abs(trendPct).toFixed(2)}/day` : 'stable';

    const bar = rangeBar(s.value, s.target, meta);
    const targetStr = s.target ? `${s.target[0]}–${s.target[1]} ${meta.unit}` : '—';

    return `<div class="sensor-card ${statusCls}">
      ${icon ? `<div class="sensor-status-icon">${icon}</div>` : ''}
      <div class="sensor-label">${meta.label}</div>
      <div class="sensor-value">${s.value.toFixed(1)}<span class="sensor-unit">${meta.unit}</span></div>
      <div class="sensor-trend ${trendCls}">${trendStr}</div>
      ${bar}
      <div class="sensor-meta">target: ${targetStr} · ${timeAgo(s.age_sec)}</div>
    </div>`;
  }).join('');
}

function rangeBar(value, target, meta) {
  if (!target) return '';
  const scale = v => Math.min(Math.max((v - meta.min) / (meta.max - meta.min) * 100, 0), 100);
  const tL  = scale(target[0]);
  const tW  = scale(target[1]) - tL;
  const vP  = scale(value);
  return `<div class="range-bar-wrap">
    <div class="range-track">
      <div class="range-zone" style="left:${tL}%;width:${tW}%"></div>
      <div class="range-val" style="left:${vP}%"></div>
    </div>
  </div>`;
}

function renderLastDecision(d) {
  const el = document.getElementById('last-decision-card');
  if (!d) { el.innerHTML = '<div class="empty">No decisions yet</div>'; return; }
  const cmds = Object.entries(d.commands).map(([k, v]) => cmdBadge(k, v)).join('');
  el.innerHTML = `
    <div class="decision-header">
      <span class="dim">Last decision</span>
      <span class="dim">${fmtDateTime(d.ts)} · ${d.model} · ${d.latency_ms}ms</span>
    </div>
    <div class="cmd-row">${cmds}</div>
    <div class="decision-reason">${d.reason}</div>
    ${d.was_overridden ? '<div class="override-badge">⚠️ Safety override applied</div>' : ''}
  `;
}

// ── Chart ─────────────────────────────────────────────────────────────────────
async function loadChart(hours) {
  state.chartHours = hours;
  document.querySelectorAll('.range-btn')
    .forEach(b => b.classList.toggle('active', parseInt(b.dataset.hours) === hours));

  const data = await api(`/api/history?hours=${hours}`);

  const datasets = Object.entries(data).map(([sid, series]) => {
    const meta = sensorMeta(sid);
    return {
      label: `${meta.label} (${meta.unit})`,
      data: series.points.map(p => ({ x: p.ts, y: p.value })),
      borderColor: meta.color,
      backgroundColor: meta.color + '18',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
      yAxisID: meta.axis,
    };
  });

  const ctx = document.getElementById('sensor-chart').getContext('2d');

  if (state.chart) {
    state.chart.data.datasets = datasets;
    state.chart.update('none');
    return;
  }

  state.chart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#7b84a3', boxWidth: 12, font: { size: 12 } } },
        tooltip: { backgroundColor: '#1b1f2e', borderColor: '#2e3352', borderWidth: 1 },
      },
      scales: {
        x: {
          type: 'time',
          time: {
            tooltipFormat: 'HH:mm',
            displayFormats: { minute: 'HH:mm', hour: 'HH:mm', day: 'MMM d' },
          },
          ticks: { color: '#7b84a3', maxTicksLimit: 8 },
          grid:  { color: '#1e2235' },
        },
        y1: {
          type: 'linear',
          position: 'left',
          title: { display: true, text: '°C', color: '#7b84a3', font: { size: 11 } },
          ticks: { color: '#7b84a3' },
          grid:  { color: '#1e2235' },
        },
        y2: {
          type: 'linear',
          position: 'right',
          min: 0,
          max: 100,
          title: { display: true, text: '%', color: '#7b84a3', font: { size: 11 } },
          ticks: { color: '#7b84a3' },
          grid:  { drawOnChartArea: false },
        },
      },
    },
  });
}

// ── Profile tab ───────────────────────────────────────────────────────────────
async function loadProfile() {
  try {
    const [profile, sensors] = await Promise.all([
      api('/api/profile'),
      api('/api/sensors/latest'),
    ]);
    state.profile = profile;
    state.sensors = sensors;
    renderProfileDetail(profile, sensors);
  } catch (e) {
    document.getElementById('profile-detail').innerHTML = `<div class="dim">Error: ${e.message}</div>`;
  }
}

function renderProfileDetail(profile, sensors) {
  const el = document.getElementById('profile-detail');
  const phases = profile.phases || [];

  const phasesHtml = phases.map(p => {
    const icon = p.status === 'active' ? '▶' : p.status === 'completed' ? '✓' : '○';

    const targetsHtml = Object.entries(p.targets || {}).map(([sid, range]) => {
      const meta     = sensorMeta(sid);
      const current  = sensors[sid];
      const val      = current ? current.value : null;
      const scale    = v => Math.min(Math.max((v - meta.min) / (meta.max - meta.min) * 100, 0), 100);
      const tL       = scale(range[0]);
      const tW       = scale(range[1]) - tL;
      const vP       = val !== null ? scale(val) : null;
      const sStatus  = val !== null ? (val < range[0] ? 'low' : val > range[1] ? 'high' : 'ok') : '';

      return `<div class="phase-target-row">
        <span class="phase-target-label">${meta.label}</span>
        <span class="phase-target-range">${range[0]}–${range[1]} ${meta.unit}</span>
        <div class="range-track wide">
          <div class="range-zone" style="left:${tL}%;width:${tW}%"></div>
          ${vP !== null && p.status === 'active'
            ? `<div class="range-val ${sStatus !== 'ok' ? 'warn' : ''}" style="left:${vP}%"></div>`
            : ''}
        </div>
        ${val !== null && p.status === 'active'
          ? `<span class="phase-target-current ${sStatus}">${val.toFixed(1)}</span>`
          : ''}
      </div>`;
    }).join('');

    const dayLabel = p.days[1] > 900 ? '∞' : p.days[1];
    return `<div class="phase-block ${p.status}">
      <div class="phase-block-header">
        <span class="phase-status-icon">${icon}</span>
        <span class="phase-name">${p.name}</span>
        <span class="phase-days dim">Day ${p.days[0]}–${dayLabel}</span>
      </div>
      ${p.notes ? `<div class="phase-notes dim">${p.notes}</div>` : ''}
      ${targetsHtml}
    </div>`;
  }).join('');

  const startDate = new Date(profile.started_at * 1000).toLocaleDateString();
  el.innerHTML = `
    <div class="profile-detail-header">
      <h2>🌿 ${profile.name}</h2>
      ${profile.cultivar ? `<div class="cultivar">${profile.cultivar}</div>` : ''}
      ${profile.notes    ? `<div class="profile-notes dim">${profile.notes}</div>` : ''}
      <div class="profile-stats">
        <span>📅 Started ${startDate}</span>
        <span>📆 Day ${profile.current_day}${profile.expected_duration_days ? ' of ' + profile.expected_duration_days : ''}</span>
        ${profile.harvest_days          ? `<span>🌾 Harvest at day ${profile.harvest_days}</span>` : ''}
        ${profile.days_until_next_phase ? `<span>⏭ ${profile.days_until_next_phase}d until next phase</span>` : ''}
      </div>
    </div>
    <div class="phases-list">${phasesHtml}</div>
  `;
}

// ── Chat ──────────────────────────────────────────────────────────────────────
async function loadChat() {
  try {
    const history = await api('/api/chat_history');
    state.chatMessages = history;
    renderChatMessages();
  } catch (e) {
    document.getElementById('chat-messages').innerHTML =
      `<div class="dim">Error loading chat: ${e.message}</div>`;
  }
}

function renderChatMessages() {
  const el = document.getElementById('chat-messages');
  if (!state.chatMessages.length) {
    el.innerHTML = '<div class="chat-empty dim">No messages yet.<br>Type an observation below or ask a question.</div>';
    return;
  }
  el.innerHTML = state.chatMessages.map(m => {
    const cls   = m.role === 'user' ? 'chat-msg-user'
                : m.type === 'system' ? 'chat-msg-system' : 'chat-msg-assistant';
    const label = m.role === 'user' ? 'You' : `OpenSoil ${m.model ? '· ' + m.model : ''}`;
    return `<div class="chat-msg ${cls}">
      <div class="chat-msg-meta">${m.ts_str || ''} <span class="chat-label">${label}</span></div>
      <div class="chat-msg-text">${escHtml(m.text)}</div>
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const send  = document.getElementById('chat-send');
  const text  = input.value.trim();
  if (!text) return;

  input.value   = '';
  input.disabled = true;
  send.disabled  = true;

  const ts_str = new Date().toLocaleString();
  state.chatMessages.push({
    role: 'user',
    type: text.endsWith('?') ? 'question' : 'observation',
    text, ts_str,
  });
  renderChatMessages();

  try {
    const resp = await api('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    state.chatMessages.push({
      role:  'assistant',
      type:  resp.type,
      text:  resp.response,
      model: resp.model || '',
      ts_str: new Date().toLocaleString(),
    });
  } catch (e) {
    state.chatMessages.push({
      role: 'assistant', type: 'system',
      text: `Error: ${e.message}`, ts_str: new Date().toLocaleString(),
    });
  }

  renderChatMessages();
  input.disabled = false;
  send.disabled  = false;
  input.focus();
}

// ── Decisions ─────────────────────────────────────────────────────────────────
async function loadDecisions() {
  const el = document.getElementById('decisions-wrap');
  try {
    const decisions = await api('/api/decisions?limit=30');
    if (!decisions.length) {
      el.innerHTML = '<div class="empty">No decisions recorded yet</div>';
      return;
    }
    const rows = decisions.map(d => {
      const cmds = Object.entries(d.commands).map(([k, v]) => cmdBadge(k, v)).join('');
      const modelShort = (d.model || '').split('-').slice(-2).join('-');
      return `<tr ${d.was_overridden ? 'class="overridden"' : ''}>
        <td class="dim" style="white-space:nowrap">${fmtDateTime(d.ts)}</td>
        <td><span class="phase-pill">${d.phase}</span></td>
        <td><div class="cmd-row" style="margin:0;gap:4px">${cmds}</div></td>
        <td class="reason-cell">${escHtml(d.reason)}</td>
        <td class="dim small" style="white-space:nowrap">${modelShort}<br>${d.latency_ms}ms</td>
      </tr>`;
    }).join('');

    el.innerHTML = `<table class="decisions-table">
      <thead>
        <tr>
          <th>Time</th><th>Phase</th><th>Commands</th>
          <th>Reason</th><th>Model</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (e) {
    el.innerHTML = `<div class="dim">Error: ${e.message}</div>`;
  }
}

// ── Events ────────────────────────────────────────────────────────────────────
async function loadEvents() {
  const el = document.getElementById('events-wrap');
  try {
    const events = await api('/api/events?limit=50&hours=48');
    if (!events.length) {
      el.innerHTML = '<div class="empty">No events in the last 48h</div>';
      return;
    }
    const icons = {
      SENSOR_ANOMALY:      '🔴',
      SAFETY_OVERRIDE:     '⚠️',
      ACTUATOR_FIRED:      '⚡',
      DOMAIN_STARTED:      'ℹ️',
      POLL_ERROR:          '❌',
      LLM_FAILURE_SAFE_OFF:'🛡️',
      DURATION_LIMIT:      '⏱️',
      ACTUATOR_NO_EFFECT:  '🔇',
    };
    const rows = events.map(e => `
      <div class="event-row sev-${e.severity}">
        <span class="event-icon">${icons[e.type] || '●'}</span>
        <span class="event-ts">${fmtDateTime(e.ts)}</span>
        <span class="event-type">${e.type}</span>
        <span class="event-note">${escHtml(e.note || '')}</span>
      </div>`).join('');

    el.innerHTML = `<div class="events-feed">${rows}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="dim">Error: ${e.message}</div>`;
  }
}

// ── Actuators ─────────────────────────────────────────────────────────────────
async function loadActuators() {
  try {
    const actuators = await api('/api/actuators');
    state.actuators = actuators;
    renderActuators(actuators);
  } catch (e) {
    document.getElementById('actuator-list').innerHTML =
      `<div class="dim" style="padding:12px">Error: ${e.message}</div>`;
  }
}

function renderActuators(actuators) {
  const el = document.getElementById('actuator-list');
  if (!actuators || actuators.length === 0) {
    el.innerHTML = '<div class="dim" style="padding:12px">No actuators configured</div>';
    return;
  }
  el.innerHTML = actuators.map(a => {
    const isOn   = a.state === 'on';
    const isOff  = a.state === 'off';
    const dotCls = isOn ? 'actuator-dot on' : isOff ? 'actuator-dot off' : 'actuator-dot unknown';
    const stateLabel = isOn ? 'ON' : isOff ? 'OFF' : a.state;
    const age = a.ts ? timeAgo(Math.floor(Date.now() / 1000 - a.ts)) : '';
    return `<div class="actuator-item" id="act-${a.id}">
      <div class="actuator-info">
        <span class="${dotCls}"></span>
        <span class="actuator-name">${a.name}</span>
        <span class="actuator-state ${isOn ? 'state-on' : isOff ? 'state-off' : ''}">${stateLabel}</span>
        ${age ? `<span class="actuator-age dim">${age}</span>` : ''}
      </div>
      <div class="actuator-btns">
        <button class="act-btn act-btn-on ${isOn ? 'active' : ''}"
          onclick="controlActuator('${a.id}', 'on')" ${isOn ? 'disabled' : ''}>ON</button>
        <button class="act-btn act-btn-off ${isOff ? 'active' : ''}"
          onclick="controlActuator('${a.id}', 'off')" ${isOff ? 'disabled' : ''}>OFF</button>
      </div>
    </div>`;
  }).join('');
}

async function controlActuator(id, value) {
  // Optimistic UI update
  const row = document.getElementById(`act-${id}`);
  if (row) row.style.opacity = '0.5';

  try {
    await api(`/api/actuator/${id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    });
    // Refresh actuator list after short delay so DB event is written
    setTimeout(loadActuators, 800);
  } catch (e) {
    if (row) row.style.opacity = '1';
    alert(`Failed to control ${id}: ${e.message}`);
  }
}

// ── SSE live stream ───────────────────────────────────────────────────────────
function connectStream() {
  const dot = document.getElementById('conn-dot');
  const es  = new EventSource('/api/stream');

  es.onopen = () => dot.classList.add('live');
  es.onerror = () => {
    dot.classList.remove('live');
    // EventSource auto-reconnects — no manual retry needed
  };

  es.onmessage = (e) => {
    try {
      const payload = JSON.parse(e.data);
      if (payload.error) return;

      if (payload.sensors) state.sensors = payload.sensors;
      if (payload.status)  state.status  = payload.status;

      // Update header badges from SSE
      if (payload.status) {
        document.getElementById('day-badge').textContent   = `Day ${payload.status.day}`;
        document.getElementById('phase-badge').textContent = payload.status.phase;
      }

      // Refresh visible tab without a full reload
      if (state.activeTab === 'dashboard' && payload.sensors) {
        renderSensorCards(payload.sensors, state.profile);
        if (state.profile) renderProfilePanel(payload.status, state.profile, payload.sensors);
        if (payload.status?.last_decision) renderLastDecision(payload.status.last_decision);
        if (payload.actuators) renderActuators(payload.actuators);
      }
    } catch { /* malformed message — ignore */ }
  };
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Tab switching
  document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab))
  );

  // Chart range buttons
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.addEventListener('click', () => loadChart(parseInt(btn.dataset.hours)))
  );

  // Chat
  document.getElementById('chat-send').addEventListener('click', sendMessage);
  document.getElementById('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) sendMessage();
  });

  // Start SSE
  connectStream();

  // Initial dashboard load
  loadDashboard();

  // Refresh dashboard data every 2 minutes for last-decision + day counter
  setInterval(() => {
    if (state.activeTab === 'dashboard') loadDashboard();
  }, 120_000);
});
