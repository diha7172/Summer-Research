"""
Launch the U.S. Demographics Explorer web app.

Serves the webapp/ folder over a tiny local HTTP server (Python stdlib only)
and opens it in your browser. No install required.

    py serve.py            # opens http://localhost:8000
    py serve.py --port 9000
    py serve.py --no-browser

(If webapp/data/ is empty, run  py census_bulk.py  first to pull the data.)
"""

import os
import sys
import argparse
import webbrowser
import http.server
import socketserver

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def end_headers(self):
        # always serve fresh data while iterating
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass  # quiet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(ROOT, "data", "index.json")):
        print("No data found in webapp/data/.")
        print("Run  py census_bulk.py  first (needs a Census API key), then re-run this.")
        # still serve so the page can show its instructions

    port = args.port
    for _ in range(20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("Could not find a free port.")
        return

    url = f"http://localhost:{port}/"
    print(f"U.S. Demographics Explorer running at  {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
