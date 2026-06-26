"""Bomberman Online - Flask + SocketIO server."""
import os
import secrets
import random
import time
import threading
import psycopg
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Original Bomberman board is 15x13. We use ~3x area, then shrunk down so the
# game is tight enough that the 15x13 viewport feels like most of the arena.
MAP_W = 43
MAP_H = 37
MAX_PLAYERS = 16
LOBBY_WAIT_SECONDS = 120  # 2 minutes
GAME_MAX_SECONDS = 600    # 10 minutes — force-end any game stuck this long
TICK_RATE = 20  # server ticks per second
BOMB_TIMER = 2.5
EXPLOSION_DURATION = 0.5
SHIELD_DURATION = 6.0
HIT_SHIELD_DURATION = 3.0  # post-hit i-frames after losing 1 hp to a bomb/poison
RESPAWN_DELAY = 0  # no respawn; last man standing

# Poison: discourages camping by spawning hazard tiles part-way through the
# match. POISON_FIRST_SPAWN_S into the game we drop POISON_INITIAL_SOURCES
# poison tiles on empty floor. Each poison tile is INERT for POISON_ARM_S
# (visible but harmless — a warning), then DAMAGING. Every poison tile, once
# armed, tries to propagate to one passable neighbour after POISON_PROPAGATE_S
# (own clock, resets after propagating so it doesn't spread infinitely from
# one source).
POISON_FIRST_SPAWN_S = 15.0
POISON_INITIAL_SOURCES = 6
POISON_ARM_S = 5.0           # inert period after spawn
POISON_PROPAGATE_S = 10.0    # delay before a poison tries to spread

POWERUP_CHANCES = {
    "fire": 0.30,
    "speed": 0.13,
    "bombs": 0.13,
    "shield": 0.13,
    "hp": 0.13,
    # remaining ~18% -> nothing
}

# Tile codes
T_EMPTY = 0
T_WALL = 1   # indestructible
T_BOX = 2    # destructible

PLAYER_COLORS = [
    "#e74c3c", "#3498db", "#2ecc71", "#f1c40f",
    "#9b59b6", "#e67e22", "#1abc9c", "#ecf0f1",
    "#34495e", "#fd79a8", "#00cec9", "#fdcb6e",
    "#6c5ce7", "#a29bfe", "#55efc4", "#d63031",
]

# Icon pool — 20 PNGs under /static/imgs/player_icons/{1..20}.png.
# Each connecting player / bot gets a unique one until the pool runs out.
ICON_POOL = list(range(1, 21))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                wins INTEGER DEFAULT 0,
                games INTEGER DEFAULT 0,
                icon INTEGER DEFAULT 1
            )
        """)


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------
class GameState:
    def __init__(self):
        self.phase = "lobby"  # lobby | starting | playing | ended
        self.players = {}  # sid -> player dict
        self.bots = {}     # bot_id -> bot dict
        self.bombs = []
        self.explosions = []
        self.powerups = {}  # (x,y) -> type
        # Poison field. (x,y) -> {"spawned_at": float, "armed_at": float,
        #                          "propagate_at": float, "propagated": bool}
        # `armed_at` is when the tile starts hurting; `propagate_at` is when
        # it spreads to one passable neighbour (once, then `propagated=True`).
        self.poisons = {}
        # Have we done the initial poison spawn for this round?
        self.poison_seeded = False
        self.grid = []
        self.lobby_start_ts = None
        self.game_start_ts = None
        self.winner = None
        self.host_sid = None        # first human in the lobby — can force-start
        self.used_icons = set()
        self.spectators = set()     # sids of clients watching without playing
        self.debug_viewers = set()  # sids that opted into the full-map AI debug overlay
        self.grid_dirty = True      # set when boxes change; flushed in snapshot
        self.lock = threading.Lock()

    def reserve_icon(self, preferred=None):
        """Try to give the player their preferred icon; if taken, fall back
        to any available one. Bots and overflow players go through the
        no-preference path."""
        if preferred in ICON_POOL and preferred not in self.used_icons:
            self.used_icons.add(preferred)
            return preferred
        avail = [i for i in ICON_POOL if i not in self.used_icons]
        if not avail:
            return random.choice(ICON_POOL)
        icon = random.choice(avail)
        self.used_icons.add(icon)
        return icon

    def release_icon(self, icon):
        self.used_icons.discard(icon)

    def reset_for_new_game(self):
        """Wipe the round but keep humans connected so they roll into the
        next lobby without having to reconnect. Bots are dropped."""
        kept = {sid: p for sid, p in self.players.items() if not p["is_bot"]}
        for p in kept.values():
            # reset per-round stats
            p["alive"] = True
            p["fire"] = 2
            p["max_bombs"] = 1
            p["speed"] = 3.0
            p["active_bombs"] = 0
            p["shield_until"] = 0.0
            p["move_dir"] = None
            p["hp"] = 1
        # only humans' icons stay reserved
        self.used_icons = {p["icon"] for p in kept.values()}
        self.spectators = set()
        self.phase = "lobby"
        self.players = kept
        self.bots = {}
        self.bombs = []
        self.explosions = []
        self.powerups = {}
        self.grid = []
        self.lobby_start_ts = None
        self.game_start_ts = None
        self.winner = None
        self.poisons = {}
        self.poison_seeded = False
        # rotate host if previous one left
        if self.host_sid not in kept:
            self.host_sid = next(iter(kept), None)
        # if anyone is still here, immediately start the next countdown
        if kept:
            self.lobby_start_ts = time.time()
            self.phase = "starting"


STATE = GameState()


def make_map():
    """Build a 60x52 map: outer walls + grid pillars + random boxes,
    with 3x3 safe corners for spawn points."""
    grid = [[T_EMPTY for _ in range(MAP_W)] for _ in range(MAP_H)]
    for x in range(MAP_W):
        grid[0][x] = T_WALL
        grid[MAP_H - 1][x] = T_WALL
    for y in range(MAP_H):
        grid[y][0] = T_WALL
        grid[y][MAP_W - 1] = T_WALL
    # pillar pattern
    for y in range(2, MAP_H - 1, 2):
        for x in range(2, MAP_W - 1, 2):
            grid[y][x] = T_WALL

    safe = set()
    # spawn corners
    spawns = generate_spawns()
    for sx, sy in spawns:
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                safe.add((sx + dx, sy + dy))

    for y in range(1, MAP_H - 1):
        for x in range(1, MAP_W - 1):
            if grid[y][x] == T_EMPTY and (x, y) not in safe:
                if random.random() < 0.65:
                    grid[y][x] = T_BOX
    return grid


def generate_spawns():
    """16 spawn points spread on a 4x4 grid across the map.

    We pick cells that are odd-coordinate (so they fall on the empty rows /
    columns of the pillar pattern), then shift toward the map center so the
    outer spawns aren't glued to the walls."""
    cols = 4
    rows = 4
    # interior bounds: leave a 3-tile margin from each edge
    x_min, x_max = 3, MAP_W - 4
    y_min, y_max = 3, MAP_H - 4
    spawns = []
    for r in range(rows):
        for c in range(cols):
            fx = c / (cols - 1)
            fy = r / (rows - 1)
            x = int(round(x_min + fx * (x_max - x_min)))
            y = int(round(y_min + fy * (y_max - y_min)))
            # snap to an odd coordinate so it lands on an empty pillar row
            if x % 2 == 0:
                x += 1 if x + 1 <= x_max else -1
            if y % 2 == 0:
                y += 1 if y + 1 <= y_max else -1
            spawns.append((x, y))
    return spawns


