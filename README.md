# Bomberman Online

Bomberman multiplayer em browser. Flask + Socket.IO + Canvas. Mapa 60×52 (4× o original 15×13), até 16 jogadores, bots preenchem o resto.

## Instalar

```bash
pip install -r requirements.txt
```

## Correr

```bash
python app.py
```

Abre `http://localhost:5000` em vários separadores/PCs para testar.

Para jogar com amigos na mesma rede: descobre o teu IP local (`ipconfig` no Windows) e partilha `http://TEU_IP:5000`.

## Como funciona

- **Login**: SQLite (`users.db`) criado automaticamente. Regista-te uma vez.
- **Lobby**: o primeiro jogador a entrar inicia o contador de 2 minutos. Quando atingir o tempo (ou 16 jogadores), o jogo começa. Lugares vazios são preenchidos com bots de dificuldade aleatória (easy / medium / hard).
- **Controlos**: setas / WASD para mover, espaço para largar bomba.
- **Power-ups** (saem das caixas):
  - 🔥 **fire** — 35% — aumenta o alcance da bomba
  - ⚡ **speed** — 15% — aumenta velocidade
  - 💣 **bombs** — 15% — mais bombas simultâneas
  - 🛡 **shield** — 15% — escudo temporário (6s)
  - 20% das caixas não dão nada
- **Vitória**: último jogador vivo. Vitórias e jogos guardados na BD.

## Arquitetura

- `app.py` — Flask + Socket.IO; toda a lógica de jogo corre no servidor (anti-cheat por defeito)
- `templates/` — `login.html`, `register.html`, `menu.html`, `game.html`
- `static/css/style.css` — estilo
- `static/js/game.js` — cliente: input, render canvas, snapshots

Tick rate do servidor: 20 Hz. Snapshots ao cliente: 15 Hz.
