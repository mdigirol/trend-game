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

# ─── Config (persists SerpAPI key and other user prefs) ────────────────────────

_app_support = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "TrendGame"
)
CONFIG_PATH = Path(os.environ.get("TRENDGAME_CONFIG", os.path.join(_app_support, "config.json")))


def load_config():
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text())
    except Exception:
        pass
    return {}


def save_config(data):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def get_serpapi_key():
    """Env var takes precedence (dev override), then config file."""
    return os.environ.get("SERPAPI_KEY", "").strip() or load_config().get("serpapi_key", "").strip()


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
# Direct requests implementation — no pytrends dependency.
# SerpAPI is used instead when SERPAPI_KEY is set in the environment.

import hashlib
import requests as _requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://trends.google.com/",
}

_session: "_requests.Session | None" = None
_session_lock = threading.Lock()

CACHE_DIR = Path(os.environ.get(
    "TRENDGAME_CACHE",
    os.path.join(os.path.expanduser("~"), "Library", "Application Support", "TrendGame", "cache"),
))
CACHE_TTL = 86400  # 24 hours


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_key(terms, geo, timeframe):
    payload = json.dumps({"t": sorted(t.lower() for t in terms), "g": geo, "f": timeframe})
    return hashlib.md5(payload.encode()).hexdigest()


def _cache_load(key):
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < CACHE_TTL:
            return data["scores"]
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _cache_save(key, scores):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"ts": time.time(), "scores": scores})
    )


# ── Session management ─────────────────────────────────────────────────────────

def _new_session():
    s = _requests.Session()
    s.headers.update(_HEADERS)
    # Pre-set GDPR consent cookie so Google doesn't redirect us
    s.cookies.set("CONSENT", "YES+cb", domain=".google.com")
    try:
        s.get("https://trends.google.com/", timeout=10)
        time.sleep(1.0)
    except Exception:
        pass
    return s


def _get_session(force=False):
    global _session
    with _session_lock:
        if _session is None or force:
            _session = _new_session()
        return _session


# ── Direct Google Trends API ───────────────────────────────────────────────────

def _parse_json(raw, label):
    """Strip Google's )]}' prefix and parse JSON, with a clear error if the body isn't JSON."""
    text = raw.strip()
    if text.startswith(")]}'"):
        text = text[4:].strip()
    if not text:
        raise RuntimeError(f"Google Trends returned an empty response ({label}) — likely rate-limited or blocked")
    if text.lstrip().startswith("<"):
        raise RuntimeError(f"Google Trends returned an HTML page ({label}) — likely a captcha or block page")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Google Trends returned unexpected content ({label}): {e}") from e


def _fetch_direct(terms, geo, timeframe):
    """
    Calls the undocumented Google Trends JSON API directly.
    Two-step: explore (gets widget token) → widgetdata/multiline (gets scores).
    """
    sess = _get_session()

    # Step 1 — explore: get the widget token for our keyword set
    req_body = json.dumps({
        "comparisonItem": [
            {"keyword": t, "geo": geo, "time": timeframe} for t in terms
        ],
        "category": 0,
        "property": "",
    })
    time.sleep(1.5)
    r1 = sess.get(
        "https://trends.google.com/trends/api/explore",
        params={"hl": "en-US", "tz": "360", "req": req_body},
        timeout=20,
    )
    r1.raise_for_status()
    widgets = _parse_json(r1.text, "explore").get("widgets", [])
    ts_widget = next((w for w in widgets if w.get("id") == "TIMESERIES"), None)
    if not ts_widget:
        return [0] * len(terms)

    # Step 2 — widgetdata: get the actual time series
    time.sleep(1.0)
    r2 = sess.get(
        "https://trends.google.com/trends/api/widgetdata/multiline",
        params={
            "hl": "en-US",
            "tz": "360",
            "req": json.dumps(ts_widget.get("request", {})),
            "token": ts_widget.get("token", ""),
        },
        timeout=20,
    )
    r2.raise_for_status()
    timeline = _parse_json(r2.text, "widgetdata").get("default", {}).get("timelineData", [])
    if not timeline:
        return [0] * len(terms)

    sums = [0] * len(terms)
    counts = [0] * len(terms)
    for point in timeline:
        for i, v in enumerate(point.get("value", [])):
            if i < len(terms):
                sums[i] += int(v)
                counts[i] += 1
    return [round(sums[i] / counts[i]) if counts[i] else 0 for i in range(len(terms))]


# ── SerpAPI fallback ───────────────────────────────────────────────────────────

