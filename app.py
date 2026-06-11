import json
import os
import time
import threading
from pathlib import Path
from flask import Flask, render_template, jsonify, request

_here = os.path.dirname(os.path.abspath(__file__))

# launcher.py sets these when running as a bundled .app; fall back to local paths
_templates = os.environ.get("TRENDGAME_TEMPLATES", os.path.join(_here, "templates"))
_terms_dir = os.environ.get("TRENDGAME_TERMS", os.path.join(_here, "terms"))

app = Flask(__name__, template_folder=_templates)

# ─── Game State ────────────────────────────────────────────────────────────────

game = {
    "phase": "setup",   # setup | show_term | loading | show_results | game_over
    "round": 0,
    "teams": [
        {"name": "Team 1", "score": 0, "round_score": 0, "final_round_score": 0,
         "current_term": "", "got_bonus": False, "place": 0},
        {"name": "Team 2", "score": 0, "round_score": 0, "final_round_score": 0,
         "current_term": "", "got_bonus": False, "place": 0},
        {"name": "Team 3", "score": 0, "round_score": 0, "final_round_score": 0,
         "current_term": "", "got_bonus": False, "place": 0},
    ],
    "terms": [],
    "settings": {
        "geo": "US",
        "date_range": "today 12-m",
        "timer": 60,
        "first_round_counts": True,
    },
    "error": None,
}

# ─── Preset Management ─────────────────────────────────────────────────────────

TERMS_DIR = Path(_terms_dir)
TERMS_DIR.mkdir(parents=True, exist_ok=True)


def list_presets():
    return sorted(f.stem for f in TERMS_DIR.glob("*.json"))


def load_preset(name):
    path = TERMS_DIR / f"{name}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_preset(name, data):
    with open(TERMS_DIR / f"{name}.json", "w") as f:
        json.dump(data, f, indent=2)


# ─── Google Trends ─────────────────────────────────────────────────────────────

# Pytrends reuses a cookie/session across calls in the same process.
# Creating it once avoids repeated cookie-fetch round trips.
_pytrends_instance = None
_pytrends_lock = threading.Lock()

