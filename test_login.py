from http.client import HTTPConnection
import json
import os
import threading
from urllib.parse import urlencode

import pytest
from login import make_server, save_session, upload_session


def test_localhost_state_single_use_and_no_echo():
    saved = []
    server, state, finished = make_server(saved.append)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def get(params):
        c = HTTPConnection('127.0.0.1', server.server_port)
        c.request('GET', '/auth/callback?' + urlencode(params))
        r = c.getresponse()
        result = r.status, r.read().decode()
        c.close()
        return result
    try:
        assert get({'state': 'wrong', 'token': 'secret-token'})[0] == 400
        assert not finished.is_set()
        status, body = get({'state': state, 'token': 'secret-token'})
        assert status == 200 and 'secret-token' not in body
        assert saved == [{'token': 'secret-token'}] and finished.is_set()
        assert get({'state': state, 'token': 'secret-token'})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_private_export_and_https_only(tmp_path):
    path = tmp_path / 'auth.json'
    save_session(path, {'token': 'session'})
    assert json.loads(path.read_text()) == {'token': 'session'}
    assert os.stat(path).st_mode & 0o777 == 0o600
    for url in ['http://example.com', 'https://a:b@example.com', 'https://example.com/path']:
        with pytest.raises(ValueError):
            upload_session(url, 'key', {'token': 'session'})
