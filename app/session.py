import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import time

from .errors import APIError


def normalize_session(data):
    if not isinstance(data, dict):
        raise ValueError('Session must be a JSON object')
    # Also accept the authStore persisted by zustand (auth-storage), not cookies.
    if isinstance(data.get('state'), dict):
        data = data['state']
    token = data.get('token') or data.get('access_token')
    if not isinstance(token, str) or not token.strip() or len(token) > 32768 or any(c.isspace() for c in token):
        raise ValueError('Session requires a nonempty token or access_token')
    result = {'token': token}
    if isinstance(data.get('refresh_token'), str) and data['refresh_token']:
        result['refresh_token'] = data['refresh_token']
    return result


def expiring(token):
    # Decode expiry ONLY as a refresh hint. It is not signature validation.
    try:
        part = token.split('.')[1]
        exp = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))['exp']
        return float(exp) <= time.time() + 60
    except (IndexError, KeyError, ValueError, TypeError):
        return False


def checked_json(response):
    if response.status_code >= 400:
        status = response.status_code
        raise APIError(status, f'Eigent returned HTTP {status}.', f'eigent_http_{status}')
    try:
        data = response.json()
    except ValueError:
        raise APIError(502, 'Eigent returned invalid JSON.', 'invalid_upstream_response')
    if not isinstance(data, dict):
        raise APIError(502, 'Eigent returned an unexpected response.', 'invalid_upstream_response')
    code = str(data.get('code', 0))
    if code not in {'0', '21'}:
        status = 401 if code in {'11', '12', '13'} else 429 if code in {'20', '22'} else 403
        raise APIError(status, f'Eigent rejected the request (code {code}). Check session, plan, quota or policy.', 'eigent_rejected')
    return data


class SessionStore:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.data = None
        self.revision = 0
        self.lock = asyncio.Lock()

    async def load(self):
        s = self.settings
        if s.auth_url:
            headers = {'Authorization': f'Bearer {s.auth_url_bearer}'} if s.auth_url_bearer else {}
            # Do not follow redirects: a signed URL is a credential.
            async with self.client.stream('GET', s.auth_url, headers=headers, timeout=30) as r:
                if r.status_code != 200:
                    raise ValueError('EIGENT_AUTH_URL download failed')
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError('Session JSON exceeds 64 KiB')
            data = json.loads(body)
        elif s.auth_json:
            data = json.loads(s.auth_json)
        elif s.token:
            data = {'token': s.token}
        elif Path(s.auth_file).exists():
            data = json.loads(Path(s.auth_file).read_text())
        else:
            return
        self.save(data)

    def save(self, data):
        data = normalize_session(data)
        path = Path(self.settings.auth_file)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.data = data
        self.revision += 1

    async def validate_token(self, token):
        r = await self.client.get(self.settings.api_base + '/api/v1/user', headers={'Authorization': 'Bearer ' + token})
        checked_json(r)

    async def refresh(self, old_token):
        async with self.lock:
            if self.data and self.data['token'] != old_token:
                return
            if not self.data or not self.data.get('refresh_token'):
                raise APIError(401, 'Eigent session expired. Sign in again or replace the session JSON.', 'eigent_login_required', 'authentication_error')
            r = await self.client.post(self.settings.api_base + '/api/v1/user/refresh', json={'refresh_token': self.data['refresh_token']})
            new = normalize_session(checked_json(r))
            if 'refresh_token' not in new:
                new['refresh_token'] = self.data['refresh_token']
            self.save(new)

    async def request(self, path):
        if not self.data:
            raise APIError(503, 'Eigent login required. Run login.py locally or import a session.', 'eigent_login_required')
        token = self.data['token']
        if expiring(token) and self.data.get('refresh_token'):
            await self.refresh(token)
            token = self.data['token']
        async def get(t):
            return checked_json(await self.client.get(self.settings.api_base + path, headers={'Authorization': 'Bearer ' + t}))
        try:
            return await get(token)
        except APIError as e:
            if e.status != 401:
                raise
            await self.refresh(token)
            return await get(self.data['token'])
