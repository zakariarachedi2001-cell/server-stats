const $ = s => document.querySelector(s);
let type = 'java';
let pollTimer = null;
let isPolling = false;
let currentTarget = null;
let previousPlayers = new Set();
let activityLog = [];
let currentView = 'server';

// ---- Visitor counter -------------------------------------------------------
// The browser is registered once (first-party cookie, no IPs), then the count is
// refreshed every 20 seconds. Polling pauses while the tab is hidden.
const VISITOR_POLL_MS = 20000;
const STATUS_POLL_MS = 3000;
let lastVisitorCount = null;

function showVisitorCount(value) {
  const el = $('#visitorCount');
  const n = Number(value || 0);
  if (n === lastVisitorCount) return;
  el.textContent = n.toLocaleString();
  if (lastVisitorCount !== null) {
    el.classList.remove('bump');
    void el.offsetWidth; // restart the animation
    el.classList.add('bump');
  }
  lastVisitorCount = n;
}

async function registerVisitor() {
  try {
    const r = await fetch('/api/visitors', { cache: 'no-store' });
    if (!r.ok) throw new Error('unavailable');
    showVisitorCount((await r.json()).unique_visitors);
  } catch {
    if (lastVisitorCount === null) $('#visitorCount').textContent = '—';
  }
}

let visitorTimer = null;
async function pollVisitors() {
  clearTimeout(visitorTimer);
  if (!document.hidden) {
    try {
      const r = await fetch('/api/visitors/count', { cache: 'no-store' });
      if (r.ok) showVisitorCount((await r.json()).unique_visitors);
    } catch { /* keep the last known value */ }
  }
  visitorTimer = setTimeout(pollVisitors, VISITOR_POLL_MS);
}

registerVisitor().finally(pollVisitors);
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollVisitors(); });

