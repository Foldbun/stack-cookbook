"""Click-to-segment backend: uv run main.py --port 8080 [--host 127.0.0.1] [--static ../web/dist].
Works without a key (free tier) or with FOLDBUN_API_KEY; see contract/02-backend.yaml."""

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

import stack_client as sc
from backend import Backend, Server

DEFAULT_STATIC = Path(__file__).resolve().parent.parent / "web" / "dist"  # next to this directory, whatever the working directory


def main() -> int:
    ap = argparse.ArgumentParser(description="Backend of the click-to-segment component.")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="directory of the page's files, served at / (default: ../web/dist)")
    args = ap.parse_args()

    # region server
    # No key: the free tier, and the backend sends the image inline with every click. With FOLDBUN_API_KEY: it uploads
    # each image once and sends its handle. The base URL, project and credit cap come from FOLDBUN_API_BASE,
    # FOLDBUN_PROJECT_ID and FOLDBUN_MAX_CREDITS.
    client = sc.StackClient()
    models = [m.strip() for m in (os.environ.get("SEGMENTER_MODELS") or "sam-lite").split(",") if m.strip()]
    server = Server(args.host, args.port, Backend(client, models=models, static_dir=args.static))

    def stop(signum, frame):
        threading.Thread(target=server.shutdown).start()  # serve_forever runs in this thread: shutdown must be called from another

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    print(f"listening on http://{args.host}:{server.server_address[1]}", flush=True)
    print("mode: " + ("free tier (images are sent inline with every click)" if client.free else "API key (images are uploaded once)"), flush=True)
    server.serve_forever()
    server.server_close()
    # endregion
    return 0


if __name__ == "__main__":
    sys.exit(main())
