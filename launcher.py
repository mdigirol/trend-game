"""
Entry point for the packaged TrendGame.app.
Starts the Flask server and opens the browser automatically.
Run directly via `python3 launcher.py` or use `python3 app.py` for dev.
"""
import os
import sys
import time
import threading
import webbrowser

PORT = 5001


def resource_path(relative):
    """Resolve path to a bundled resource (works in both dev and PyInstaller)."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def user_terms_dir():
    """Persistent terms directory in the user's Application Support folder."""
    d = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "TrendGame", "terms"
    )
    os.makedirs(d, exist_ok=True)
    return d


def seed_terms(dest):
    """Copy bundled preset files into dest if it's empty."""
    import shutil

    src = resource_path("terms")
    if not os.path.isdir(src):
        return
    for fname in os.listdir(src):
        dst_file = os.path.join(dest, fname)
        if not os.path.exists(dst_file):
            shutil.copy2(os.path.join(src, fname), dst_file)


def open_browser():
    time.sleep(2.0)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    terms_dir = user_terms_dir()
    seed_terms(terms_dir)

    # Tell app.py where to find templates and the user's terms
    os.environ["TRENDGAME_TEMPLATES"] = resource_path("templates")
    os.environ["TRENDGAME_TERMS"] = terms_dir

    threading.Thread(target=open_browser, daemon=True).start()

    # Import after env vars are set
    from app import app

    print(f"\n  TrendGame → http://localhost:{PORT}\n")
    app.run(port=PORT, host="127.0.0.1", use_reloader=False, threaded=True)
