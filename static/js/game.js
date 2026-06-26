// Bomberman client
const socket = io();
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
ctx.imageSmoothingEnabled = true;
ctx.imageSmoothingQuality = "low";

// Logical viewport — we always show exactly VIEW_TILES_X x VIEW_TILES_Y
// tiles around the player, regardless of window size. TILE_PX is computed
// from the window so the same 15x13 view fits any monitor.
const VIEW_TILES_X = 15;
const VIEW_TILES_Y = 13;
let TILE_PX = 40;

let MAP_W = 43, MAP_H = 37;
let grid = null;
let snap = null;       // latest authoritative snapshot from server
let prevSnap = null;   // previous snapshot, for interpolation
let snapTime = 0;      // performance.now() when `snap` arrived
let prevSnapTime = 0;  // when `prevSnap` arrived
let mySid = null;
let myName = null;
let isHost = false;
let phase = "lobby";
let spectating = false;

// --- Client-side prediction ---
// We simulate our own player locally so movement feels instant.
// The server snapshot reconciles position softly (no hard teleport).
const predicted = { x: 0, y: 0, speed: 3.0, alive: true };
let predictionActive = false;  // only true once we have an initial position
let lastTickTime = null;

// Debug overlay — press B to toggle.
let debugMode = false;

// Outgoing position update throttle. We tell the server where we are 20x
// per second (every 50ms). The server trusts us and uses it verbatim.
let lastPosSent = 0;
const POS_SEND_INTERVAL_MS = 50;

// Preload all 20 player icons up-front so rendering never blocks on disk IO.
const ICON_IMGS = {};
for (let i = 1; i <= 20; i++) {
  const img = new Image();
  img.src = `/static/imgs/player_icons/${i}.png`;
  ICON_IMGS[i] = img;
}

// Tile sprites.
const TILE_IMGS = {};
let tilesReady = 0;
for (const k of ["floor", "wall1", "unbreakable", "wall2"]) {
  const img = new Image();
  img.onload = () => { tilesReady++; mapDirty = true; };
  img.src = `/static/imgs/${k}.png`;
  TILE_IMGS[k] = img;
}

// Poison sprite — overlay rendered in-game, not in the static mapCanvas
// (because individual poison tiles can armed/disarmed independently and
// we want to pulse them).
const POISON_IMG = new Image();
POISON_IMG.src = "/static/imgs/poison.png";

const mapCanvas = document.createElement("canvas");
const mapCtx = mapCanvas.getContext("2d");
let mapDirty = true;
const MAP_TILE = 48;

const cam = { x: 0, y: 0 };
let viewW = window.innerWidth;
let vignette = null;
let viewH = window.innerHeight - 0;

function resize() {
  viewW = window.innerWidth;
  viewH = window.innerHeight;
  canvas.width = viewW;
  canvas.height = viewH;
  TILE_PX = Math.max(16, Math.floor(Math.min(
    viewW / VIEW_TILES_X,
    viewH / VIEW_TILES_Y,
  )));
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "low";
  vignette = null;
}
window.addEventListener("resize", resize);
resize();

// --- Client-side movement simulation ---
// Mirrors app.py move_player / passable logic so prediction matches server.
function tileAt(x, y) {
  if (!grid) return 1;
  if (x < 0 || y < 0 || x >= MAP_W || y >= MAP_H) return 1;
  return grid[y][x];
}

function hasBombAt(tx, ty) {
  if (!snap || !snap.bombs) return false;
  for (const b of snap.bombs) {
    if (b.x === tx && b.y === ty) return true;
  }
  return false;
}

function passableLocal(tx, ty, fromX, fromY) {
  if (tileAt(tx, ty) !== 0) return false;
  // A bomb blocks its tile UNLESS we're already standing on it (so we can
  // step off a bomb we just placed).
  if (hasBombAt(tx, ty)) {
    return tx === fromX && ty === fromY;
  }
  return true;
}