def new_player(name, is_bot=False):
    return {
        "name": name,
        "x": 1.0,
        "y": 1.0,
        "alive": True,
        "fire": 2,
        "max_bombs": 1,
        "speed": 3.0,        # tiles per second
        "active_bombs": 0,
        "shield_until": 0.0,
        "color": "#fff",
        "icon": 1,
        "is_bot": is_bot,
        "move_dir": None,
        # Health system. Default 1 hp = lethal on first hit (matches original
        # Bomberman). Picking up an "hp" powerup grants +1 hp. When hit by a
        # bomb or poison without a shield, lose 1 hp and gain a short
        # invulnerability window (HIT_SHIELD_DURATION).
        "hp": 1,
        # End-of-round stats for the leaderboard.
        "kills": 0,
        "died_at": None,
        "killed_by": None,
        # All bots run at max quality — fast reactions, decent cooldown so
        # the bot has time to leave the blast radius of its previous bomb
        # before it considers placing another one. The tiny jitter on
        # next_decision (5ms) keeps 15 bots from updating in perfect
        # lockstep, which would spike the tick budget.
        "bot_state": {
            "next_decision": random.uniform(0, 0.005),
            "last_bomb": 0,
            "bomb_cooldown": 2.8,                    # > BOMB_TIMER, so our
                                                     # last bomb has cleared
            "aggression": 1.0,                       # always bomb when safe
            "react_ms": 0.05,                        # 20 decisions per second
            # When the bot is fleeing or has just placed a bomb, we lock it
            # into "stay defensive" mode until both (a) the tile is out of
            # every blast and (b) `safe_until` has passed — that grace period
            # lets the lingering explosion clear before we wander back in.
            # Without this the bot recomputes every 50ms, leaves flee mode
            # the instant it steps one tile clear, and walks straight back
            # into the active explosion chasing a target.
            "safe_until": 0.0,
            "flee_goal": None,       # (x,y) of the safe tile we're heading for
            # Debug breadcrumbs — purely for the spectator overlay & death
            # logs. Each tick of bot_think overwrites these.
            "intent": "idle",        # "flee" | "seek" | "bomb" | "stuck" | "idle"
            "intent_reason": "",     # short human-readable explanation
            "flee_path": [],         # [(x, y), ...] tiles BFS chose, in order
            "seek_path": [],         # [(x, y), ...] A* path to current target
            "target": None,          # (x, y) of the thing we're going for
            "last_bomb_xy": None,    # tile we last placed a bomb on
        },
        "last_move_ts": 0,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("menu"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if not u or not p:
            error = "Preenche tudo."
        else:
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM users WHERE username=%s", (u,)
                ).fetchone()
            if row and check_password_hash(row["password_hash"], p):
                session["user"] = u
                return redirect(url_for("menu"))
            error = "Credenciais inválidas."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if len(u) < 3 or len(p) < 4:
            error = "Username >= 3, password >= 4."
        else:
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO users(username, password_hash) VALUES(%s,%s)",
                        (u, generate_password_hash(p)),
                    )
                session["user"] = u
                return redirect(url_for("menu"))
            except UniqueViolation:
                error = "Username já existe."
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/menu")
def menu():
    if "user" not in session:
        return redirect(url_for("login"))
    with db() as conn:
        row = conn.execute(
            "SELECT wins, games, icon FROM users WHERE username=%s",
            (session["user"],)
        ).fetchone()
    return render_template("menu.html", user=session["user"],
                           wins=row["wins"] if row else 0,
                           games=row["games"] if row else 0,
                           icon=row["icon"] if row else 1,
                           icon_count=len(ICON_POOL))


@app.route("/set_icon", methods=["POST"])
def set_icon():
    if "user" not in session:
        return jsonify({"ok": False}), 401
    try:
        icon = int(request.json.get("icon", 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False}), 400
    if icon not in ICON_POOL:
        return jsonify({"ok": False}), 400
    with db() as conn:
        conn.execute("UPDATE users SET icon=%s WHERE username=%s",
                     (icon, session["user"]))
    return jsonify({"ok": True, "icon": icon})


@app.route("/play")
def play():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("game.html", user=session["user"])


@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    """Emergency reset — forces the global STATE back to a clean lobby."""
    _force_reset()
    return "OK", 200


@app.route("/admin/status")
def admin_status():
    """Returns current STATE info for debugging."""
    return jsonify({
        "phase": STATE.phase,
        "players": len(STATE.players),
        "spectators": len(STATE.spectators),
        "game_start_ts": STATE.game_start_ts,
        "lobby_start_ts": STATE.lobby_start_ts,
        "running_secs": round(time.time() - STATE.game_start_ts, 1) if STATE.game_start_ts else None,
    })


def _force_reset():
    with STATE.lock:
        STATE.phase = "lobby"
        STATE.players = {}
        STATE.bots = {}
        STATE.bombs = []
        STATE.explosions = []
        STATE.powerups = {}
        STATE.grid = []
        STATE.lobby_start_ts = None
        STATE.game_start_ts = None
        STATE.winner = None
        STATE.host_sid = None
        STATE.used_icons = set()
        STATE.spectators = set()
        STATE.debug_viewers = set()
        STATE.grid_dirty = True
    socketio.emit("reset", {})


# ---------------------------------------------------------------------------
# Socket events
# ---------------------------------------------------------------------------
@socketio.on("connect")
def on_connect():
    if "user" not in session:
        print(f"[connect] REJECTED — no session. cookies={request.cookies.keys()}")
        return False
    print(f"[connect] user={session['user']} sid={request.sid} phase={STATE.phase}")
    sid = request.sid
    # Look up the player's chosen icon from the DB.
    pref_icon = 1
    try:
        with db() as conn:
            row = conn.execute("SELECT icon FROM users WHERE username=%s",
                               (session["user"],)).fetchone()
            if row and row["icon"]:
                pref_icon = row["icon"]
    except Exception:
        pass

    # Auto-reset if game is stuck (no humans alive or running > GAME_MAX_SECONDS).
    with STATE.lock:
        if STATE.phase == "playing":
            no_humans = not any(not p["is_bot"] and p["alive"] for p in STATE.players.values())
            stuck = STATE.game_start_ts and (time.time() - STATE.game_start_ts > GAME_MAX_SECONDS)
            if not no_humans and not stuck:
                STATE.spectators.add(sid)
                emit("spectator", {})
                emit("start", {"grid": STATE.grid, "map_w": MAP_W, "map_h": MAP_H,
                               "spectating": True})
                return
    # Outside the lock: do the reset if needed (re-acquires lock internally).
    with STATE.lock:
        needs_reset = STATE.phase == "playing"
    if needs_reset:
        _force_reset()

    with STATE.lock:
        if len(STATE.players) >= MAX_PLAYERS:
            emit("full", {})
            return
        color = PLAYER_COLORS[len(STATE.players) % len(PLAYER_COLORS)]
        p = new_player(session["user"])
        p["color"] = color
        p["icon"] = STATE.reserve_icon(pref_icon)
        STATE.players[sid] = p
        if STATE.host_sid is None:
            STATE.host_sid = sid
        if STATE.lobby_start_ts is None and STATE.phase == "lobby":
            STATE.lobby_start_ts = time.time()
            STATE.phase = "starting"
            threading.Thread(target=lobby_countdown, daemon=True).start()
        emit("hello", {"sid": sid, "you": session["user"],
                       "is_host": sid == STATE.host_sid})
    broadcast_lobby()


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with STATE.lock:
        STATE.spectators.discard(sid)
        STATE.debug_viewers.discard(sid)
        if sid in STATE.players:
            STATE.release_icon(STATE.players[sid].get("icon"))
            # Don't outright remove a connected human mid-round — mark them
            # dead so the round still counts. Only kick from lobby/ended.
            if STATE.phase == "playing":
                STATE.players[sid]["alive"] = False
            else:
                del STATE.players[sid]
        if STATE.host_sid == sid:
            STATE.host_sid = next(
                (s for s, p in STATE.players.items() if not p["is_bot"]), None
            )
    broadcast_lobby()


