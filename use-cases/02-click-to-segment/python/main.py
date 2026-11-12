"""Click-to-segment backend: uv run main.py --port 8080 [--host 127.0.0.1] [--static ../web/dist]. Holds the API key; see contract/02-backend.yaml."""

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

import stack_client as sc
from backend import Backend, Server

EXIT_NO_KEY = 2
DEFAULT_STATIC = Path(__file__).resolve().parent.parent / "web" / "dist"  # next to this directory, whatever the working directory


def main() -> int:
    ap = argparse.ArgumentParser(description="Key-holding backend of the click-to-segment component.")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="directory of the page's files, served at / (default: ../web/dist)")
    args = ap.parse_args()

    # region server
    try:
        client = sc.StackClient()  # key, base URL and credit cap from FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    models = [m.strip() for m in (os.environ.get("SEGMENTER_MODELS") or "sam-lite").split(",") if m.strip()]
    server = Server(args.host, args.port, Backend(client, models=models, static_dir=args.static))

    def stop(signum, frame):
        threading.Thread(target=server.shutdown).start()  # serve_forever runs in this thread: shutdown must be called from another

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    print(f"listening on http://{args.host}:{server.server_address[1]}", flush=True)
    server.serve_forever()
    server.server_close()
    # endregion
    return 0


if __name__ == "__main__":
    sys.exit(main())