function moveLocal(p, dir, dt) {
  const dirs = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
  if (!dirs[dir]) return;
  const [dx, dy] = dirs[dir];
  const step = p.speed * dt;
  const curTx = Math.floor(p.x);
  const curTy = Math.floor(p.y);

  if (dx !== 0) {
    const nx = p.x + dx * step;
    const edge = nx + 0.45 * dx;
    const tx = Math.floor(edge);
    const ty = Math.floor(p.y);
    if (passableLocal(tx, ty, curTx, curTy)) {
      p.x = nx;
    } else {
      p.x = Math.floor(p.x) + 0.5;
    }
  }
  if (dy !== 0) {
    const ny = p.y + dy * step;
    const tx = Math.floor(p.x);
    const edge = ny + 0.45 * dy;
    const ty = Math.floor(edge);
    if (passableLocal(tx, ty, curTx, curTy)) {
      p.y = ny;
    } else {
      p.y = Math.floor(p.y) + 0.5;
    }
  }
}

// --- Input ---
const keys = {};
let lastDir = null;
function currentDir() {
  if (keys["ArrowUp"] || keys["w"] || keys["W"]) return "up";
  if (keys["ArrowDown"] || keys["s"] || keys["S"]) return "down";
  if (keys["ArrowLeft"] || keys["a"] || keys["A"]) return "left";
  if (keys["ArrowRight"] || keys["d"] || keys["D"]) return "right";
  return null;
}
window.addEventListener("keydown", (e) => {
  if (e.repeat) return;
  keys[e.key] = true;
  if (e.code === "KeyB") {
    debugMode = !debugMode;
    socket.emit("debug_view", { on: debugMode });
    mapDirty = true;
    const ind = document.getElementById("debug-indicator");
    if (ind) ind.classList.toggle("hidden", !debugMode);
    e.preventDefault();
    return;
  }
  if (spectating) return;
  if (e.code === "Space") {
    socket.emit("bomb");
    e.preventDefault();
  }
  const d = currentDir();
  if (d !== lastDir) {
    lastDir = d;
    socket.emit("input", { dir: d });
  }
});
window.addEventListener("keyup", (e) => {
  keys[e.key] = false;
  if (spectating) return;
  const d = currentDir();
  if (d !== lastDir) {
    lastDir = d;
    socket.emit("input", { dir: d });
  }
});

// --- Socket ---
socket.on("hello", (d) => {
  mySid = d.sid; myName = d.you; isHost = !!d.is_host;
  updateHostUI();
});

socket.on("lobby", (d) => {
  phase = d.phase;
  document.getElementById("countdown").textContent = Math.max(0, d.remaining);
  document.getElementById("pcount").textContent = d.count;
  isHost = (d.host_sid === mySid);
  updateHostUI();
  const list = document.getElementById("plist");
  list.innerHTML = "";
  d.players.forEach(p => {
    const li = document.createElement("li");
    const icon = document.createElement("img");
    icon.className = "plist-icon";
    icon.src = `/static/imgs/player_icons/${p.icon || 1}.png`;
    li.appendChild(icon);
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = p.color;
    li.appendChild(sw);
    const txt = document.createElement("span");
    txt.textContent = `${p.name} ${p.is_bot ? "🤖" : ""}`;
    li.appendChild(txt);
    list.appendChild(li);
  });
});

function updateHostUI() {
  const btn = document.getElementById("start-now");
  const hint = document.getElementById("host-hint");
  if (!btn || !hint) return;
  if (isHost && phase !== "playing") {
    btn.classList.remove("hidden");
    hint.classList.remove("hidden");
  } else {
    btn.classList.add("hidden");
    hint.classList.add("hidden");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const startBtn = document.getElementById("start-now");
  if (startBtn) startBtn.addEventListener("click", () => socket.emit("start_now"));
  const termBtn = document.getElementById("terminate-btn");
  if (termBtn) termBtn.addEventListener("click", () => {
    if (confirm("Terminar a partida e mostrar a leaderboard?")) {
      socket.emit("terminate");
    }
  });
});
{
  const startBtn = document.getElementById("start-now");
  if (startBtn) startBtn.addEventListener("click", () => socket.emit("start_now"));
  const termBtn = document.getElementById("terminate-btn");
  if (termBtn) termBtn.addEventListener("click", () => {
    if (confirm("Terminar a partida e mostrar a leaderboard?")) {
      socket.emit("terminate");
    }
  });
}

socket.on("start", (d) => {
  grid = d.grid;
  MAP_W = d.map_w; MAP_H = d.map_h;
  phase = "playing";
  spectating = !!d.spectating;
  predictionActive = false;
  mapDirty = true;
  document.getElementById("lobby").classList.add("hidden");
  document.getElementById("endscreen").classList.add("hidden");
  updateSpectatorBar();
});

