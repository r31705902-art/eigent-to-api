import asyncio
import time
from urllib.parse import quote, urlsplit

from .config import https_url
from .errors import APIError

SUPPORTED_PLATFORMS = {'openai', 'azure', 'openai-compatible-model', 'openai_compatible_model', 'litellm', 'openrouter'}


def transport(model):
    capabilities = model.get('capabilities') or {}
    compat = capabilities.get('request_compatibility') or {}
    return compat.get('preferred_transport', 'chat_completions')


class Gateway:
    def __init__(self, settings, client, sessions):
        self.settings, self.client, self.sessions = settings, client, sessions
        self.catalog_cache = None
        self.key_cache = None
        self.catalog_lock, self.key_lock = asyncio.Lock(), asyncio.Lock()
        self.active = 0

    def invalidate(self):
        self.catalog_cache = self.key_cache = None

    async def catalog(self):
        async with self.catalog_lock:
            if self.catalog_cache:
                at, rev, data = self.catalog_cache
                if rev == self.sessions.revision and time.monotonic() - at < 60:
                    return data
            data = await self.sessions.request('/api/v1/cloud-models?kind=chat')
            if not isinstance(data.get('models'), list):
                raise APIError(502, 'Eigent catalog has no models array.', 'invalid_catalog')
            rows = [m for m in data['models'] if isinstance(m, dict) and m.get('kind', 'chat') == 'chat'
                    and m.get('model_platform') in SUPPORTED_PLATFORMS
                    and isinstance(m.get('id'), str) and isinstance(m.get('model_type'), str)
                    and transport(m) in {'chat_completions', 'responses'}]
            self.catalog_cache = time.monotonic(), self.sessions.revision, rows
            return rows

    async def model(self, name):
        for m in await self.catalog():
            if m['id'] == name:
                return m
        raise APIError(404, 'Unknown or unsupported model. Use GET /v1/models for available IDs.', 'model_not_found', 'invalid_request_error')

    async def key(self):
        async with self.key_lock:
            if self.key_cache:
                at, rev, data = self.key_cache
                if rev == self.sessions.revision and time.monotonic() - at < self.settings.key_ttl:
                    return data
            data = await self.sessions.request('/api/v1/user/key')
            if not isinstance(data.get('value'), str) or not data['value'] or not isinstance(data.get('api_url'), str):
                raise APIError(502, 'Eigent did not provide a model key and api_url.', 'invalid_model_key')
            try:
                https_url(data['api_url'], 'Eigent model api_url')
            except ValueError:
                raise APIError(502, 'Eigent provided an unsafe model API URL.', 'invalid_model_endpoint')
            if urlsplit(data['api_url']).query:
                raise APIError(502, 'Model api_url must not contain a query.', 'invalid_model_endpoint')
            self.key_cache = time.monotonic(), self.sessions.revision, data
            return data

    async def send(self, model, body, route, stream):
        key = await self.key()
        base = key['api_url'].rstrip('/')
        headers = {'Authorization': 'Bearer ' + key['value'], 'Content-Type': 'application/json'}
        if model['model_platform'] == 'azure' and route == 'chat/completions':
            # Same AzureOpenAI URL convention used by the upstream CAMEL client.
            if not urlsplit(base).path.rstrip('/').endswith('/openai'):
                base += '/openai'
            path = '/deployments/' + quote(model['model_type'], safe='') + '/chat/completions?api-version=2024-10-21'
            headers['api-key'] = key['value']
        else:
            path = '/' + route
        payload = {**body, 'model': model['model_type']}
        r = await self.client.send(self.client.build_request('POST', base + path, json=payload, headers=headers), stream=stream)
        if r.status_code >= 400:
            status = r.status_code
            await r.aclose()
            if status == 401:
                self.key_cache = None  # Re-resolve on next call; never replay a billable POST.
            message = f'Eigent model proxy returned HTTP {status}.'
            if status == 403:
                message += ' The proxy may enforce agent prompt, plan or access policy; this gateway does not bypass it.'
            if status == 429:
                message += ' Check account quota and rate limits.'
            raise APIError(status, message, f'eigent_model_http_{status}')
        return r
