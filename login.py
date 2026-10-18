#!/usr/bin/env python3
"""Run on YOUR computer, not Railway. Python standard library only (3.10+)."""
import argparse
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request
import webbrowser


def save_session(path, session):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(session, file)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def upload_session(server, key, session):
    p = urlsplit(server)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path.rstrip('/'):
        raise ValueError('--server must be an HTTPS origin, without path/query/credentials')
    # Never send a credential through an HTTP redirect.
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = Request(server.rstrip('/') + '/auth/import', data=json.dumps(session).encode(), method='POST',
                  headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    with build_opener(NoRedirect()).open(req, timeout=45) as response:
        if response.status != 200 or not json.load(response).get('authenticated'):
            raise ValueError('Server did not confirm session import')


def make_server(on_session, port=0):
    state = secrets.token_urlsafe(32)
    finished = threading.Event()
    deadline = time.monotonic() + 900

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log callback URLs containing tokens.

        def do_GET(self):
            parsed = urlsplit(self.path)
            args = parse_qs(parsed.query)
            received_state = args.get('state', [''])[0]
            token = args.get('token', [''])[0]
            valid = parsed.path == '/auth/callback' and not finished.is_set() and time.monotonic() < deadline
            valid = valid and secrets.compare_digest(received_state, state)
            valid = valid and bool(token) and len(token) <= 32768 and not any(c.isspace() for c in token)
            status, text = 400, 'Invalid or expired login callback. Restart login.py.'
            if valid:
                try:
                    on_session({'token': token})
                    finished.set()
                    status, text = 200, 'Signed in. Session saved/imported. You can close this tab.'
                except Exception:
                    status, text = 502, 'Could not save/import session. Check server URL, API_KEY and connectivity, then retry login.'
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(text.encode())

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.timeout = .5
    return server, state, finished


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', help='Upload directly to your gateway HTTPS origin')
    parser.add_argument('--output', help='Save private session JSON (default: auth.json when --server is absent)')
    parser.add_argument('--port', type=int, default=0, help='Loopback callback port (default: random free port)')
    parser.add_argument('--no-browser', action='store_true', help='Print link without opening browser')
    args = parser.parse_args()
    key = os.getenv('API_KEY') or (getpass.getpass('Gateway API_KEY (hidden): ') if args.server else None)
    output = args.output or (None if args.server else 'auth.json')

    def accept(session):
        if args.server:
            upload_session(args.server, key, session)
        if output:
            save_session(output, session)

    server, state, finished = make_server(accept, args.port)
    callback = 'http://127.0.0.1:' + str(server.server_port) + '/auth/callback?' + urlencode({'state': state})
    link = 'https://www.eigent.ai/signin?' + urlencode({'callbackUrl': callback})
    print('Open this link in a browser ON THIS computer (expires in 15 minutes):\n' + link, flush=True)
    if not args.no_browser:
        webbrowser.open(link)
    deadline = time.monotonic() + 900
    try:
        while not finished.is_set() and time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    if not finished.is_set():
        print('Login timed out; run login.py again.')
        return 1
    print('Session imported to server.' if args.server else 'Private session saved to ' + output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