socket.on("snap", (s) => {
  prevSnap = snap;
  prevSnapTime = snapTime;
  snap = s;
  snapTime = performance.now();
  if (s.grid_diff) {
    grid = s.grid_diff;
    mapDirty = true;
  }

  // Movement is fully client-authoritative for our own player — we send our
  // position to the server, not the other way around. We only sync from the
  // server snapshot on:
  //   - the first snap after (re)spawn, to seed our position
  //   - when the server says we're dead (so we stop predicting)
  //   - if somehow we drift impossibly far from server (>3 tiles): hard snap
  //     (defensive, shouldn't happen normally).
  if (!spectating) {
    const me = s.players.find(p => p.id === mySid);
    if (me) {
      predicted.speed = me.speed;
      predicted.alive = me.alive;
      if (!predictionActive) {
        predicted.x = me.x;
        predicted.y = me.y;
        predictionActive = true;
      } else {
        const dx = me.x - predicted.x;
        const dy = me.y - predicted.y;
        if (dx * dx + dy * dy > 9) {
          // 3+ tile divergence — something is very wrong, hard snap.
          predicted.x = me.x;
          predicted.y = me.y;
        }
        // Otherwise: trust the client. Server position is ignored.
      }
    }
  }

  const wasSpectating = spectating;
  spectating = !!s.spectating;
  if (wasSpectating !== spectating) updateSpectatorBar();
  if (spectating) updateSpectatorBar();
});

socket.on("end", (d) => {
  spectating = false;
  predictionActive = false;
  updateSpectatorBar();
  document.getElementById("winner").textContent = d.winner || "—";
  renderLeaderboard(d.leaderboard || []);
  document.getElementById("endscreen").classList.remove("hidden");
});

socket.on("reset", () => {
  document.getElementById("endscreen").classList.add("hidden");
  document.getElementById("lobby").classList.remove("hidden");
  spectating = false;
  predictionActive = false;
  grid = null; snap = null;
});

socket.on("full", () => {
  alert("Lobby cheio!");
});

socket.on("spectator", () => {
  spectating = true;
  updateSpectatorBar();
});

// --- Spectator UI ---
function updateSpectatorBar() {
  const bar = document.getElementById("spectator-bar");
  const term = document.getElementById("terminate-btn");
  const hud = document.getElementById("hud");
  if (!bar || !term) return;
  if (spectating && phase === "playing") {
    bar.classList.remove("hidden");
    const onlyBots = snap && snap.only_bots_left;
    term.classList.toggle("hidden", !onlyBots);
    if (hud) hud.classList.add("hidden");
  } else {
    bar.classList.add("hidden");
    if (hud) hud.classList.remove("hidden");
  }
}

