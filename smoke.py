#!/usr/bin/env python3
"""Windows CMD/Linux/macOS smoke test. Stdlib only; never prints API_KEY."""
import argparse
import getpass
import http.client
import json
import os
import sys
from urllib.parse import urlsplit


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url', required=True)
    p.add_argument('--model')
    p.add_argument('--generate', action='store_true', help='One generation request; may spend account credits')
    args = p.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path.rstrip('/'):
        p.error('--base-url must be an HTTPS origin')
    key = os.getenv('API_KEY') or getpass.getpass('Gateway API_KEY (hidden): ')
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    report = []
    def request(path, data=None):
        c = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=300)
        try:
            c.request('POST' if data else 'GET', path, body=json.dumps(data) if data else None, headers=headers)
            r = c.getresponse()
            body = r.read(65536)
            try:
                result = json.loads(body)
            except ValueError:
                result = {'message': 'Non-JSON response (body omitted)'}
            report.append({'path': path, 'status': r.status, 'body': result})
            return r.status, result
        finally:
            c.close()
    ok = True
    for path in ['/healthz', '/readyz', '/auth/status']:
        status, _ = request(path)
        ok = ok and status == 200
    status, catalog = request('/v1/models')
    models = catalog.get('data', []) if isinstance(catalog, dict) else []
    ok = ok and status == 200 and bool(models)
    status, diagnostics = request('/admin/diagnostics')
    if status == 404:
        report.append({'notice': 'Old gateway deployed: /admin/diagnostics is unavailable. Deploy v0.2 first.'})
        ok = False
    if args.generate and models:
        model = args.model or models[0]['id']
        status, result = request('/v1/chat/completions', {'model': model, 'messages': [{'role': 'user', 'content': 'Say hello in one short sentence.'}], 'max_completion_tokens': 128})
        ok = ok and status == 200
        request('/admin/diagnostics')
    # Only server responses, no request headers or key. Diagnostics are server-redacted.
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, http.client.HTTPException):
        print('Network/JSON failure. Check the HTTPS origin and connectivity; credentials omitted.', file=sys.stderr)
        raise SystemExit(2)