@socketio.on("start_now")
def on_start_now():
    """Host can skip the lobby wait and launch the round immediately."""
    sid = request.sid
    with STATE.lock:
        if sid != STATE.host_sid:
            return
        if STATE.phase != "starting":
            return
        start_game()


@socketio.on("terminate")
def on_terminate():
    """Spectators can end the round when only bots remain alive (or all
    humans died). Useful to skip a long bot-vs-bot endgame."""
    with STATE.lock:
        if STATE.phase != "playing":
            return
        humans_alive = any(
            p["alive"] and not p["is_bot"] for p in STATE.players.values()
        )
        if humans_alive:
            return  # someone is still in the fight; don't let them bail
        # pick whichever alive bot has the most kills as the "winner"
        alive_bots = [p for p in STATE.players.values()
                      if p["alive"] and p["is_bot"]]
        winner = max(alive_bots, key=lambda p: p.get("kills", 0), default=None)
        end_game(winner)


@socketio.on("input")
def on_input(data):
    sid = request.sid
    with STATE.lock:
        if sid not in STATE.players or STATE.phase != "playing":
            return
        p = STATE.players[sid]
        if not p["alive"]:
            return
        d = data.get("dir")
        if d in ("up", "down", "left", "right", None):
            p["move_dir"] = d


@socketio.on("pos")
def on_pos(data):
    """Client-authoritative position update for human players.

    The client simulates its own movement (collision included) and reports
    its position ~20 times per second. We trust it — we only clamp to the
    map bounds so a buggy/malicious client can't drift to NaN coordinates.
    Bombs, explosions, damage and powerups remain server-authoritative, so
    the worst a cheater can do is teleport themselves into a wall, which
    just gets them killed faster.
    """
    sid = request.sid
    with STATE.lock:
        if sid not in STATE.players or STATE.phase != "playing":
            return
        p = STATE.players[sid]
        if not p["alive"] or p["is_bot"]:
            return
        try:
            x = float(data.get("x"))
            y = float(data.get("y"))
        except (TypeError, ValueError):
            return
        # Clamp to playable area (walls are at the edges).
        x = max(0.5, min(MAP_W - 0.5, x))
        y = max(0.5, min(MAP_H - 0.5, y))
        p["x"] = x
        p["y"] = y


@socketio.on("bomb")
def on_bomb():
    sid = request.sid
    with STATE.lock:
        if sid not in STATE.players or STATE.phase != "playing":
            return
        p = STATE.players[sid]
        place_bomb(sid, p)


@socketio.on("debug_view")
def on_debug_view(payload):
    """Client opted into (or out of) the full-map AI debug overlay. When on,
    that viewer's next snapshot will include the whole map plus per-bot
    debug info (blast cells, flee paths, intent labels)."""
    sid = request.sid
    on = bool(payload and payload.get("on"))
    with STATE.lock:
        if on:
            STATE.debug_viewers.add(sid)
        else:
            STATE.debug_viewers.discard(sid)


# ---------------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------------
def broadcast_lobby():
    payload = {
        "phase": STATE.phase,
        "players": [
            {"name": p["name"], "color": p["color"],
             "is_bot": p["is_bot"], "icon": p["icon"]}
            for p in STATE.players.values()
        ],
        "count": len(STATE.players),
        "max": MAX_PLAYERS,
        "host_sid": STATE.host_sid,
        "remaining": int(LOBBY_WAIT_SECONDS - (time.time() - STATE.lobby_start_ts))
                     if STATE.lobby_start_ts else LOBBY_WAIT_SECONDS,
    }
    socketio.emit("lobby", payload)


def lobby_countdown():
    while True:
        time.sleep(1)
        with STATE.lock:
            if STATE.phase != "starting":
                return
            elapsed = time.time() - STATE.lobby_start_ts
            if elapsed >= LOBBY_WAIT_SECONDS or len(STATE.players) >= MAX_PLAYERS:
                start_game()
                return
        broadcast_lobby()


