#!/usr/bin/env python3
"""Serve the Hoop-X browser UI (static HTML/CSS/JS).

  python3 serve_web.py

Then open http://127.0.0.1:8765/
Data is stored in the browser (localStorage), independent of Streamlit swish.py.
"""
from __future__ import annotations

import http.server
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "web"
PORT = 8765


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f"Missing web root: {ROOT}")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def log_message(self, format: str, *args) -> None:
            return

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Hoop-X web → http://127.0.0.1:{PORT}/")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
