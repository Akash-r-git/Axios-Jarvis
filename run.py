#!/usr/bin/env python3
"""
Maestro Twin — one command to run the whole thing.

    python3 run.py                 start on http://127.0.0.1:8000 and open a browser
    python3 run.py --port 9000     different port
    python3 run.py --no-browser    just serve
    python3 run.py --check         run self-tests and exit

No pip install. No npm install. No network. Python 3.10 or newer.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BANNER = r"""
  ┌─────────────────────────────────────────────────────────────┐
  │  MAESTRO TWIN  ·  production-line digital twin              │
  │  discrete-event simulation · bottleneck intelligence        │
  └─────────────────────────────────────────────────────────────┘
"""


def free_port(host: str, port: int, tries: int = 20) -> int:
    for p in range(port, port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"No free port in {port}..{port+tries}. "
                     f"Pass --port with something else.")


def preflight() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(f"Python 3.10+ required, found "
                         f"{sys.version_info.major}.{sys.version_info.minor}.")
    from dtwin import Plant
    here = os.path.dirname(os.path.abspath(__file__))
    for fn in sorted(os.listdir(os.path.join(here, "plants"))):
        if fn.endswith(".json"):
            errs = Plant.load(os.path.join(here, "plants", fn)).validate()
            if errs:
                raise SystemExit(f"plants/{fn} is invalid: {errs}")


def check_ui() -> bool:
    """Run the Node interface tests. Node is not a requirement to run the
    app, so a missing runtime is reported and skipped, not failed."""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        print("  SKIP  interface tests (node not installed; the app does "
              "not need it)\n")
        return True
    return subprocess.call([node, os.path.join(here, "tests", "test_ui.js")],
                           cwd=here) == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="run the engine and interface self-tests and exit")
    args = ap.parse_args()

    if args.check:
        from tests.test_engine import run_all
        engine_ok = run_all()
        suites_ok = True
        total_p = total_f = 0
        for mod in ("tests.test_live", "tests.test_ingest"):
            try:
                m = __import__(mod, fromlist=["S"])
            except ImportError as exc:
                print(f"  SKIP  {mod} ({exc})")
                continue
            p_, f_ = m.S.run()
            total_p += p_
            total_f += f_
            suites_ok = suites_ok and f_ == 0
        ui_ok = check_ui()
        print(f"  intake suites: {total_p} passed, {total_f} failed\n")
        return 0 if (engine_ok and suites_ok and ui_ok) else 1

    preflight()
    from server.api import serve

    port = free_port(args.host, args.port)
    httpd = serve(args.host, port)
    url = f"http://{args.host}:{port}/"

    print(BANNER)
    print(f"  Running at  {url}")
    print(f"  Lines       {len([f for f in os.listdir('plants') if f.endswith('.json')])} in plants/")
    print(f"  Stop with   Ctrl-C\n")

    if not args.no_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.8), webbrowser.open(url)),
            daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
