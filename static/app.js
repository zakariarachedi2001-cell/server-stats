const $ = s => document.querySelector(s);
let type = 'java';
let pollTimer = null;
let isPolling = false;
let currentTarget = null;
let previousPlayers = new Set();
let activityLog = [];

// Theme Management (Dark / Light Mode)
const savedTheme = localStorage.getItem('mc_checker_theme') || 'dark';
applyTheme(savedTheme);

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('mc_checker_theme', theme);
  $('#themeIcon').textContent = theme === 'dark' ? '☀️' : '🌙';
  $('#themeLabel').textContent = theme === 'dark' ? 'Light Mode' : 'Dark Mode';
}

$('#themeToggle').onclick = () => {
  const current = document.documentElement.getAttribute('data-theme');
  applyTheme(current === 'dark' ? 'light' : 'dark');
};

// Minecraft Formatting Color Codes
const CODES = {
  0: '#000000', 1: '#0000AA', 2: '#00AA00', 3: '#00AAAA',
  4: '#AA0000', 5: '#AA00AA', 6: '#FFAA00', 7: '#AAAAAA',
  8: '#555555', 9: '#5555FF', a: '#55FF55', b: '#55FFFF',
  c: '#FF5555', d: '#FF55FF', e: '#FFFF55', f: '#FFFFFF'
};

const NAMES = {
  black: '0', dark_blue: '1', dark_green: '2', dark_aqua: '3',
  dark_red: '4', dark_purple: '5', gold: '6', gray: '7',
  dark_gray: '8', blue: '9', green: 'a', aqua: 'b',
  red: 'c', light_purple: 'd', yellow: 'e', white: 'f'
};

const esc = t => t.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function span(text, st) {
  if (!text) return '';
  let css = '', cls = '';
  if (st.color) css += `color:${st.color};`;
  if (st.b) css += 'font-weight:bold;';
  if (st.i) css += 'font-style:italic;';
  const deco = [st.u && 'underline', st.s && 'line-through'].filter(Boolean).join(' ');
  if (deco) css += `text-decoration:${deco};`;
  if (st.o) cls = ' class="obf"';
  return `<span${cls} style="${css}">${esc(text)}</span>`;
}

// Legacy section-sign (§) text formatting
function legacy(text, st) {
  let out = '', buf = '';
  st = { ...st };
  const flush = () => { out += span(buf, st); buf = ''; };
  for (let i = 0; i < text.length; i++) {
    if ((text[i] === '§' || text[i] === '\u00c2') && i + 1 < text.length) {
      if (text[i] === '\u00c2') continue;
      flush();
      const c = text[++i].toLowerCase();
      if (c in CODES) st = { color: CODES[c] };
      else if (c === 'l') st.b = true;
      else if (c === 'o') st.i = true;
      else if (c === 'n') st.u = true;
      else if (c === 'm') st.s = true;
      else if (c === 'k') st.o = true;
      else if (c === 'r') st = {};
    } else {
      buf += text[i];
    }
  }
  flush();
  return out;
}

// JSON Chat Component parser
function comp(c, parent = {}) {
  if (typeof c === 'string') return legacy(c, parent);
  if (Array.isArray(c)) return c.map(x => comp(x, parent)).join('');
  const st = { ...parent };
  if (c.color) st.color = c.color.startsWith('#') ? c.color : (CODES[NAMES[c.color]] || st.color);
  for (const [k, v] of [['bold', 'b'], ['italic', 'i'], ['underlined', 'u'], ['strikethrough', 's'], ['obfuscated', 'o']]) {
    if (c[k] !== undefined) st[v] = !!c[k];
  }
  return legacy(c.text || '', st) + (c.extra || []).map(x => comp(x, st)).join('');
}

const renderMotd = raw => comp(raw);

// Edition Switcher
document.querySelectorAll('.edition .mc-btn').forEach(b => b.onclick = () => {
  type = b.dataset.type;
  document.querySelectorAll('.edition .mc-btn').forEach(x => {
    x.classList.toggle('active', x === b);
    x.setAttribute('aria-checked', x === b);
  });
});

$('#addr').addEventListener('keydown', e => e.key === 'Enter' && startTracking());
$('#go').onclick = startTracking;
$('#stopBtn').onclick = stopTracking;

function showMsg(t) {
  $('#msg').textContent = t;
  $('#msg').hidden = !t;
}

function stopTracking() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  currentTarget = null;
  $('#liveBadge').hidden = true;
  $('#stopBtn').hidden = true;
  $('#go').disabled = false;
  $('#go').textContent = 'Check';
}

async function startTracking() {
  const address = $('#addr').value.trim();
  if (!address) return showMsg('Please enter a server address first.');

  if (pollTimer) clearTimeout(pollTimer);
  previousPlayers = new Set();
  activityLog = [];
  $('#activityFeed').hidden = true;
  $('#activityFeed').innerHTML = '';

  currentTarget = { address, type };
  showMsg('');
  $('#go').disabled = true;
  $('#go').textContent = '...';

  await fetchStatus(true);
}