def _fetch_serpapi(terms, geo, timeframe, api_key):
    r = _requests.get(
        "https://serpapi.com/search",
        params={
            "engine": "google_trends",
            "q": ",".join(terms),
            "date": timeframe,
            "geo": geo,
            "data_type": "TIMESERIES",
            "api_key": api_key,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    # SerpAPI may return an 'averages' shortcut
    averages = data.get("interest_over_time", {}).get("averages", [])
    if averages:
        avg_map = {a["query"].lower(): int(a["value"]) for a in averages}
        return [avg_map.get(t.lower(), 0) for t in terms]

    # Otherwise calculate from timeline_data
    timeline = data.get("interest_over_time", {}).get("timeline_data", [])
    sums = {t.lower(): 0 for t in terms}
    counts = {t.lower(): 0 for t in terms}
    for point in timeline:
        for v in point.get("values", []):
            q = v.get("query", "").lower()
            try:
                val = int(str(v.get("extracted_value", v.get("value", 0))).replace("<1", "0"))
            except (ValueError, TypeError):
                val = 0
            if q in sums:
                sums[q] += val
                counts[q] += 1
    return [
        round(sums[t.lower()] / counts[t.lower()]) if counts[t.lower()] else 0
        for t in terms
    ]


# ── Public entry point ─────────────────────────────────────────────────────────

def fetch_trends_scores(terms, geo, timeframe):
    """
    Returns a list of int scores (0–100) for each term.

    Priority:
      1. 24-hour local cache (no network call)
      2. SerpAPI (primary — real browser sessions, avoids bot detection)
      3. Direct Google Trends API (fallback — may get rate-limited or fake data)
    """
    clean = [t.strip() for t in terms]
    unique = list(dict.fromkeys(t for t in clean if t))
    if not unique:
        return [0] * len(clean)

    def to_result(scores):
        score_map = {t.lower(): s for t, s in zip(unique, scores)}
        return [score_map.get(t.lower(), 0) for t in clean]

    # 1 — Cache
    ck = _cache_key(unique, geo, timeframe)
    cached = _cache_load(ck)
    if cached is not None and len(cached) == len(unique):
        return to_result(cached)

    # 2 — SerpAPI (primary, if key is configured)
    serpapi_key = get_serpapi_key()
    serpapi_err = None
    if serpapi_key:
        try:
            scores = _fetch_serpapi(unique, geo, timeframe, serpapi_key)
            _cache_save(ck, scores)
            return to_result(scores)
        except Exception as e:
            serpapi_err = e  # fall through to direct, but remember the error

    # 3 — Direct Google Trends (fallback, 3 retries)
    last_err = None
    for attempt in range(3):
        if attempt:
            time.sleep([10, 25][attempt - 1])
        try:
            scores = _fetch_direct(unique, geo, timeframe)
            _cache_save(ck, scores)
            return to_result(scores)
        except Exception as e:
            last_err = e
            _get_session(force=True)

    if serpapi_err:
        raise RuntimeError(f"SerpAPI failed ({serpapi_err}); Google Trends also failed: {last_err}")
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


@app.route("/api/config")
def api_get_config():
    cfg = load_config()
    key = cfg.get("serpapi_key", "")
    return jsonify({
        "serpapi_key": key,
        "serpapi_key_set": bool(key),
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    cfg = load_config()
    data = request.json or {}
    if "serpapi_key" in data:
        cfg["serpapi_key"] = data["serpapi_key"].strip()
    save_config(cfg)
    return jsonify({"ok": True})


def _check_serpapi_key(key):
    """
    Validates a SerpAPI key via the /account endpoint (no search consumed).
    Returns dict: {ok, searches_left, plan, error}
    """
    if not key:
        return {"ok": False, "error": "No key provided"}
    try:
        r = _requests.get(
            "https://serpapi.com/account",
            params={"api_key": key},
            timeout=10,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "Invalid API key"}
        r.raise_for_status()
        data = r.json()
        return {
            "ok": True,
            "searches_left": data.get("searches_left", "?"),
            "plan": data.get("plan_name", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.route("/api/config/test-key", methods=["POST"])
def api_test_key():
    """Test a key passed in the request body, or the currently saved key."""
    data = request.json or {}
    key = data.get("key", "").strip() or get_serpapi_key()
    return jsonify(_check_serpapi_key(key))


if __name__ == "__main__":
    print("\n  TrendGame is running → http://localhost:5001\n")
    app.run(debug=False, port=5001, host="0.0.0.0")