// Theme Management (Dark / Light Mode)
function readTheme() {
  try { return localStorage.getItem('mc_checker_theme') === 'light' ? 'light' : 'dark'; }
  catch { return 'dark'; }
}
const savedTheme = readTheme();
applyTheme(savedTheme);

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('mc_checker_theme', theme); } catch { /* storage blocked */ }
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
function comp(c, parent = {}, depth = 0) {
  if (depth > 20 || c === null || typeof c !== 'object' && typeof c !== 'string') return '';
  if (typeof c === 'string') return legacy(c, parent);
  if (Array.isArray(c)) return c.map(x => comp(x, parent, depth + 1)).join('');
  const st = { ...parent };
  if (typeof c.color === 'string') {
    // Only accept #RRGGBB or a known colour name: the value ends up inside a style attribute.
    if (/^#[0-9a-fA-F]{6}$/.test(c.color)) st.color = c.color;
    else if (c.color in NAMES) st.color = CODES[NAMES[c.color]];
  }
  for (const [k, v] of [['bold', 'b'], ['italic', 'i'], ['underlined', 'u'], ['strikethrough', 's'], ['obfuscated', 'o']]) {
    if (c[k] !== undefined) st[v] = !!c[k];
  }
  return legacy(typeof c.text === 'string' ? c.text : '', st) + (Array.isArray(c.extra) ? c.extra : []).map(x => comp(x, st, depth + 1)).join('');
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

function scheduleNext(target, delay = STATUS_POLL_MS) {
  if (currentTarget !== target || currentView !== 'server') return;
  pollTimer = setTimeout(() => fetchStatus(false), delay);
}

async function fetchStatus(isInitial = false) {
  if (!currentTarget || isPolling) return;
  isPolling = true;
  const target = currentTarget;
  const { address, type: targetType } = target;

  try {
    const r = await fetch(`/api/status?type=${targetType}&address=${encodeURIComponent(address)}`);
    const d = await r.json();

    if (currentTarget !== target) return;
    const retryDelay = r.status === 429 ? 10000 : STATUS_POLL_MS;

    if (d.error) {
      if (isInitial) {
        $('#result').hidden = true;
        showMsg(d.error);
        stopTracking();
      } else {
        // Keep polling so the page recovers by itself when the server comes back.
        $('#statStatus').textContent = 'OFFLINE';
        $('#statStatus').className = 'stat-value offline-txt';
        $('#liveText').textContent = r.status === 429 ? 'SLOWING DOWN' : 'RETRYING';
        scheduleNext(target, retryDelay);
      }
      return;
    }

    showMsg('');
    $('#liveText').textContent = 'LIVE • 3s';
    $('#liveBadge').hidden = false;
    $('#stopBtn').hidden = false;
    render(d, isInitial);
    scheduleNext(target);
  } catch {
    if (currentTarget !== target) return;
    if (isInitial) {
      showMsg('Failed to connect to the backend service.');
      stopTracking();
    } else {
      $('#liveText').textContent = 'RECONNECTING';
      scheduleNext(target);
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
  if (typeof d.icon === 'string' && d.icon.startsWith('data:image/png;base64,')) {
    if (icon.src !== d.icon) icon.src = d.icon;
    dl.hidden = false;
    $('#noicon').hidden = true;
    dl.href = d.icon;
    dl.download = `${(d.host || 'minecraft-server').replace(/[^a-z0-9._-]/gi, '_')}-${d.port || 25565}.png`;
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
    // Only real-looking usernames get an avatar request; no inline handlers (CSP-safe).
    if (/^[A-Za-z0-9_]{1,16}$/.test(p.name)) {
      const img = document.createElement('img');
      img.alt = ''; img.width = img.height = 32;
      img.src = `https://minotar.net/helm/${encodeURIComponent(p.name)}/32.png`;
      img.addEventListener('error', () => { img.style.visibility = 'hidden'; });
      li.appendChild(img);
    }
    const nameEl = document.createElement('span');
    nameEl.innerHTML = legacy(String(p.name), {}); // legacy() escapes all text
    li.appendChild(nameEl);
    ul.appendChild(li);
  });

  $('#pnote').textContent = d.players.length
    ? ''
    : (d.edition === 'bedrock'
        ? 'Bedrock Edition servers only report player counts, not individual player names.'
        : (d.players_online ? 'This server hides its online player list.' : 'No players currently online.'));
}

// ---- Tabs (Creeper = servers, Steve = players) ------------------------------
function setView(view, updateHash = true) {
  if (!['server', 'player', 'pixel'].includes(view)) view = 'server';
  const previous = currentView;
  currentView = view;
  document.querySelectorAll('.tile').forEach(t => {
    const on = t.dataset.view === view;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on);
    t.tabIndex = on ? 0 : -1;
  });
  $('#view-server').hidden = view !== 'server';
  $('#view-player').hidden = view !== 'player';
  $('#view-pixel').hidden = view !== 'pixel';
  if (updateHash && location.hash !== '#' + view) history.replaceState(null, '', '#' + view);

  if (view !== 'server') {
    // Pause live server polling (and hide its LIVE badge) while the player tab is open.
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    $('#liveBadge').hidden = true;
    if (view === 'player' && previous !== 'player') setTimeout(() => $('#pname').focus(), 0);
  } else if (previous !== 'server' && currentTarget && !pollTimer) {
    $('#liveBadge').hidden = false;
    fetchStatus(false);
  }
}
document.querySelectorAll('.tile').forEach(t => t.onclick = () => setView(t.dataset.view));
document.querySelector('.hub').addEventListener('keydown', e => {
  const tiles = [...document.querySelectorAll('.tile')];
  const i = tiles.findIndex(t => t.dataset.view === currentView);
  let n = -1;
  if (e.key === 'ArrowRight') n = (i + 1) % tiles.length;
  else if (e.key === 'ArrowLeft') n = (i - 1 + tiles.length) % tiles.length;
  else if (e.key === 'Home') n = 0;
  else if (e.key === 'End') n = tiles.length - 1;
  if (n < 0) return;
  e.preventDefault();
  setView(tiles[n].dataset.view);
  tiles[n].focus();
});
window.addEventListener('hashchange', () => setView(location.hash.slice(1), false));

// ---- Player lookup -----------------------------------------------------------
let playerData = null;
let skinImage = null;
let skinSide = 'front';

const pmsg = t => { $('#pmsg').textContent = t; $('#pmsg').hidden = !t; };

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}

// Draws a 16x32 (x k) front or back view of a 64x64 / 64x32 skin.
function drawSkin(canvas, img, slim, side) {
  const k = img.width / 64;
  const legacy = img.height <= img.width / 2;
  canvas.width = 16 * k;
  canvas.height = 32 * k;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const blit = (sx, sy, w, h, dx, dy, flip = false) => {
    ctx.save();
    if (flip) { ctx.translate((dx + w) * k, dy * k); ctx.scale(-1, 1); dx = 0; dy = 0; }
    ctx.drawImage(img, sx * k, sy * k, w * k, h * k, dx * k, dy * k, w * k, h * k);
    ctx.restore();
  };

  const aw = slim ? 3 : 4;
  const front = side === 'front';

  // source x of an arm's front/back face (right arm / left arm)
  const rArm = front ? 44 : (slim ? 51 : 52);
  const lArm = front ? 36 : (slim ? 43 : 44);
  const lSleeve = front ? 52 : (slim ? 59 : 60);

  // Layout in the picture: viewer's left holds the player's RIGHT arm/leg from the front,
  // and the player's LEFT arm/leg from the back.
  const leftArmX = slim ? 1 : 0, rightArmX = 12;

  // Base layer
  blit(front ? 8 : 24, 8, 8, 8, 4, 0);                    // head
  blit(front ? 20 : 32, 20, 8, 12, 4, 8);                 // body
  if (front) {
    blit(rArm, 20, aw, 12, leftArmX, 8);                  // right arm
    legacy ? blit(rArm, 20, aw, 12, rightArmX, 8, true) : blit(lArm, 52, aw, 12, rightArmX, 8);
    blit(4, 20, 4, 12, 4, 20);                            // right leg
    legacy ? blit(4, 20, 4, 12, 8, 20, true) : blit(20, 52, 4, 12, 8, 20);
  } else {
    legacy ? blit(rArm, 20, aw, 12, leftArmX, 8, true) : blit(lArm, 52, aw, 12, leftArmX, 8);
    blit(rArm, 20, aw, 12, rightArmX, 8);
    legacy ? blit(12, 20, 4, 12, 4, 20, true) : blit(28, 52, 4, 12, 4, 20);
    blit(12, 20, 4, 12, 8, 20);
  }

  // Overlay layer (hat, jacket, sleeves, pants)
  blit(front ? 40 : 56, 8, 8, 8, 4, 0);
  if (!legacy) {
    blit(front ? 20 : 32, 36, 8, 12, 4, 8);
    if (front) {
      blit(rArm, 36, aw, 12, leftArmX, 8);
      blit(lSleeve, 52, aw, 12, rightArmX, 8);
      blit(4, 36, 4, 12, 4, 20);
      blit(4, 52, 4, 12, 8, 20);
    } else {
      blit(lSleeve, 52, aw, 12, leftArmX, 8);
      blit(rArm, 36, aw, 12, rightArmX, 8);
      blit(12, 52, 4, 12, 4, 20);
      blit(12, 36, 4, 12, 8, 20);
    }
  }
}

function drawHead(canvas, img, size) {
  const k = img.width / 64;
  const tmp = document.createElement('canvas');
  tmp.width = tmp.height = 8 * k;
  const t = tmp.getContext('2d');
  t.imageSmoothingEnabled = false;
  t.drawImage(img, 8 * k, 8 * k, 8 * k, 8 * k, 0, 0, 8 * k, 8 * k);
  t.drawImage(img, 40 * k, 8 * k, 8 * k, 8 * k, 0, 0, 8 * k, 8 * k);
  canvas.width = canvas.height = size;
  const c = canvas.getContext('2d');
  c.imageSmoothingEnabled = false;
  c.drawImage(tmp, 0, 0, size, size);
}

function setSkinSide(side) {
  skinSide = side;
  $('#viewFront').classList.toggle('active', side === 'front');
  $('#viewBack').classList.toggle('active', side === 'back');
  if (skinImage && playerData) drawSkin($('#skinCanvas'), skinImage, playerData.skin_model === 'slim', side);
}
$('#viewFront').onclick = () => setSkinSide('front');
$('#viewBack').onclick = () => setSkinSide('back');

async function searchPlayer() {
  const query = $('#pname').value.trim();
  if (!query) return pmsg('Please enter a player name first.');
  pmsg('');
  const btn = $('#pgo');
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch(`/api/player?query=${encodeURIComponent(query)}`);
    const d = await r.json();
    if (!r.ok || d.error) {
      $('#presult').hidden = true;
      return pmsg(d.error || 'Something went wrong. Please try again.');
    }
    await renderPlayer(d);
  } catch {
    $('#presult').hidden = true;
    pmsg('Failed to connect to the backend service.');
  } finally {
    btn.disabled = false; btn.textContent = 'Search';
  }
}
$('#pgo').onclick = searchPlayer;
$('#pname').addEventListener('keydown', e => e.key === 'Enter' && searchPlayer());