def start_game():
    # fill bots
    needed = MAX_PLAYERS - len(STATE.players)
    bot_names = ["Bomby", "Blaster", "Fuse", "Sparky", "Boom", "Nitro",
                 "Pyro", "Crash", "Volt", "Ash", "Char", "Ember",
                 "Flint", "Smoky", "Tnt", "Zap"]
    random.shuffle(bot_names)
    for i in range(needed):
        bid = f"bot_{i}"
        b = new_player(bot_names[i % len(bot_names)] + f"#{i+1}", is_bot=True)
        b["color"] = PLAYER_COLORS[(len(STATE.players) + i) % len(PLAYER_COLORS)]
        b["icon"] = STATE.reserve_icon()
        STATE.players[bid] = b

    STATE.grid = make_map()
    STATE.grid_dirty = True
    STATE.poisons = {}
    STATE.poison_seeded = False
    spawns = generate_spawns()
    random.shuffle(spawns)
    for i, (sid, p) in enumerate(STATE.players.items()):
        sx, sy = spawns[i % len(spawns)]
        p["x"], p["y"] = sx + 0.5, sy + 0.5

    STATE.phase = "playing"
    STATE.game_start_ts = time.time()
    socketio.emit("start", {
        "grid": STATE.grid,
        "map_w": MAP_W,
        "map_h": MAP_H,
    })
    threading.Thread(target=game_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Game loop
# ---------------------------------------------------------------------------
def game_loop():
    last = time.time()
    snapshot_acc = 0
    while True:
        now = time.time()
        dt = now - last
        last = now
        with STATE.lock:
            if STATE.phase != "playing":
                return
            # Force-end games that have been running too long (bots stuck, etc.)
            if STATE.game_start_ts and now - STATE.game_start_ts > GAME_MAX_SECONDS:
                alive = [p for p in STATE.players.values() if p["alive"]]
                end_game(alive[0] if alive else None)
                return
            tick(dt, now)
            snapshot_acc += dt
            if snapshot_acc >= 1 / 20:  # 20 snapshots/sec (interpolated client-side)
                send_snapshot()
                snapshot_acc = 0
            check_win()
        time.sleep(1 / TICK_RATE)


def tile(x, y):
    if 0 <= x < MAP_W and 0 <= y < MAP_H:
        return STATE.grid[y][x]
    return T_WALL


def has_bomb_at(x, y):
    for b in STATE.bombs:
        if b["x"] == x and b["y"] == y:
            return True
    return False


def passable(x, y, from_x=None, from_y=None):
    """Tile is walkable. A bomb blocks the tile UNLESS the player is
    currently standing on it (so they can step off a freshly-placed bomb)."""
    if tile(x, y) != T_EMPTY:
        return False
    if has_bomb_at(x, y):
        return (from_x, from_y) == (x, y)
    return True


def tick(dt, now):
    # move players
    for sid, p in STATE.players.items():
        if not p["alive"]:
            continue
        if p["is_bot"]:
            # Never let one buggy bot kill the whole game loop. A bot that
            # explodes its brain just stands still for this tick.
            try:
                bot_think(sid, p, now)
            except Exception as ex:
                print(f"[bot {p['name']}] {ex.__class__.__name__}: {ex}")
                p["move_dir"] = None
            if p["move_dir"]:
                move_player(p, dt)
        # Humans don't get simulated here — they send their own position via
        # the "pos" socket event (client-authoritative movement). The server
        # still owns bombs, explosions, damage and powerups.

    # bombs
    for b in list(STATE.bombs):
        if now >= b["explode_at"]:
            detonate(b, now)

    # explosions cleanup
    STATE.explosions = [e for e in STATE.explosions if now < e["until"]]

    # poison: seed initial sources once we hit POISON_FIRST_SPAWN_S into the
    # round, then propagate each armed source every POISON_PROPAGATE_S.
    update_poison(now)

    # damage check
    for sid, p in STATE.players.items():
        if not p["alive"]:
            continue
        px, py = int(p["x"]), int(p["y"])
        for e in STATE.explosions:
            if (px, py) in e["cells"] and now < e["until"]:
                killer_sid = e.get("owner")
                hit = take_damage(p, sid, now, source="bomb", killer_sid=killer_sid)
                if hit and not p["alive"] and p["is_bot"]:
                    bs = p.get("bot_state", {})
                    suicide = (killer_sid == sid)
                    tag = "SUICIDE" if suicide else "killed"
                    killer = STATE.players.get(killer_sid)
                    killer_name = (killer["name"] if killer else "?") if not suicide else "self"
                    print(f"[DEATH/{tag}] {p['name']:<14} @({px},{py}) by {killer_name:<14} "
                          f"| intent={bs.get('intent','?'):<5} "
                          f"reason='{bs.get('intent_reason','')}' "
                          f"| flee={bs.get('flee_path') or []} "
                          f"seek={bs.get('seek_path') or []} "
                          f"| last_bomb_xy={bs.get('last_bomb_xy')} "
                          f"active_bombs_pre={p.get('active_bombs', 0)}")
                break

        # poison contact damage (only if the tile we're standing on has an
        # ARMED poison — newly spawned poisons have a grace period).
        if p["alive"]:
            ps = STATE.poisons.get((px, py))
            if ps and now >= ps["armed_at"]:
                take_damage(p, sid, now, source="poison")

        # pickup
        if p["alive"] and (px, py) in STATE.powerups:
            apply_powerup(p, STATE.powerups[(px, py)])
            del STATE.powerups[(px, py)]


def move_player(p, dt):
    dirs = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
    if p["move_dir"] not in dirs:
        return
    dx, dy = dirs[p["move_dir"]]
    step = p["speed"] * dt

    # Move axis at a time, with corner-sliding: if the axis we want to walk
    # along is blocked but we're slightly off-center on the OTHER axis,
    # nudge toward the center so the player slides around corners naturally.
    cur_tx, cur_ty = int(p["x"]), int(p["y"])
    if dx != 0:
        nx = p["x"] + dx * step
        edge = nx + 0.45 * dx           # leading edge of the player
        tx = int(edge)
        ty = int(p["y"])
        if passable(tx, ty, cur_tx, cur_ty):
            p["x"] = nx
        else:
            # snap to center of current tile on the X axis
            p["x"] = int(p["x"]) + 0.5

    if dy != 0:
        ny = p["y"] + dy * step
        tx = int(p["x"])
        edge = ny + 0.45 * dy
        ty = int(edge)
        if passable(tx, ty, cur_tx, cur_ty):
            p["y"] = ny
        else:
            p["y"] = int(p["y"]) + 0.5


def place_bomb(sid, p, at=None):
    """Drop a bomb on the player's tile (or `at=(x,y)` if the caller already
    pinned which tile they intended). Bots MUST pass `at` because by the time
    place_bomb runs, the float position can have crossed an int boundary,
    and the bomb would land one tile off from where the safety BFS thought."""
    if p["active_bombs"] >= p["max_bombs"]:
        return
    if at is None:
        bx, by = int(p["x"]), int(p["y"])
    else:
        bx, by = at
    if has_bomb_at(bx, by):
        return
    STATE.bombs.append({
        "owner": sid,
        "x": bx, "y": by,
        "fire": p["fire"],
        "explode_at": time.time() + BOMB_TIMER,
    })
    p["active_bombs"] += 1


def detonate(b, now, chain=None):
    if chain is None:
        chain = set()
    if id(b) in chain:
        return
    chain.add(id(b))
    if b not in STATE.bombs:
        return
    STATE.bombs.remove(b)
    owner = STATE.players.get(b["owner"])
    if owner:
        owner["active_bombs"] = max(0, owner["active_bombs"] - 1)
    cells = {(b["x"], b["y"])}
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        for r in range(1, b["fire"] + 1):
            x, y = b["x"] + dx * r, b["y"] + dy * r
            t = tile(x, y)
            if t == T_WALL:
                break
            cells.add((x, y))
            # chain
            for other in list(STATE.bombs):
                if other["x"] == x and other["y"] == y:
                    other["explode_at"] = now
            if t == T_BOX:
                STATE.grid[y][x] = T_EMPTY
                STATE.grid_dirty = True
                roll_powerup(x, y)
                break
    STATE.explosions.append({
        "cells": cells,
        "until": now + EXPLOSION_DURATION,
        "owner": b["owner"],
    })


def roll_powerup(x, y):
    r = random.random()
    acc = 0
    for k, v in POWERUP_CHANCES.items():
        acc += v
        if r < acc:
            STATE.powerups[(x, y)] = k
            return


def apply_powerup(p, kind):
    if kind == "fire":
        p["fire"] = min(p["fire"] + 1, 10)
    elif kind == "bombs":
        p["max_bombs"] = min(p["max_bombs"] + 1, 8)
    elif kind == "speed":
        p["speed"] = min(p["speed"] + 0.6, 7.0)
    elif kind == "shield":
        p["shield_until"] = time.time() + SHIELD_DURATION
    elif kind == "hp":
        p["hp"] += 1


def take_damage(p, sid, now, source="bomb", killer_sid=None):
    """Apply 1 point of damage to player `p`. If they have an active shield,
    the hit is absorbed entirely. Otherwise hp drops by 1, they gain a 3s
    post-hit shield (i-frames), and only die when hp hits 0.

    `source` is "bomb" or "poison" — used for kill attribution and logging.
    `killer_sid` is the bomb owner (for bombs). For poison, killer_sid is
    None and the death is logged as "envenenado".

    Returns True if the hit landed (hp lost), False if absorbed by shield.
    """
    if now < p["shield_until"]:
        return False
    p["hp"] -= 1
    # i-frame shield so consecutive ticks of the same explosion (or stepping
    # off then onto a poison) don't drain hp instantly.
    p["shield_until"] = now + HIT_SHIELD_DURATION
    if p["hp"] > 0:
        return True
    # hp <= 0 — player dies.
    p["alive"] = False
    p["died_at"] = now
    p["active_bombs"] = 0
    killer = STATE.players.get(killer_sid) if killer_sid else None
    if source == "poison":
        p["killed_by"] = "envenenado"
    elif killer and killer_sid != sid:
        killer["kills"] = killer.get("kills", 0) + 1
        p["killed_by"] = killer["name"]
    else:
        p["killed_by"] = p["name"] if killer_sid == sid else "?"
    return True


# ---------------------------------------------------------------------------
# Poison
# ---------------------------------------------------------------------------
def _spawn_poison(x, y, now):
    """Drop a poison tile at (x,y). Inert for POISON_ARM_S, then damages.
    Will try to propagate once after POISON_PROPAGATE_S (own clock)."""
    STATE.poisons[(x, y)] = {
        "spawned_at": now,
        "armed_at": now + POISON_ARM_S,
        "propagate_at": now + POISON_PROPAGATE_S,
        "propagated": False,
    }


def update_poison(now):
    """Seed the initial poison sources POISON_FIRST_SPAWN_S into the round,
    then have each existing poison try to spread to one neighbour after its
    own POISON_PROPAGATE_S delay (only once per tile)."""
    if STATE.game_start_ts is None:
        return
    elapsed = now - STATE.game_start_ts

    # Initial seeding (once).
    if not STATE.poison_seeded and elapsed >= POISON_FIRST_SPAWN_S:
        STATE.poison_seeded = True
        # Pick empty floor tiles that aren't already a powerup or bomb.
        # Try to avoid spawning right on top of a living player by keeping a
        # 2-tile clearance.
        candidates = []
        alive_xy = [(int(p["x"]), int(p["y"])) for p in STATE.players.values()
                    if p["alive"]]
        for y in range(MAP_H):
            for x in range(MAP_W):
                if STATE.grid[y][x] != T_EMPTY:
                    continue
                if (x, y) in STATE.powerups or has_bomb_at(x, y):
                    continue
                # too close to a living player?
                too_close = any(abs(x - ax) + abs(y - ay) < 3
                                for ax, ay in alive_xy)
                if too_close:
                    continue
                candidates.append((x, y))
        random.shuffle(candidates)
        for (x, y) in candidates[:POISON_INITIAL_SOURCES]:
            _spawn_poison(x, y, now)
        if candidates:
            print(f"[poison] seeded {min(len(candidates), POISON_INITIAL_SOURCES)} sources")

    # Propagation: each poison spreads once to a passable neighbour.
    if not STATE.poisons:
        return
    new_tiles = []
    for (x, y), ps in STATE.poisons.items():
        if ps["propagated"] or now < ps["propagate_at"]:
            continue
        neighbours = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < MAP_W and 0 <= ny < MAP_H):
                continue
            if STATE.grid[ny][nx] != T_EMPTY:
                continue
            if (nx, ny) in STATE.poisons:
                continue
            neighbours.append((nx, ny))
        ps["propagated"] = True
        if neighbours:
            new_tiles.append(random.choice(neighbours))
    for (x, y) in new_tiles:
        if (x, y) not in STATE.poisons:
            _spawn_poison(x, y, now)


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------
def bomb_blast_cells(b):
    """Compute every tile a single bomb's flames will touch when it goes
    off. Pure geometry — does not look at time."""
    cells = {(b["x"], b["y"])}
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        for r in range(1, b["fire"] + 1):
            cx, cy = b["x"] + dx * r, b["y"] + dy * r
            t = tile(cx, cy)
            if t == T_WALL:
                break
            cells.add((cx, cy))
            if t == T_BOX:
                break
    return cells


