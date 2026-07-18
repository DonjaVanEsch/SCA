"""
Persistent fingerprint-target app for the client-fingerprinting experiment.

Runs continuously (unlike the per-language server images, which are started
per test) and accepts calls from generated client images. It doesn't inspect
or store anything itself -- the actual fingerprint is the raw network
capture taken externally by a tcpdump sidecar attached to THIS container's
network namespace while a client container makes its one outbound call (see
manager.py's _capture_client_fingerprint, which mirrors the existing
server-side _capture_fingerprint the same way, just with the sniffer and the
"thing driving traffic" swapped). Any method/path is accepted and always
answered with 200 + a small JSON body, so the client's own success/failure
handling never gets in the way of the capture.

Listens on both plain HTTP (9000, for stdlib/requests/httpx/urllib3 clients)
and TLS (9443, self-signed cert baked in at image build time) -- the
crypto-lib-driven raw clients (pyopenssl-raw/m2crypto-raw) need something
that actually speaks TLS to make their own handshake implementation visible
at all.
"""

import http.server
import json
import ssl
import threading
from datetime import datetime, timezone

HTTP_PORT  = 9000
HTTPS_PORT = 9443
CERT_FILE  = "/certs/cert.pem"
KEY_FILE   = "/certs/key.pem"


class Handler(http.server.BaseHTTPRequestHandler):
    def _respond(self):
        body = json.dumps({
            "target": "pqc-fingerprint-target",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "method": self.command,
            "path": self.path,
            "tls": isinstance(self.connection, ssl.SSLSocket),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def do_PUT(self):
        self._respond()

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # stay quiet -- the real record is the external packet capture


# ThreadingHTTPServer, not plain HTTPServer -- confirmed the hard way: a
# single client that hangs mid-request (e.g. a raw-TLS client's ClientHello
# arriving at the plain HTTP listener) wedges a single-threaded server for
# every other client forever after, surfacing as a ~2 minute TCP-connect
# timeout on totally unrelated, otherwise-working combos. Handler has no
# shared mutable state (each response is built purely from the request's
# own attributes), so concurrent handling needs no locking.
def _serve_http():
    http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


def _serve_https():
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=_serve_https, daemon=True).start()
    _serve_http()