// --- Leaderboard render ---
function renderLeaderboard(rows) {
  const root = document.getElementById("leaderboard");
  root.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    li.className = "lb-row" + (r.is_winner ? " winner" : "");

    const rank = document.createElement("div");
    rank.className = "lb-rank";
    rank.textContent = r.is_winner ? "🏆" : `#${r.rank}`;
    li.appendChild(rank);

    const icon = document.createElement("img");
    icon.className = "lb-icon";
    icon.src = `/static/imgs/player_icons/${r.icon || 1}.png`;
    li.appendChild(icon);

    const name = document.createElement("div");
    name.className = "lb-name";
    name.innerHTML = `${esc(r.name)}<span class="lb-tag">${r.is_bot ? "🤖" : "👤"}</span>`;
    if (!r.alive && r.killed_by) {
      const fate = document.createElement("span");
      fate.className = "lb-fate";
      fate.textContent = r.killed_by === r.name
        ? "morreu pela própria bomba"
        : `morto por ${r.killed_by}`;
      name.appendChild(fate);
    }
    li.appendChild(name);

    const kills = document.createElement("div");
    kills.className = "lb-kills";
    kills.textContent = r.kills || 0;
    li.appendChild(kills);

    const status = document.createElement("div");
    status.className = "lb-status " + (r.alive ? "alive" : "dead");
    status.textContent = r.alive ? "vivo" : "morto";
    li.appendChild(status);

    root.appendChild(li);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function rebuildMap() {
  if (!grid) return;
  if (tilesReady < 4) return;
  mapCanvas.width = MAP_W * MAP_TILE;
  mapCanvas.height = MAP_H * MAP_TILE;
  const floorImg = TILE_IMGS.floor;
  const brickImg = TILE_IMGS.wall1;
  const metalImg = TILE_IMGS.unbreakable;
  const crateImg = TILE_IMGS.wall2;
  for (let y = 0; y < MAP_H; y++) {
    for (let x = 0; x < MAP_W; x++) {
      const sx = x * MAP_TILE, sy = y * MAP_TILE;
      mapCtx.drawImage(floorImg, sx, sy, MAP_TILE, MAP_TILE);
      const t = grid[y][x];
      if (t === 1) {
        const onEdge = (x === 0 || y === 0 || x === MAP_W - 1 || y === MAP_H - 1);
        mapCtx.drawImage(onEdge ? brickImg : metalImg, sx, sy, MAP_TILE, MAP_TILE);
      } else if (t === 2) {
        mapCtx.drawImage(crateImg, sx, sy, MAP_TILE, MAP_TILE);
      }
    }
  }
  mapDirty = false;
}

// --- Render ---
function interpAlpha() {
  if (!prevSnap || !snap) return 1;
  const dt = snapTime - prevSnapTime;
  if (dt <= 0) return 1;
  const elapsed = performance.now() - snapTime;
  return Math.min(1, elapsed / dt);
}
function interpPlayer(curr) {
  // Use predicted position for our own player; interpolate others normally.
  if (curr.id === mySid && predictionActive && !spectating) {
    return { x: predicted.x, y: predicted.y };
  }
  if (!prevSnap) return { x: curr.x, y: curr.y };
  const a = interpAlpha();
  const prev = prevSnap.players.find(p => p.id === curr.id);
  if (!prev) return { x: curr.x, y: curr.y };
  const dx = curr.x - prev.x, dy = curr.y - prev.y;
  if (dx * dx + dy * dy > 9) return { x: curr.x, y: curr.y };
  return { x: prev.x + dx * a, y: prev.y + dy * a };
}

function findMe() {
  // For our own player, use predicted position directly if available.
  if (!spectating && predictionActive && snap) {
    const raw = snap.players.find(p => p.id === mySid);
    if (raw) return { ...raw, x: predicted.x, y: predicted.y };
  }
  if (!snap) return null;
  let raw;
  if (spectating && snap.focus_id) {
    raw = snap.players.find(p => p.id === snap.focus_id);
  } else {
    raw = snap.players.find(p => p.id === mySid);
  }
  if (!raw) return null;
  const i = interpPlayer(raw);
  return { ...raw, x: i.x, y: i.y };
}

function draw(now) {
  // Advance local prediction every frame.
  if (predictionActive && !spectating && phase === "playing") {
    if (lastTickTime === null) lastTickTime = now;
    const dt = Math.min((now - lastTickTime) / 1000, 0.05); // cap at 50ms
    lastTickTime = now;
    const dir = currentDir();
    if (dir && predicted.alive) {
      moveLocal(predicted, dir, dt);
    }
    // Report our position to the server at ~20Hz. Server uses this as
    // authoritative for our player — no rubber-banding, no reconciliation.
    if (predicted.alive && now - lastPosSent >= POS_SEND_INTERVAL_MS) {
      socket.emit("pos", { x: predicted.x, y: predicted.y });
      lastPosSent = now;
    }
  } else {
    lastTickTime = now;
  }

  ctx.clearRect(0, 0, viewW, viewH);
  if (!grid || !snap) {
    requestAnimationFrame(draw);
    return;
  }

  const dbg = !!snap.debug;
  const VTX = dbg ? MAP_W : VIEW_TILES_X;
  const VTY = dbg ? MAP_H : VIEW_TILES_Y;
  const tilePx = dbg
    ? Math.max(8, Math.floor(Math.min(viewW / VTX, viewH / VTY)))
    : TILE_PX;

  const me = findMe();
  if (me && !dbg) {
    cam.x = me.x - VTX / 2;
    cam.y = me.y - VTY / 2;
  } else if (dbg) {
    cam.x = 0;
    cam.y = 0;
  }
  cam.x = Math.max(0, Math.min(cam.x, MAP_W - VTX));
  cam.y = Math.max(0, Math.min(cam.y, MAP_H - VTY));

  const viewPxW = VTX * tilePx;
  const viewPxH = VTY * tilePx;
  const offX = Math.floor((viewW - viewPxW) / 2);
  const offY = Math.floor((viewH - viewPxH) / 2);

  const wx2sx = (wx) => offX + (wx - cam.x) * tilePx;
  const wy2sy = (wy) => offY + (wy - cam.y) * tilePx;

  if (mapDirty && grid) rebuildMap();
  if (mapCanvas.width > 0) {
    const srcX = cam.x * MAP_TILE;
    const srcY = cam.y * MAP_TILE;
    const srcW = VTX * MAP_TILE;
    const srcH = VTY * MAP_TILE;
    ctx.drawImage(mapCanvas, srcX, srcY, srcW, srcH,
                  offX | 0, offY | 0, viewPxW | 0, viewPxH | 0);
  }

  const x0 = Math.max(0, Math.floor(cam.x));
  const y0 = Math.max(0, Math.floor(cam.y));
  const x1 = Math.min(MAP_W, Math.ceil(cam.x + VTX) + 1);
  const y1 = Math.min(MAP_H, Math.ceil(cam.y + VTY) + 1);

  if (dbg && snap.blast_cells) {
    ctx.fillStyle = "rgba(231, 76, 60, 0.32)";
    for (const [bx, by] of snap.blast_cells) {
      const sx = wx2sx(bx);
      const sy = wy2sy(by);
      ctx.fillRect(sx, sy, tilePx, tilePx);
    }
    ctx.strokeStyle = "rgba(231, 76, 60, 0.7)";
    ctx.lineWidth = 1;
    for (const [bx, by] of snap.blast_cells) {
      const sx = wx2sx(bx);
      const sy = wy2sy(by);
      ctx.strokeRect(sx + 0.5, sy + 0.5, tilePx - 1, tilePx - 1);
    }
  }

  const PU_STYLE = {
    fire:   { bg: "#e74c3c", glow: "rgba(255,184,107,0.55)", icon: "🔥" },
    bombs:  { bg: "#2c3e50", glow: "rgba(155,89,182,0.55)", icon: "💣" },
    speed:  { bg: "#f1c40f", glow: "rgba(255,241,118,0.55)", icon: "⚡" },
    shield: { bg: "#3498db", glow: "rgba(133,224,255,0.55)", icon: "🛡" },
    hp:     { bg: "#c0392b", glow: "rgba(255,120,120,0.6)",  icon: "❤" },
  };
  const puPulse = 1 + 0.12 * Math.sin(now / 220);
  ctx.font = `${Math.floor(tilePx - 10)}px serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const pu of snap.powerups) {
    if (pu.x < x0 - 1 || pu.x > x1 || pu.y < y0 - 1 || pu.y > y1) continue;
    const sx = wx2sx(pu.x + 0.5);
    const sy = wy2sy(pu.y + 0.5);
    const st = PU_STYLE[pu.k] || { bg: "#fff", glow: "rgba(255,255,255,0.5)", icon: "?" };

    ctx.fillStyle = st.glow;
    ctx.beginPath();
    ctx.arc(sx, sy, (tilePx / 2 + 4) * puPulse, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = st.bg;
    ctx.beginPath();
    ctx.arc(sx, sy, (tilePx / 2 - 3) * puPulse, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.fillText(st.icon, sx, sy + 1);
  }

  // Poison tiles. Armed = full opacity + green pulse halo, inert = faded.
  // The poison.png sprite is the body; we add a pulsing aura on top.
  if (snap.poisons && POISON_IMG.complete && POISON_IMG.naturalWidth > 0) {
    const poisonPulse = 0.6 + 0.4 * Math.sin(now / 260);
    for (const pz of snap.poisons) {
      if (pz.x < x0 - 1 || pz.x > x1 || pz.y < y0 - 1 || pz.y > y1) continue;
      const sx = wx2sx(pz.x);
      const sy = wy2sy(pz.y);
      const cx = sx + tilePx / 2;
      const cy = sy + tilePx / 2;

      if (pz.armed) {
        // Hazard aura.
        ctx.fillStyle = `rgba(46, 204, 113, ${0.18 + 0.18 * poisonPulse})`;
        ctx.beginPath();
        ctx.arc(cx, cy, tilePx * 0.55, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      } else {
        // Inert: faded sprite + warning ring so players know it's coming.
        ctx.globalAlpha = 0.55;
        ctx.strokeStyle = `rgba(46, 204, 113, ${0.6 + 0.4 * poisonPulse})`;
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.arc(cx, cy, tilePx * 0.45, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.drawImage(POISON_IMG, sx + 2, sy + 2, tilePx - 4, tilePx - 4);
      ctx.globalAlpha = 1;
    }
  }

  for (const b of snap.bombs) {
    const cx = wx2sx(b.x + 0.5);
    const cy = wy2sy(b.y + 0.5);
    const pulse = 1 + 0.15 * Math.sin(now / 80);
    const r = (tilePx / 2 - 4) * pulse;
    ctx.fillStyle = "#111";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = b.t < 0.7 ? "#ff5555" : "#f1c40f";
    ctx.fillRect(cx - 2, cy - tilePx / 2 + 3, 4, 5);
    if (dbg) {
      ctx.fillStyle = "#fff";
      ctx.font = `bold ${Math.max(10, Math.floor(tilePx * 0.45))}px monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.strokeStyle = "#000";
      ctx.lineWidth = 3;
      const label = b.t.toFixed(1);
      ctx.strokeText(label, cx, cy);
      ctx.fillText(label, cx, cy);
    }
  }

  for (const e of snap.explosions) {
    for (const [x, y] of e.cells) {
      const sx = wx2sx(x);
      const sy = wy2sy(y);
      ctx.fillStyle = "rgba(255,140,0,0.85)";
      ctx.fillRect(sx + 1, sy + 1, tilePx - 2, tilePx - 2);
      ctx.fillStyle = "rgba(255,230,100,0.7)";
      ctx.fillRect(sx + 5, sy + 5, tilePx - 10, tilePx - 10);
    }
  }

  if (dbg) {
    for (const p of snap.players) {
      if (!p.alive || !p.dbg) continue;
      const [tx, ty] = p.dbg.tile;
      if (p.dbg.flee && p.dbg.flee.length) {
        ctx.strokeStyle = "rgba(46, 204, 113, 0.95)";
        ctx.lineWidth = Math.max(2, tilePx * 0.12);
        ctx.beginPath();
        ctx.moveTo(wx2sx(tx + 0.5), wy2sy(ty + 0.5));
        for (const [x, y] of p.dbg.flee) {
          ctx.lineTo(wx2sx(x + 0.5), wy2sy(y + 0.5));
        }
        ctx.stroke();
        const [fx, fy] = p.dbg.flee[p.dbg.flee.length - 1];
        ctx.fillStyle = "rgba(46, 204, 113, 0.95)";
        ctx.beginPath();
        ctx.arc(wx2sx(fx + 0.5), wy2sy(fy + 0.5), tilePx * 0.18, 0, Math.PI * 2);
        ctx.fill();
      }
      if (p.dbg.seek && p.dbg.seek.length) {
        ctx.strokeStyle = "rgba(52, 152, 219, 0.7)";
        ctx.lineWidth = Math.max(1, tilePx * 0.08);
        ctx.setLineDash([tilePx * 0.25, tilePx * 0.15]);
        ctx.beginPath();
        ctx.moveTo(wx2sx(tx + 0.5), wy2sy(ty + 0.5));
        for (const [x, y] of p.dbg.seek) {
          ctx.lineTo(wx2sx(x + 0.5), wy2sy(y + 0.5));
        }
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (p.dbg.target) {
        const [gx, gy] = p.dbg.target;
        ctx.strokeStyle = "rgba(241, 196, 15, 0.9)";
        ctx.lineWidth = 2;
        ctx.strokeRect(wx2sx(gx) + 2, wy2sy(gy) + 2, tilePx - 4, tilePx - 4);
      }
    }
  }

  let alive = 0;
  for (const p of snap.players) {
    if (!p.alive) continue;
    alive++;
    const interp = interpPlayer(p);
    const sx = wx2sx(interp.x);
    const sy = wy2sy(interp.y);
    ctx.fillStyle = "rgba(0,0,0,.4)";
    ctx.beginPath();
    ctx.ellipse(sx, sy + tilePx / 3, tilePx / 3, tilePx / 6, 0, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = p.color;
    ctx.beginPath();
    ctx.arc(sx, sy, tilePx / 2 - 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 2;
    ctx.stroke();

    const img = ICON_IMGS[p.icon];
    if (img && img.complete && img.naturalWidth > 0) {
      const sz = tilePx - 6;
      ctx.save();
      ctx.beginPath();
      ctx.arc(sx, sy, sz / 2, 0, Math.PI * 2);
      ctx.clip();
      ctx.drawImage(img, sx - sz / 2, sy - sz / 2, sz, sz);
      ctx.restore();
    }

    if (p.shield > 0) {
      ctx.strokeStyle = `rgba(100,200,255,${0.5 + 0.5 * Math.sin(now / 100)})`;
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(sx, sy, tilePx / 2 + 1, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.fillStyle = "#fff";
    ctx.font = `bold ${Math.max(11, Math.floor(tilePx * 0.32))}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 3;
    ctx.strokeText(p.name, sx, sy - tilePx / 2);
    ctx.fillText(p.name, sx, sy - tilePx / 2);
    if (dbg && p.dbg) {
      const INTENT_COLOR = {
        flee: "#2ecc71", seek: "#3498db", bomb: "#f1c40f",
        stuck: "#e74c3c", idle: "#95a5a6",
      };
      ctx.fillStyle = INTENT_COLOR[p.dbg.intent] || "#fff";
      ctx.font = `bold ${Math.max(9, Math.floor(tilePx * 0.22))}px monospace`;
      ctx.textBaseline = "top";
      ctx.strokeStyle = "#000";
      ctx.lineWidth = 3;
      const line1 = p.dbg.intent.toUpperCase();
      const line2 = (p.dbg.reason || "").slice(0, 28);
      ctx.strokeText(line1, sx, sy + tilePx / 2 + 1);
      ctx.fillText(line1, sx, sy + tilePx / 2 + 1);
      if (line2) {
        ctx.font = `${Math.max(8, Math.floor(tilePx * 0.18))}px monospace`;
        ctx.strokeText(line2, sx, sy + tilePx / 2 + tilePx * 0.28);
        ctx.fillText(line2, sx, sy + tilePx / 2 + tilePx * 0.28);
      }
    }
    if (p.id === mySid) {
      ctx.fillStyle = "#f1c40f";
      ctx.beginPath();
      ctx.moveTo(sx, sy - tilePx / 2 - 14);
      ctx.lineTo(sx - 5, sy - tilePx / 2 - 6);
      ctx.lineTo(sx + 5, sy - tilePx / 2 - 6);
      ctx.closePath();
      ctx.fill();
    }
  }

  if (me) {
    document.getElementById("hud-fire").textContent = me.fire;
    document.getElementById("hud-bombs").textContent = me.bombs;
    document.getElementById("hud-speed").textContent = me.speed.toFixed(1);
    const hpEl = document.getElementById("hud-hp");
    if (hpEl) hpEl.textContent = (me.hp != null) ? me.hp : 1;
    const sh = document.getElementById("hud-shield");
    if (me.shield > 0) {
      sh.classList.remove("shield-off");
      document.getElementById("hud-shield-t").textContent = me.shield.toFixed(1);
    } else {
      sh.classList.add("shield-off");
      document.getElementById("hud-shield-t").textContent = "0";
    }
  }

  if (!dbg) {
    if (!vignette) {
      const g = ctx.createRadialGradient(
        offX + viewPxW / 2, offY + viewPxH / 2, Math.min(viewPxW, viewPxH) * 0.30,
        offX + viewPxW / 2, offY + viewPxH / 2, Math.max(viewPxW, viewPxH) * 0.65,
      );
      g.addColorStop(0, "rgba(0,0,0,0)");
      g.addColorStop(1, "rgba(0,0,0,0.55)");
      vignette = g;
    }
    ctx.fillStyle = vignette;
    ctx.fillRect(offX, offY, viewPxW, viewPxH);
  }

  ctx.fillStyle = "#000";
  if (offX > 0) {
    ctx.fillRect(0, 0, offX, viewH);
    ctx.fillRect(offX + viewPxW, 0, viewW - (offX + viewPxW), viewH);
  }
  if (offY > 0) {
    ctx.fillRect(0, 0, viewW, offY);
    ctx.fillRect(0, offY + viewPxH, viewW, viewH - (offY + viewPxH));
  }

  const totalAlive = (snap.alive_total != null) ? snap.alive_total : alive;
  document.getElementById("hud-alive").textContent = totalAlive;

  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