def tile_in_any_blast(x, y):
    """True if (x,y) is in the future blast radius of ANY pending bomb,
    or in any active explosion, or hosts an armed (or soon-armed) poison.
    Used as the BFS safety predicate — a tile is only a valid hideout if no
    bomb can ever reach it and no poison is sitting on it.

    We also treat *inert* poison as dangerous — it'll arm within seconds
    and we don't want bots strolling onto a tile that's about to hurt them.
    """
    for e in STATE.explosions:
        if (x, y) in e["cells"]:
            return True
    for b in STATE.bombs:
        if (x, y) in bomb_blast_cells(b):
            return True
    if (x, y) in STATE.poisons:
        return True
    return False


def in_danger(x, y, now, time_horizon=2.0):
    """Return True if (x,y) is in an explosion or will be hit by a bomb
    that explodes within time_horizon seconds. Used to decide whether the
    bot must drop what it's doing and run."""
    for e in STATE.explosions:
        if (x, y) in e["cells"]:
            return True
    for b in STATE.bombs:
        if b["explode_at"] - now < time_horizon:
            if (x, y) in bomb_blast_cells(b):
                return True
    return False


def effective_explode_times():
    """Return {id(bomb): effective_explode_time} accounting for chain
    detonations. When bomb A's blast covers bomb B's tile, B is dragged
    forward to A's time (or earlier, if A is itself chained). We iterate
    to a fixed point so a chain of N bombs collapses to the earliest fuse.

    This is what the safety BFS should reason about — `bomb["explode_at"]`
    alone lies when bombs sit in each other's blasts.
    """
    times = {id(b): b["explode_at"] for b in STATE.bombs}
    # Precompute blast cells once per bomb.
    cells = {id(b): bomb_blast_cells(b) for b in STATE.bombs}
    # Index bombs by their tile so we can find "which bomb sits at (x,y)".
    by_tile = {}
    for b in STATE.bombs:
        by_tile.setdefault((b["x"], b["y"]), []).append(b)

    changed = True
    # Worst case the chain is len(bombs) long; bound iterations defensively.
    for _ in range(len(STATE.bombs) + 1):
        if not changed:
            break
        changed = False
        for b in STATE.bombs:
            t = times[id(b)]
            for (cx, cy) in cells[id(b)]:
                for other in by_tile.get((cx, cy), ()):
                    if other is b:
                        continue
                    if times[id(other)] > t:
                        times[id(other)] = t
                        changed = True
    return times


def bfs_safe(sx, sy, now, max_depth=8, speed=3.0):
    """BFS to a tile that's outside every pending bomb's blast radius.

    Each step takes 1/speed seconds. The destination must be a tile no
    pending bomb can reach. Every tile we pass through must also not be
    exploding at the moment the bot is on it — passing under a bomb that
    detonates at that instant still kills you. We use the *effective*
    explode time for each bomb (chain detonations pulled forward) so
    placing a bomb next to a near-fuse bomb doesn't fool the BFS.
    """
    from collections import deque
    eff = effective_explode_times()
    step_time = 1.0 / speed
    visited = {(sx, sy)}
    # queue: (x, y, path, arrival_time)
    q = deque([((sx, sy), [], now)])
    while q:
        (x, y), path, t_arrive = q.popleft()
        # Genuine hideout: no pending bomb's blast reaches here at all.
        if path and not tile_in_any_blast(x, y):
            return path
        if len(path) >= max_depth:
            continue
        for dx, dy, name in ((1, 0, "right"), (-1, 0, "left"),
                             (0, 1, "down"), (0, -1, "up")):
            nx, ny = x + dx, y + dy
            if (nx, ny) in visited:
                continue
            if not passable(nx, ny, from_x=x, from_y=y):
                continue
            t_next = t_arrive + step_time
            # Don't walk through a tile that's exploding when we'd be on it.
            if _exploding_at(nx, ny, t_next, eff):
                continue
            visited.add((nx, ny))
            q.append(((nx, ny), path + [name], t_next))
    return None


