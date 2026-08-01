"""Serve the (patched) Cambridge 2D visualiser with a local replay auto-loaded.

Usage: python3 tools/watch2d.py <replay-file>

Same serving scheme as `cambc watch`: static dist + /local-replay endpoint.
The dist in tools/visualiser2d is patched for Titan's 2x2 top-left-anchored
core (Cambridge's was 3x3 centered); everything else renders unchanged.
"""

import http.server
import mimetypes
import sys
import threading
import webbrowser
from pathlib import Path

DIST = Path(__file__).parent / "visualiser2d"


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: watch2d.py <replay-file>", file=sys.stderr)
        raise SystemExit(1)
    replay_path = Path(sys.argv[1]).resolve()
    if not replay_path.is_file():
        print(f"replay not found: {replay_path}", file=sys.stderr)
        raise SystemExit(1)

    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/wasm", ".wasm")
    mimetypes.add_type("text/css", ".css")

    class ReplayHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DIST), **kwargs)

        def do_GET(self):
            if self.path == "/local-replay":
                data = replay_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            super().do_GET()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), ReplayHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?replayUrl=/local-replay"
    print(f"2D visualiser: {url}")
    print(f"Replay: {replay_path.name}")
    print("Press Ctrl+C to stop")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