async function fetchStatus(isInitial = false) {
  if (!currentTarget || isPolling) return;
  isPolling = true;
  const { address, type: targetType } = currentTarget;

  try {
    const r = await fetch(`/api/status?type=${targetType}&address=${encodeURIComponent(address)}`);
    const d = await r.json();

    if (!currentTarget || currentTarget.address !== address) return;

    if (d.error) {
      if (isInitial) {
        $('#result').hidden = true;
        showMsg(d.error);
        stopTracking();
      } else {
        $('#statStatus').textContent = 'OFFLINE';
        $('#statStatus').className = 'stat-value offline-txt';
      }
      return;
    }

    showMsg('');
    $('#liveBadge').hidden = false;
    $('#stopBtn').hidden = false;
    render(d, isInitial);

    pollTimer = setTimeout(() => fetchStatus(false), 1000);
  } catch {
    if (isInitial) {
      showMsg('Failed to connect to the backend service.');
      stopTracking();
    }
  } finally {
    isPolling = false;
    $('#go').disabled = false;
    $('#go').textContent = 'Check';
  }
}

function trackPlayerActivity(players, isInitial) {
  const currentNames = new Set(players.map(p => p.name));
  if (!isInitial) {
    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    currentNames.forEach(name => {
      if (!previousPlayers.has(name)) {
        activityLog.unshift({ type: 'join', text: `[${now}] + ${name} joined the game` });
      }
    });
    previousPlayers.forEach(name => {
      if (!currentNames.has(name)) {
        activityLog.unshift({ type: 'leave', text: `[${now}] - ${name} left the game` });
      }
    });
    activityLog = activityLog.slice(0, 5);
  }
  previousPlayers = currentNames;

  const feed = $('#activityFeed');
  if (activityLog.length > 0) {
    feed.hidden = false;
    feed.innerHTML = activityLog
      .map(item => `<div class="${item.type === 'join' ? 'feed-join' : 'feed-leave'}">${esc(item.text)}</div>`)
      .join('');
  }
}

function render(d, isInitial) {
  $('#result').hidden = false;

  $('#statStatus').textContent = 'ONLINE';
  $('#statStatus').className = 'stat-value online-txt';
  $('#statPlayers').textContent = `${d.players_online} / ${d.players_max}`;
  $('#statPing').textContent = `${d.latency} ms`;
  $('#statSync').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const pct = d.players_max > 0 ? Math.min(100, Math.round((d.players_online / d.players_max) * 100)) : 0;
  $('#xpFill').style.width = `${pct}%`;
  $('#xpLabel').textContent = `${pct}% Capacity (${d.players_online}/${d.players_max})`;

  $('#motd').innerHTML = renderMotd(d.motd_raw);
  $('#lname').textContent = `${d.host}:${d.port}`;
  $('#lcount').textContent = `${d.players_online}/${d.players_max}`;

  const icon = $('#icon'), dl = $('#dl');
  if (d.icon) {
    if (icon.src !== d.icon) icon.src = d.icon;
    dl.hidden = false;
    $('#noicon').hidden = true;
    dl.href = `/icons/${d.icon_file}?dl=1`;
    dl.download = d.icon_file;
  } else {
    icon.src = '/static/logo.svg';
    dl.hidden = true;
    $('#noicon').hidden = d.edition !== 'bedrock';
  }

  const rows = [
    ['Edition', d.edition === 'java' ? 'Java Edition' : 'Bedrock Edition'],
    ['Host', d.host],
    ['Port', d.port],
    ['Version', d.version],
    ['Protocol', d.protocol],
    ['Latency', d.latency + ' ms']
  ];
  if (d.edition === 'bedrock') {
    rows.push(['Gamemode', d.gamemode], ['World Map', d.map], ['Server Brand', d.brand]);
  }
  $('#info').innerHTML = rows
    .filter(r => r[1] !== null && r[1] !== undefined && r[1] !== '')
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(String(v))}</dd></div>`)
    .join('');

  $('#playerBadge').textContent = d.players_online;
  trackPlayerActivity(d.players, isInitial);

  const ul = $('#players');
  ul.innerHTML = '';
  d.players.forEach(p => {
    const li = document.createElement('li');
    li.innerHTML = `<img src="https://minotar.net/helm/${encodeURIComponent(p.name)}/32.png" alt="" onerror="this.style.visibility='hidden'"><span>${legacy(p.name, {})}</span>`;
    ul.appendChild(li);
  });

  $('#pnote').textContent = d.players.length
    ? ''
    : (d.edition === 'bedrock'
        ? 'Bedrock Edition servers only report player counts, not individual player names.'
        : (d.players_online ? 'This server hides its online player list.' : 'No players currently online.'));
}