def _exploding_at(x, y, t, eff=None):
    """True if (x,y) will be inside an active explosion at time t.

    For pending bombs we treat [effective_explode_at,
    effective_explode_at+EXPLOSION_DURATION] as the lethal window; for
    already-spawned explosions we use their actual `until` timestamp.
    `eff` is the {id(bomb): effective_explode_at} dict from
    effective_explode_times() — pass it in when calling from a BFS loop
    so we don't recompute it for every neighbour.
    """
    for e in STATE.explosions:
        if (x, y) in e["cells"] and t < e["until"]:
            return True
    if eff is None:
        eff = effective_explode_times()
    for b in STATE.bombs:
        bt = eff.get(id(b), b["explode_at"])
        if bt <= t <= bt + EXPLOSION_DURATION:
            if (x, y) in bomb_blast_cells(b):
                return True
    return False


def _dirs_to_tiles(sx, sy, dirs):
    """Turn ['right', 'up', ...] starting at (sx, sy) into the list of
    tiles visited (excluding the start). Used by the debug overlay."""
    DELTA = {"right": (1, 0), "left": (-1, 0), "down": (0, 1), "up": (0, -1)}
    out = []
    x, y = sx, sy
    for d in dirs:
        dx, dy = DELTA[d]
        x += dx; y += dy
        out.append((x, y))
    return out


def _safe_wander_dir(bx, by):
    """Pick a random passable neighbour that isn't in any blast. Falls back
    to a blast-only-but-passable tile if everything is on fire, then to
    None. Used by the "no target" / "unreachable" wander branches — those
    used to roll random.choice over the four cardinals blindly and walk
    bots straight into their own pending blast."""
    safe, risky = [], []
    for dx, dy, name in ((1, 0, "right"), (-1, 0, "left"),
                         (0, 1, "down"), (0, -1, "up")):
        if not passable(bx + dx, by + dy, bx, by):
            continue
        if tile_in_any_blast(bx + dx, by + dy):
            risky.append(name)
        else:
            safe.append(name)
    if safe:
        return random.choice(safe)
    if risky:
        return random.choice(risky)
    return None


def bot_think(sid, p, now):
    """All bots use the same A*+flee brain; per-bot quirks come from
    bot_state (react_ms, bomb_cooldown, aggression) so they don't clone
    each other's moves."""
    bs = p["bot_state"]
    if now < bs["next_decision"]:
        return
    bs["next_decision"] = now + bs["react_ms"]
    bx, by = int(p["x"]), int(p["y"])
    # Reset debug breadcrumbs each tick; whichever branch runs fills them in.
    bs["flee_path"] = []
    bs["seek_path"] = []
    bs["target"] = None

    # SAFETY FIRST. If we're standing anywhere a pending bomb's flames will
    # reach, we run — full stop. Use pure geometry (tile_in_any_blast), not
    # the "explodes within Xs" time-window check, because the latter lies
    # about freshly-placed bombs (their fuse is still > 2s).
    # Cap the BFS depth to how far we can actually walk before BOMB_TIMER
    # runs out — a "safe tile" 15 steps away isn't safe if we'd still be
    # mid-corridor when the bomb goes off.
    max_reachable = max(2, int(p["speed"] * BOMB_TIMER) - 1)
    if tile_in_any_blast(bx, by):
        path = bfs_safe(bx, by, now, max_depth=max_reachable, speed=p["speed"])
        if path:
            bs["intent"] = "flee"
            bs["intent_reason"] = f"in blast, BFS->{len(path)} steps"
            bs["flee_path"] = _dirs_to_tiles(bx, by, path)
            # Lock the bot into "stay defensive" mode until the worst pending
            # bomb has not only detonated but its explosion has fully cleared,
            # plus a small buffer. Without this lock the bot drops out of
            # flee the instant it steps one tile clear, then walks back into
            # the still-burning explosion chasing a target.
            eff = effective_explode_times()
            worst = max((eff[id(b)] for b in STATE.bombs
                         if (bx, by) in bomb_blast_cells(b)),
                        default=now)
            bs["safe_until"] = max(bs["safe_until"],
                                   worst + EXPLOSION_DURATION + 0.25)
            bs["flee_goal"] = bs["flee_path"][-1] if bs["flee_path"] else None
            p["move_dir"] = path[0]
            return
        # No escape route at all — pick any passable neighbour. PREFER one
        # that's outside every blast (any breathing room beats none); only
        # fall back to a blast tile if all four neighbours are also lethal.
        dirs = (((1, 0, "right"), (-1, 0, "left"),
                 (0, 1, "down"), (0, -1, "up")))
        best = None
        for dx, dy, name in dirs:
            nx, ny = bx + dx, by + dy
            if not passable(nx, ny, bx, by):
                continue
            if not tile_in_any_blast(nx, ny):
                bs["intent"] = "flee"
                bs["intent_reason"] = f"no BFS path, side-step to {name}"
                bs["flee_path"] = [(nx, ny)]
                p["move_dir"] = name
                return
            if best is None:
                best = (name, nx, ny)
        if best:
            bs["intent"] = "stuck"
            bs["intent_reason"] = f"all sides lethal, fallback {best[0]}"
            bs["flee_path"] = [(best[1], best[2])]
            p["move_dir"] = best[0]
        else:
            bs["intent"] = "stuck"
            bs["intent_reason"] = "trapped, no passable neighbour"
            p["move_dir"] = None
        return

    # If we recently fled a bomb we still want to stay defensive — the
    # bomb's explosion lingers for EXPLOSION_DURATION after detonation, and
    # an over-eager bot recomputes targets every 50ms and walks back into
    # the burning tile. So while `safe_until` hasn't passed, only act if
    # we're (a) genuinely in the blast (handled above) or (b) heading to a
    # parked flee_goal we haven't reached yet. Otherwise stay put.
    if now < bs["safe_until"]:
        goal = bs["flee_goal"]
        if goal is not None and (bx, by) != goal:
            # Still on our way to the parked safe tile — keep walking the
            # rest of the path. Re-BFS from the current tile to handle any
            # new bombs that appeared since the original plan.
            path = bfs_safe(bx, by, now, max_depth=max_reachable, speed=p["speed"])
            if path:
                bs["intent"] = "flee"
                bs["intent_reason"] = f"committed flee, {len(path)} more"
                bs["flee_path"] = _dirs_to_tiles(bx, by, path)
                bs["flee_goal"] = bs["flee_path"][-1] if bs["flee_path"] else None
                p["move_dir"] = path[0]
                return
            # No new BFS path (everything is dangerous) — fall through to the
            # general logic so the side-step fallback can run.
        else:
            # Already at the safe tile. Sit tight until the explosion clears.
            bs["intent"] = "idle"
            bs["intent_reason"] = f"waiting at safe spot ({bs['safe_until'] - now:.1f}s)"
            p["move_dir"] = None
            return

    # 1. Find someone or something to attack.
    target = find_target(bx, by)
    if not target:
        bs["intent"] = "idle"
        bs["intent_reason"] = "no target in range"
        if random.random() < 0.3 or not p["move_dir"]:
            p["move_dir"] = _safe_wander_dir(bx, by)
        return

    tx, ty = target
    bs["target"] = (tx, ty)
    dist = abs(bx - tx) + abs(by - ty)

    # 2. Movement: A* toward the target. Prefer a path that routes around
    # known blast tiles; only fall through to a "may walk through danger"
    # path when no safe route exists (we'll still gate each step below).
    # astar() returns an empty list when we're already adjacent (its goal
    # test is dist <= 1) — that used to crash on path[0]. Treat empty path
    # as "stay put".
    path = astar(bx, by, tx, ty, avoid_blast=True)
    safe_route = path is not None
    if path is None:
        path = astar(bx, by, tx, ty)
    if path is None:
        # genuinely unreachable — wander, but never *toward* a blast.
        bs["intent"] = "seek"
        bs["intent_reason"] = f"target ({tx},{ty}) unreachable, wander"
        p["move_dir"] = _safe_wander_dir(bx, by)
    elif path:
        bs["seek_path"] = _dirs_to_tiles(bx, by, path)
        # Don't take a step that walks us straight into a future blast.
        dx, dy = {"right": (1, 0), "left": (-1, 0),
                  "down": (0, 1), "up": (0, -1)}[path[0]]
        if tile_in_any_blast(bx + dx, by + dy):
            bs["intent"] = "seek"
            bs["intent_reason"] = "next step in blast, waiting"
            p["move_dir"] = None
        else:
            bs["intent"] = "seek"
            bs["intent_reason"] = ("safe path " if safe_route else "unsafe path ") + \
                                  f"to ({tx},{ty})"
            p["move_dir"] = path[0]
    else:
        # already adjacent — stop moving, we're about to bomb anyway
        bs["intent"] = "seek"
        bs["intent_reason"] = f"adjacent to ({tx},{ty})"
        p["move_dir"] = None

    # 3. Drop a bomb when we're at or next to the target (dist <= 1 works for
    # both: caixas can never be on top of us, humans usually walk into us).
    # We also try to bomb when there's a destructible box right next to us
    # even if the target is far — this keeps bots breaking the map open.
    box_neighbour = adjacent_box(bx, by)
    can_bomb = (
        p["active_bombs"] < p["max_bombs"]
        and now - bs["last_bomb"] > bs["bomb_cooldown"]
        and (dist <= 1 or box_neighbour)
        and random.random() < bs["aggression"]
    )
    if can_bomb:
        # Don't drop a bomb we can't escape from — pre-check the retreat.
        # We pretend the bomb is already placed so bfs_safe accounts for it.
        STATE.bombs.append({
            "owner": sid, "x": bx, "y": by, "fire": p["fire"],
            "explode_at": now + BOMB_TIMER,
        })
        flee = bfs_safe(bx, by, now, max_depth=max_reachable, speed=p["speed"])
        STATE.bombs.pop()   # remove the probe bomb
        if flee:
            # Pin the tile so the actually-placed bomb lands EXACTLY where
            # the safety BFS imagined it. p["x"]/p["y"] can have crept across
            # an int boundary since we computed bx,by at the top, which would
            # leave the bot in the bomb's blast with the wrong flee path.
            place_bomb(sid, p, at=(bx, by))
            bs["last_bomb"] = now
            bs["last_bomb_xy"] = (bx, by)
            bs["intent"] = "bomb"
            bs["intent_reason"] = f"placed @({bx},{by}), flee {len(flee)} steps"
            bs["flee_path"] = _dirs_to_tiles(bx, by, flee)
            bs["flee_goal"] = bs["flee_path"][-1] if bs["flee_path"] else None
            # Stay defensive until our own bomb (and any chain it triggers)
            # has detonated AND its 0.5s explosion has cleared.
            bs["safe_until"] = max(bs["safe_until"],
                                   now + BOMB_TIMER + EXPLOSION_DURATION + 0.25)
            # Move along the escape path RIGHT NOW. Next tick the
            # tile_in_any_blast check at the top of this function will keep
            # us fleeing until we're out of the radius.
            p["move_dir"] = flee[0]
        else:
            bs["intent_reason"] += " | wanted bomb but no escape"


