# TrendGame

A Google Trends party game. Teams guess which search terms are most popular on Google — higher search volume wins points.

## How to play

One person hosts on a laptop (ideally projected on a screen). For each round a topic is shown — each team shouts their best guess for what people search for. The host types in the guesses and submits. Google Trends scores each guess (0–100) and the highest score wins the round. Bonus terms and score multipliers add extra drama.

## Run from source

Requires Python 3.9+.

```bash
git clone https://github.com/YOUR_USERNAME/trend-game
cd trend-game
./start.sh          # installs deps + opens http://localhost:5001
```

Or manually:
```bash
pip3 install -r requirements.txt
python3 app.py
# then open http://localhost:5001
```

## Build a Mac .app

```bash
./build.sh
# → dist/TrendGame.app
```

Double-click `TrendGame.app` to launch. It opens automatically in your browser. Your saved term presets are stored in `~/Library/Application Support/TrendGame/terms/`.

## Game setup

- **Teams** — rename, add, or remove teams (2–8 players)
- **Rounds** — each round has a topic, an optional bonus term (exact match = extra points), and a point multiplier
- **Presets** — save and load term lists as JSON files in the `terms/` folder
- **Settings** — region, date range, timer length

## Fallback

If Google Trends is unavailable (rate limit), the results screen shows a manual score entry form — the host can look up the terms at [trends.google.com](https://trends.google.com) and enter scores by hand.

## Customizing term lists

Edit or add `.json` files in the `terms/` folder. Format:

```json
{
  "name": "My List",
  "settings": { "geo": "US", "date_range": "today 12-m", "timer": 60 },
  "terms": [
    { "text": "Topic shown to players", "bonus_term": "exact guess", "bonus_points": 15, "multiplier": 1 }
  ]
}
```