function copyRow(label, value, copyable = true) {
  const div = document.createElement('div');
  const dt = document.createElement('dt'); dt.textContent = label;
  const dd = document.createElement('dd'); dd.textContent = value;
  div.append(dt, dd);
  if (copyable && value !== null && value !== undefined) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'copy-btn'; b.textContent = 'COPY';
    b.onclick = async () => {
      try { await navigator.clipboard.writeText(String(value)); b.textContent = 'COPIED'; }
      catch { b.textContent = 'FAILED'; }
      setTimeout(() => (b.textContent = 'COPY'), 1200);
    };
    div.appendChild(b);
  }
  return div;
}

async function renderPlayer(d) {
  playerData = d;
  skinImage = null;
  $('#presult').hidden = false;
  $('#pdName').textContent = d.name;

  const badges = [d.skin_model === 'slim' ? 'SLIM (ALEX)' : 'CLASSIC (STEVE)'];
  if (d.cape_hash) badges.push('HAS CAPE');
  if (d.legacy) badges.push('LEGACY ACCOUNT');
  $('#pdBadges').innerHTML = badges.map(b => `<span class="badge">${esc(b)}</span>`).join('');
  $('#namemc').href = d.namemc_url;

  const dlSkin = $('#dlSkin'), dlCape = $('#dlCape'), dlHead = $('#dlHead'), note = $('#pdNote');
  dlSkin.hidden = dlCape.hidden = dlHead.hidden = true;
  note.hidden = true;

  if (d.skin_hash) {
    const base = `/api/texture/${d.skin_hash}.png`;
    dlSkin.href = `${base}?download=1&name=${encodeURIComponent(d.name + '-skin')}`;
    dlSkin.hidden = false;
    try {
      skinImage = await loadImage(base);
      drawHead($('#headCanvas'), skinImage, 64);
      setSkinSide(skinSide);
      dlHead.hidden = false;
    } catch {
      note.textContent = 'The skin image could not be loaded right now.';
      note.hidden = false;
    }
  } else {
    // No custom skin returned: show the default look.
    const fallback = await loadImage('/static/steve.svg').catch(() => null);
    const hc = $('#headCanvas'); hc.width = hc.height = 64;
    if (fallback) { const c = hc.getContext('2d'); c.imageSmoothingEnabled = false; c.drawImage(fallback, 0, 0, 64, 64); }
    $('#skinCanvas').getContext('2d').clearRect(0, 0, 999, 999);
    note.textContent = 'This player uses a default skin, so there is no custom skin file to download.';
    note.hidden = false;
  }

  if (d.cape_hash) {
    dlCape.href = `/api/texture/${d.cape_hash}.png?download=1&name=${encodeURIComponent(d.name + '-cape')}`;
    dlCape.hidden = false;
  }

  const rows = [
    ['Username', d.name],
    ['UUID', d.uuid],
    ['UUID (no dashes)', d.uuid_compact],
    ['UUID (int array)', `[I; ${d.uuid_ints.join(', ')}]`],
    ['Offline-mode UUID', d.offline_uuid],
    ['Skin model', d.skin_model === 'slim' ? 'Slim (3px arms)' : 'Classic (4px arms)', false],
    ['Cape', d.cape_hash ? 'Yes' : 'No', false],
    ['Skin URL', d.skin_url],
    ['Cape URL', d.cape_url],
    ['Textures updated', d.textures_updated ? new Date(d.textures_updated).toLocaleString() : null, false],
  ];
  const info = $('#pInfo');
  info.innerHTML = '';
  rows.filter(r => r[1] !== null && r[1] !== undefined && r[1] !== '')
      .forEach(([k, v, copyable]) => info.appendChild(copyRow(k, v, copyable !== false)));
}

$('#dlHead').onclick = () => {
  if (!skinImage || !playerData) return;
  const c = document.createElement('canvas');
  drawHead(c, skinImage, 512);
  c.toBlob(blob => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${playerData.name}-head.png`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }, 'image/png');
};

setView(location.hash.slice(1), false);