def adjacent_box(x, y):
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        if tile(x + dx, y + dy) == T_BOX:
            return True
    return False


def find_target(sx, sy):
    """Prefer the nearest human; fall back to nearest box."""
    best = None
    bd = 9999
    for sid, pl in STATE.players.items():
        if not pl["alive"] or pl["is_bot"]:
            continue
        d = abs(int(pl["x"]) - sx) + abs(int(pl["y"]) - sy)
        if d < bd:
            bd = d
            best = (int(pl["x"]), int(pl["y"]))
    if best and bd < 20:
        return best
    # else: find a box to break for powerups
    near = None
    nd = 9999
    for y in range(max(1, sy - 10), min(MAP_H - 1, sy + 10)):
        for x in range(max(1, sx - 10), min(MAP_W - 1, sx + 10)):
            if STATE.grid[y][x] == T_BOX:
                d = abs(x - sx) + abs(y - sy)
                if d < nd:
                    nd = d
                    near = (x, y)
    return near or best


def astar(sx, sy, tx, ty, limit=200, avoid_blast=False):
    """A* on the grid. Walls and (other) boxes block. If avoid_blast is
    True, tiles inside any pending bomb's blast also block — used by the
    bot's movement so it routes around danger instead of through it."""
    import heapq
    open_set = [(0, 0, sx, sy, [])]
    visited = set()
    steps = 0
    while open_set and steps < limit:
        steps += 1
        f, g, x, y, path = heapq.heappop(open_set)
        if (x, y) in visited:
            continue
        visited.add((x, y))
        if abs(x - tx) + abs(y - ty) <= 1:
            return path
        for dx, dy, name in ((1, 0, "right"), (-1, 0, "left"),
                             (0, 1, "down"), (0, -1, "up")):
            nx, ny = x + dx, y + dy
            if (nx, ny) in visited:
                continue
            t = tile(nx, ny)
            if t == T_WALL:
                continue
            if t == T_BOX and (nx, ny) != (tx, ty):
                continue
            if avoid_blast and (nx, ny) != (tx, ty) and tile_in_any_blast(nx, ny):
                continue
            ng = g + 1
            h = abs(nx - tx) + abs(ny - ty)
            heapq.heappush(open_set, (ng + h, ng, nx, ny, path + [name]))
    return None