def _get_pytrends():
    global _pytrends_instance
    from pytrends.request import TrendReq
    with _pytrends_lock:
        if _pytrends_instance is None:
            _pytrends_instance = TrendReq(
                hl="en-US",
                tz=360,
                timeout=(10, 35),
                requests_args={
                    "headers": {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                },
            )
        return _pytrends_instance


def fetch_trends_scores(terms, geo, timeframe):
    """Returns list of int scores (0–100) for each term. Retries 3×."""
    try:
        from pytrends.request import TrendReq  # noqa: ensure installed
    except ImportError:
        raise RuntimeError("pytrends is not installed. Run: pip install pytrends")

    clean = [t.strip() for t in terms]
    unique = list(dict.fromkeys(t for t in clean if t))
    if not unique:
        return [0] * len(clean)

    last_err = None
    for attempt in range(3):
        if attempt:
            # Longer back-off on 429: 8s then 20s
            delay = [8, 20][min(attempt - 1, 1)]
            time.sleep(delay)
        try:
            pt = _get_pytrends()
            # Small polite pause before every request
            time.sleep(1.5)
            pt.build_payload(kw_list=unique, timeframe=timeframe, geo=geo)
            df = pt.interest_over_time()

            if df.empty:
                score_map = {t: 0 for t in unique}
            else:
                score_map = {
                    t: int(df[t].mean()) if t in df.columns else 0
                    for t in unique
                }
            return [score_map.get(t, 0) for t in clean]
        except Exception as e:
            last_err = e
            # Force a fresh session on next attempt if we got rate-limited
            global _pytrends_instance
            with _pytrends_lock:
                _pytrends_instance = None

    raise RuntimeError(f"Google Trends failed after 3 attempts: {last_err}")


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(game)


@app.route("/api/teams", methods=["POST"])
def api_teams():
    names = request.json.get("names", [])
    game["teams"] = [
        {"name": n.strip(), "score": 0, "round_score": 0, "final_round_score": 0,
         "current_term": "", "got_bonus": False, "place": 0}
        for n in names if n.strip()
    ]
    return jsonify({"ok": True})


@app.route("/api/terms", methods=["POST"])
def api_terms():
    game["terms"] = request.json.get("terms", [])
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    game["settings"].update(request.json)
    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    if len(game["teams"]) < 2:
        return jsonify({"error": "Need at least 2 teams"}), 400
    if not game["terms"]:
        return jsonify({"error": "Need at least 1 term/round"}), 400

    game["round"] = 0
    game["error"] = None
    for t in game["teams"]:
        t.update(score=0, round_score=0, final_round_score=0,
                 current_term="", got_bonus=False, place=0)
    game["phase"] = "show_term"
    return jsonify({"ok": True})


@app.route("/api/submit", methods=["POST"])
def api_submit():
    guesses = request.json.get("guesses", {})
    for team in game["teams"]:
        team["current_term"] = guesses.get(team["name"], "").strip()

    game["phase"] = "loading"
    game["error"] = None

    def do_fetch():
        try:
            term_def = game["terms"][game["round"]]
            team_terms = [t["current_term"] for t in game["teams"]]
            scores = fetch_trends_scores(
                team_terms,
                game["settings"]["geo"],
                game["settings"]["date_range"],
            )

            multiplier = max(1, int(term_def.get("multiplier") or 1))
            bonus_term = (term_def.get("bonus_term") or "").strip().lower()
            bonus_pts = int(term_def.get("bonus_points") or 0)

            for i, team in enumerate(game["teams"]):
                raw = scores[i]
                final = raw * multiplier
                team["round_score"] = raw
                if bonus_term and team["current_term"].strip().lower() == bonus_term:
                    team["got_bonus"] = True
                    final += bonus_pts
                else:
                    team["got_bonus"] = False
                team["final_round_score"] = final

            game["phase"] = "show_results"
        except Exception as e:
            game["error"] = str(e)
            game["phase"] = "show_results"

    threading.Thread(target=do_fetch, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/override-scores", methods=["POST"])
def api_override():
    """Manual fallback when Google Trends is unavailable."""
    scores = request.json.get("scores", {})  # {team_name: int}
    term_def = game["terms"][game["round"]]
    multiplier = max(1, int(term_def.get("multiplier") or 1))
    bonus_term = (term_def.get("bonus_term") or "").strip().lower()
    bonus_pts = int(term_def.get("bonus_points") or 0)

    for team in game["teams"]:
        raw = int(scores.get(team["name"], 0))
        final = raw * multiplier
        if bonus_term and team["current_term"].strip().lower() == bonus_term:
            team["got_bonus"] = True
            final += bonus_pts
        else:
            team["got_bonus"] = False
        team["round_score"] = raw
        team["final_round_score"] = final

    game["error"] = None
    game["phase"] = "show_results"
    return jsonify({"ok": True})


@app.route("/api/next", methods=["POST"])
def api_next():
    is_first = game["round"] == 0
    counts = game["settings"].get("first_round_counts", True)

    for team in game["teams"]:
        if not is_first or counts:
            team["score"] += team.get("final_round_score", 0)
        team.update(round_score=0, final_round_score=0,
                    current_term="", got_bonus=False)

    game["round"] += 1

    if game["round"] >= len(game["terms"]):
        sorted_t = sorted(game["teams"], key=lambda t: t["score"], reverse=True)
        for i, t in enumerate(sorted_t):
            t["place"] = i + 1
        game["phase"] = "game_over"
    else:
        game["phase"] = "show_term"

    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    game["phase"] = "setup"
    game["round"] = 0
    game["error"] = None
    for t in game["teams"]:
        t.update(score=0, round_score=0, final_round_score=0,
                 current_term="", got_bonus=False, place=0)
    return jsonify({"ok": True})


@app.route("/api/presets")
def api_presets():
    return jsonify({"presets": list_presets()})


@app.route("/api/preset/<name>")
def api_get_preset(name):
    data = load_preset(name)
    return jsonify(data) if data else ("Not found", 404)


@app.route("/api/preset/<name>", methods=["POST"])
def api_save_preset(name):
    save_preset(name, request.json)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("\n  TrendGame is running → http://localhost:5001\n")
    app.run(debug=False, port=5001, host="0.0.0.0")
