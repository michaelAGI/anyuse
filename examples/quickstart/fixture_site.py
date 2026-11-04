"""A tiny package-search site for the offline quickstart (docs/QUICKSTART.md).

It stands in for a public site so the whole quickstart runs with NO network:
it listens on 127.0.0.1 only, over HTTPS with a certificate made on the spot
for 127.0.0.1 (the compiler only compiles https requests), and serves

    /robots.txt          allows everything
    /search?q=<text>     an HTML results page, as a browser would show it
    /api/search?q=<text> the JSON the page's own script loads

``--shape`` picks what the site serves (:data:`SHAPES`):

* ``original``: the JSON lists ``results[*].name``; the page lists the same names.
* ``changed``: the site ships a new API. The JSON moves the list to
  ``packages[*].title``. The HTML page is the same, so a person would see
  nothing wrong; the compiled tool must notice (and can heal from the page).
* ``gone``: the site removes the data. The API answers 410 and the page shows
  a notice instead of results. Nothing can be healed from that.

The other shapes exist for tests/bench/heal_bench.py: ``html_renamed`` (the
page's list gets new class names), ``js_only`` and ``js_only_changed`` (the
server sends an empty list and the page's script fills it in), ``api_gone``
(the API answers 410, the page still lists the results) and
``changed_lying_page`` (the API moved and the page lists other packages).

    python fixture_site.py --port 8443 --certdir DIR [--shape original|changed|gone|...]

The certificate and key are written to DIR (``cert.pem``, ``key.pem``) the
first time and reused afterwards; point ``SSL_CERT_FILE`` at ``cert.pem`` so
AnyUse's HTTP client trusts it. Nothing here is a secret: the key exists only
for this loopback fixture.
"""
from __future__ import annotations

import argparse
import datetime
import html
import ipaddress
import json
import os
import socketserver
import ssl
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORDS = ["amber", "birch", "cedar", "cloud", "delta", "ember", "falcon", "harbor",
         "maple", "river", "stone", "tulip"]
SUFFIXES = ["core", "cli", "kit", "lite", "sdk", "tools"]
# Six packages per word, in a fixed per-word order so the result order is a
# property of the site, not of the query.
CATALOG = [f"{w}-{s}" for w in WORDS for s in SUFFIXES[WORDS.index(w) % 6:] + SUFFIXES[:WORDS.index(w) % 6]]


#: shape -> (what /api/search serves, what /search shows)
SHAPES = {
    "original": ("results", "list"),
    "changed": ("packages", "list"),
    "gone": ("gone", "notice"),
    "html_renamed": ("results", "renamed_list"),
    "js_only": ("results", "script"),
    "js_only_changed": ("packages", "script"),
    "api_gone": ("gone", "list"),
    "changed_lying_page": ("packages", "featured"),
}


def search(q: str):
    q = q.strip().lower()
    return [name for name in CATALOG if q and q in name][:10]


def featured(q: str):
    """Other packages than the search finds, changing with the query (``changed_lying_page``)."""
    q = q.strip().lower()
    other = WORDS[(sum(map(ord, q)) + 5) % len(WORDS)]
    if other in q:
        other = WORDS[(WORDS.index(other) + 1) % len(WORDS)]
    return [f"{n}-pro" for n in search(other)]


def api_response(q: str, shape: str = "original"):
    """``(status, payload)`` for ``/api/search?q=``."""
    api = SHAPES[shape][0]
    names = search(q)
    if api == "gone":
        return 410, {"error": "this API was retired"}
    if api == "packages":
        return 200, {"total": len(names), "packages": [{"title": n} for n in names]}
    return 200, {"count": len(names),
                 "results": [{"name": n, "downloads": 1000 - 7 * i} for i, n in enumerate(names)]}


def page_html(q: str, shape: str = "original") -> str:
    """The results page for ``/search?q=``."""
    page = SHAPES[shape][1]
    names = featured(q) if page == "featured" else search(q)
    if page == "renamed_list":
        rows = "".join(f'<li class="hit"><a class="pkg" href="/pkg/{html.escape(n)}">'
                       f"{html.escape(n)}</a></li>" for n in names)
        region = f"<ol class='hits'>{rows}</ol>"
    elif page == "script":
        # the server sends an empty list; the page's script fills it from the API
        region = "<ul id='results'></ul>"
    elif page == "notice":
        region = "<p class='notice'>Search has moved to our app. No results are shown here.</p>"
    else:
        rows = "".join(f'<li class="result"><a href="/pkg/{html.escape(n)}">'
                       f"{html.escape(n)}</a></li>" for n in names)
        region = f"<ul id='results'>{rows}</ul>"
    return (f"<!doctype html><html><head><title>Search results for {html.escape(q)}"
            f" - Fixture Packages</title></head><body><nav><a href='/'>Fixture Packages</a>"
            f"</nav><form action='/search'><input name='q' value='{html.escape(q)}'></form>"
            f"{region}"
            f"<script>fetch('/api/search?q='+encodeURIComponent({json.dumps(q)}))</script>"
            f"</body></html>")


def respond(path: str, shape: str = "original"):
    """``(status, content type, body)`` for a GET of ``path`` (path and query)."""
    url = urllib.parse.urlsplit(path)
    q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
    if url.path == "/robots.txt":
        return 200, "text/plain", "User-agent: *\nAllow: /\n"
    if url.path == "/api/search":
        status, payload = api_response(q, shape)
        return status, "application/json", json.dumps(payload)
    if url.path in ("/", "/search"):
        return 200, "text/html; charset=utf-8", page_html(q, shape)
    return 404, "text/plain", "not found"


def make_cert(certdir: str):
    cert, key = os.path.join(certdir, "cert.pem"), os.path.join(certdir, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    os.makedirs(certdir, exist_ok=True)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:  # fall back to the openssl binary if it exists
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "2",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, capture_output=True)
        return cert, key
    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
           .public_key(pk.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(minutes=5))
           .not_valid_after(now + datetime.timedelta(days=2))
           .add_extension(x509.SubjectAlternativeName(
               [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
           .sign(pk, hashes.SHA256()))
    with open(key, "wb") as fh:
        fh.write(pk.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert, "wb") as fh:
        fh.write(crt.public_bytes(serialization.Encoding.PEM))
    return cert, key


class Handler(BaseHTTPRequestHandler):
    shape = "original"

    def _send(self, status, ctype, body: str):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        return self._send(*respond(self.path, self.shape))

    def log_message(self, *_args):
        pass


class LoopbackServer(ThreadingHTTPServer):
    """http.server's server_bind also runs socket.getfqdn(host): a reverse DNS lookup that, on macOS,
    raises the Local Network permission prompt for Python and stalls until it is answered."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--certdir", required=True)
    ap.add_argument("--shape", choices=sorted(SHAPES), default="original")
    args = ap.parse_args()
    cert, key = make_cert(args.certdir)
    Handler.shape = args.shape
    server = LoopbackServer(("127.0.0.1", args.port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"fixture: https://127.0.0.1:{args.port} shape={args.shape}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