# ---------------------------------------------------------------------------
# Snapshots & win
# ---------------------------------------------------------------------------
def send_snapshot():
    """Per-client snapshot with fog of war.

    - Alive players see the world centered on themselves.
    - Dead players (and disconnected-mid-round) become spectators centered
      on the first alive human, or the first alive bot if no humans remain.
    - True spectators (joined after start) likewise follow someone alive.

    Each viewer only sees players, bombs, powerups, explosions inside their
    own 15x13 viewport. The grid layout is sent in full.
    """
    now = time.time()
    VX, VY = 15, 13

    # Pick a fallback focus target for spectators — prefer alive humans so
    # spectators see the "real game", fall back to bots.
    alive_humans = [p for p in STATE.players.values()
                    if p["alive"] and not p["is_bot"]]
    alive_bots = [p for p in STATE.players.values()
                  if p["alive"] and p["is_bot"]]
    fallback = (alive_humans + alive_bots + [None])[0]

    only_bots_left = (not alive_humans) and bool(alive_bots)
    # Send the static grid only when it actually changed (a box broke or a new
    # map started). On the wire this drops ~1.6k ints per client per tick.
    send_grid = STATE.grid_dirty
    grid_payload = STATE.grid if send_grid else None

    def slice_for(focus):
        cx, cy = (focus["x"], focus["y"]) if focus else (MAP_W / 2, MAP_H / 2)
        x0, x1 = cx - VX / 2, cx + VX / 2
        y0, y1 = cy - VY / 2, cy + VY / 2

        def visible(x, y):
            return x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1

        players = []
        for sid2, p in STATE.players.items():
            if p is focus or visible(p["x"], p["y"]):
                players.append({
                    "id": sid2, "name": p["name"], "color": p["color"],
                    "icon": p["icon"],
                    "x": round(p["x"], 2), "y": round(p["y"], 2),
                    "alive": p["alive"], "is_bot": p["is_bot"],
                    "shield": max(0, p["shield_until"] - now),
                    "fire": p["fire"], "bombs": p["max_bombs"], "speed": p["speed"],
                    "hp": p["hp"],
                })
        bombs = [{"x": b["x"], "y": b["y"], "t": max(0, b["explode_at"] - now)}
                 for b in STATE.bombs if visible(b["x"], b["y"])]
        powerups = [{"x": x, "y": y, "k": k}
                    for (x, y), k in STATE.powerups.items() if visible(x, y)]
        poisons = [{"x": x, "y": y,
                    "armed": now >= ps["armed_at"],
                    "arm_in": max(0, ps["armed_at"] - now)}
                   for (x, y), ps in STATE.poisons.items() if visible(x, y)]
        explosions = []
        for e in STATE.explosions:
            vis_cells = [c for c in e["cells"] if visible(c[0], c[1])]
            if vis_cells:
                explosions.append({"cells": vis_cells})

        payload = {
            "t": now,
            "alive_total": sum(1 for p in STATE.players.values() if p["alive"]),
            "humans_alive": len(alive_humans),
            "only_bots_left": only_bots_left,
            "focus_id": id_of(focus),
            "players": players,
            "bombs": bombs,
            "explosions": explosions,
            "powerups": powerups,
            "poisons": poisons,
        }
        if send_grid:
            payload["grid_diff"] = grid_payload
        return payload

    def debug_payload():
        """Full-map snapshot for the AI debug overlay — no fog, every bot's
        last decision, every pending blast cell."""
        # Pre-compute all current blast cells once (sum of pending bombs).
        blast_cells = set()
        for b in STATE.bombs:
            blast_cells |= bomb_blast_cells(b)
        players = []
        for sid2, p in STATE.players.items():
            entry = {
                "id": sid2, "name": p["name"], "color": p["color"],
                "icon": p["icon"],
                "x": round(p["x"], 2), "y": round(p["y"], 2),
                "alive": p["alive"], "is_bot": p["is_bot"],
                "shield": max(0, p["shield_until"] - now),
                "fire": p["fire"], "bombs": p["max_bombs"], "speed": p["speed"],
                "hp": p["hp"],
            }
            if p["is_bot"]:
                bs = p.get("bot_state", {})
                entry["dbg"] = {
                    "intent": bs.get("intent", "?"),
                    "reason": bs.get("intent_reason", ""),
                    "flee": bs.get("flee_path") or [],
                    "seek": bs.get("seek_path") or [],
                    "target": bs.get("target"),
                    "tile": [int(p["x"]), int(p["y"])],
                }
            players.append(entry)
        bombs = [{"x": b["x"], "y": b["y"],
                  "t": max(0, b["explode_at"] - now),
                  "fire": b["fire"], "owner": b["owner"]}
                 for b in STATE.bombs]
        powerups = [{"x": x, "y": y, "k": k}
                    for (x, y), k in STATE.powerups.items()]
        poisons = [{"x": x, "y": y,
                    "armed": now >= ps["armed_at"],
                    "arm_in": max(0, ps["armed_at"] - now)}
                   for (x, y), ps in STATE.poisons.items()]
        explosions = [{"cells": list(e["cells"])} for e in STATE.explosions]
        return {
            "t": now,
            "alive_total": sum(1 for p in STATE.players.values() if p["alive"]),
            "humans_alive": len(alive_humans),
            "only_bots_left": only_bots_left,
            "focus_id": id_of(fallback),
            "players": players,
            "bombs": bombs,
            "explosions": explosions,
            "powerups": powerups,
            "poisons": poisons,
            # Full grid every tick in debug — bandwidth doesn't matter here.
            "grid_diff": STATE.grid,
            "debug": True,
            "blast_cells": list(blast_cells),
            "map_w": MAP_W, "map_h": MAP_H,
        }

    # Build the debug payload once if anyone wants it — it's expensive enough
    # (full grid, full player list) that we don't want to rebuild per viewer.
    dbg = debug_payload() if STATE.debug_viewers else None

    # Humans (alive get themselves, dead follow fallback).
    for sid, p in list(STATE.players.items()):
        if p["is_bot"]:
            continue
        if sid in STATE.debug_viewers:
            payload = dict(dbg)
            payload["spectating"] = not p["alive"]
            socketio.emit("snap", payload, to=sid)
            continue
        focus = p if p["alive"] else fallback
        payload = slice_for(focus)
        payload["spectating"] = not p["alive"]
        socketio.emit("snap", payload, to=sid)

    # Pure spectators (joined mid-round).
    for sid in list(STATE.spectators):
        if sid in STATE.debug_viewers:
            payload = dict(dbg)
            payload["spectating"] = True
            socketio.emit("snap", payload, to=sid)
            continue
        payload = slice_for(fallback)
        payload["spectating"] = True
        socketio.emit("snap", payload, to=sid)

    # Grid sent — clear dirty flag so subsequent snapshots stay small.
    STATE.grid_dirty = False


def id_of(player):
    if not player:
        return None
    for sid, p in STATE.players.items():
        if p is player:
            return sid
    return None


def check_win():
    alive = [(sid, p) for sid, p in STATE.players.items() if p["alive"]]
    humans_alive = [p for _, p in alive if not p["is_bot"]]
    if len(alive) <= 1 or (len(humans_alive) == 0 and STATE.phase == "playing"):
        winner = alive[0][1] if alive else None
        end_game(winner)


def end_game(winner):
    STATE.phase = "ended"
    name = winner["name"] if winner else "Ninguém"

    # Build a leaderboard for the end screen: alive players first, then dead
    # sorted by time-of-death (later = better), tie-break by kills.
    board = []
    for sid, p in STATE.players.items():
        board.append({
            "name": p["name"],
            "icon": p["icon"],
            "color": p["color"],
            "is_bot": p["is_bot"],
            "alive": p["alive"],
            "kills": p.get("kills", 0),
            "died_at": p.get("died_at"),
            "killed_by": p.get("killed_by"),
            "is_winner": (p is winner),
        })
    board.sort(key=lambda r: (
        not r["alive"],
        -(r["died_at"] or 0),
        -r["kills"],
    ))
    for i, row in enumerate(board, start=1):
        row["rank"] = i

    # stats
    if winner and not winner["is_bot"]:
        try:
            with db() as conn:
                conn.execute("UPDATE users SET wins=wins+1, games=games+1 WHERE username=%s",
                             (winner["name"],))
        except Exception:
            pass
    for sid, p in STATE.players.items():
        if not p["is_bot"] and p["name"] != name:
            try:
                with db() as conn:
                    conn.execute("UPDATE users SET games=games+1 WHERE username=%s",
                                 (p["name"],))
            except Exception:
                pass
    socketio.emit("end", {"winner": name, "leaderboard": board})
    # reset after a bit
    threading.Thread(target=delayed_reset, daemon=True).start()


def delayed_reset():
    time.sleep(15)
    with STATE.lock:
        STATE.reset_for_new_game()
        need_countdown = STATE.phase == "starting"
    socketio.emit("reset", {})
    if need_countdown:
        threading.Thread(target=lobby_countdown, daemon=True).start()
    broadcast_lobby()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    init_db()
    socketio.run(app, host="0.0.0.0", port=port, debug=False,
                 allow_unsafe_werkzeug=True)
