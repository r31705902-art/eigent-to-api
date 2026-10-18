import asyncio
import json
import os

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI

from app.config import Settings
from app.main import create_app

SECRET = 'test-only-gateway-secret-long-enough'
HEADERS = {'Authorization': 'Bearer ' + SECRET}

CATALOG = [
    {'id': 'chat-model', 'model_type': 'actual-chat', 'model_platform': 'openai', 'kind': 'chat'},
    {'id': 'responses-model', 'model_type': 'actual-response', 'model_platform': 'azure', 'kind': 'chat',
     'capabilities': {'request_compatibility': {'preferred_transport': 'responses'}}},
    {'id': 'azure-chat', 'model_type': 'azure-chat', 'model_platform': 'azure', 'kind': 'chat'},
    {'id': 'native-bedrock', 'model_type': 'claude', 'model_platform': 'aws-bedrock-converse', 'kind': 'chat'},
]
RESPONSE = {'id': 'resp_1', 'status': 'completed', 'output': [
    {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Привет'}]}],
    'usage': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}}
CHAT = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'actual-chat',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Привет'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}


def event(data):
    return ('data: ' + json.dumps(data) + '\n\n').encode()


class Upstream:
    def __init__(self):
        self.calls = []
        self.key_count = 0
        self.reject = None
        self.reject_user = False
        self.responses_events = None
        self.auth_download = {'token': 'downloaded-session'}
        self.catalog = CATALOG
        self.response = RESPONSE
        self.delay = 0

    async def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if request.url.host == 'session.example':
            return httpx.Response(200, json=self.auth_download)
        if path == '/api/v1/user/refresh':
            return httpx.Response(200, json={'access_token': 'refreshed-token', 'refresh_token': 'rotated-refresh'})
        if path == '/api/v1/user':
            return httpx.Response(200, json={'code': 13, 'text': 'invalid'} if self.reject_user else {'email': 'test@example.com', 'id': 7})
        if path == '/api/v1/cloud-models':
            return httpx.Response(200, json={'models': self.catalog})
        if path == '/api/v1/user/key':
            self.key_count += 1
            return httpx.Response(200, json={'value': 'upstream-only-key', 'api_url': 'https://proxy.example/v1'})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.reject:
            return httpx.Response(self.reject, json={'error': {'message': 'private detail upstream-only-key'}})
        body = json.loads(request.content)
        if path.endswith('/responses'):
            if body.get('stream'):
                events = self.responses_events or [
                    {'type': 'response.output_text.delta', 'delta': 'При'},
                    {'type': 'response.output_text.delta', 'delta': 'вет'},
                    {'type': 'response.completed', 'response': self.response}]
                return httpx.Response(200, content=b''.join(event(e) for e in events), headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json=self.response)
        if path.endswith('/chat/completions'):
            if body.get('stream'):
                chunk = {'id': 'chatcmpl-test', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'actual-chat',
                         'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Привет'}, 'finish_reason': 'stop'}]}
                return httpx.Response(200, content=event(chunk) + b'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json=CHAT)
        raise AssertionError('Unexpected route: ' + path)


@pytest_asyncio.fixture
async def env(tmp_path):
    upstream = Upstream()
    s = Settings(api_mode='passthrough', api_key=SECRET, token='initial-session', auth_file=str(tmp_path / 'auth.json'), public_url='https://gateway.example', max_body=4096)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(s, remote)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway.example') as api:
                yield api, app, upstream


@pytest.mark.asyncio
async def test_auth_health_models(env):
    api, app, up = env
    assert (await api.get('/healthz')).status_code == 200
    assert (await api.get('/readyz')).status_code == 200
    assert (await api.get('/v1/models')).status_code == 401
    r = await api.get('/v1/models', headers=HEADERS)
    assert {m['id'] for m in r.json()['data']} == {'chat-model', 'responses-model', 'azure-chat'}
    assert r.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_sdk_chat_and_stream(env):
    api, app, up = env
    async with AsyncOpenAI(api_key=SECRET, base_url='https://gateway.example/v1', http_client=api, max_retries=0) as sdk:
        result = await sdk.chat.completions.create(model='chat-model', messages=[{'role': 'user', 'content': 'hi'}])
        assert result.choices[0].message.content == 'Привет'
        assert result.model == 'chat-model'
        stream = await sdk.chat.completions.create(model='chat-model', messages=[{'role': 'user', 'content': 'hi'}], stream=True)
        assert ''.join([c.choices[0].delta.content or '' async for c in stream]) == 'Привет'
        assert app.state.gateway.active == 0
        assert up.key_count == 1
        generation = [r for r in up.calls if r.url.path.endswith('chat/completions')][-1]
        assert generation.headers['authorization'] == 'Bearer upstream-only-key'
        assert SECRET not in generation.content.decode()


@pytest.mark.asyncio
async def test_sdk_responses_bridge(env):
    api, app, up = env
    async with AsyncOpenAI(api_key=SECRET, base_url='https://gateway.example/v1', http_client=api, max_retries=0) as sdk:
        r = await sdk.chat.completions.create(model='responses-model', messages=[{'role': 'system', 'content': 'User supplied instruction'}, {'role': 'user', 'content': 'hi'}], max_completion_tokens=50)
        assert r.choices[0].message.content == 'Привет'
        assert r.usage.total_tokens == 6
        sent = json.loads(up.calls[-1].content)
        assert sent['max_output_tokens'] == 50
        assert sent['model'] == 'actual-response'
        assert sent['instructions'] == 'User supplied instruction'  # User text only; no spoofed agent prompt.
        assert sent['input'][0] == {'role': 'user', 'content': 'hi'}
        stream = await sdk.chat.completions.create(model='responses-model', messages=[{'role': 'user', 'content': 'hi'}], stream=True, stream_options={'include_usage': True})
        chunks = [c async for c in stream]
        assert ''.join(c.choices[0].delta.content or '' for c in chunks if c.choices) == 'Привет'
        assert chunks[-1].usage.total_tokens == 6
        assert chunks[-2].choices[0].finish_reason == 'stop'
        assert len({c.id for c in chunks}) == 1


@pytest.mark.asyncio
async def test_native_responses(env):
    api, app, up = env
    async with AsyncOpenAI(api_key=SECRET, base_url='https://gateway.example/v1', http_client=api, max_retries=0) as sdk:
        r = await sdk.responses.create(model='responses-model', input='hello')
        assert r.output_text == 'Привет'
        with pytest.raises(Exception):
            await sdk.responses.create(model='chat-model', input='hello')


@pytest.mark.asyncio
async def test_azure_route(env):
    api, app, up = env
    r = await api.post('/v1/chat/completions', headers=HEADERS, json={'model': 'azure-chat', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 200
    sent = up.calls[-1]
    assert sent.url.path == '/v1/openai/deployments/azure-chat/chat/completions'
    assert sent.url.params['api-version'] == '2024-10-21'
    assert sent.headers['api-key'] == 'upstream-only-key'


@pytest.mark.asyncio
async def test_import_validation_and_permissions(env):
    api, app, up = env
    assert (await api.post('/auth/import', json={'token': 'another'})).status_code == 401
    assert (await api.post('/auth/import', headers=HEADERS, json={'token': ''})).status_code == 400
    r = await api.post('/auth/import', headers=HEADERS, json={'state': {'token': 'another', 'refresh_token': 'refresh', 'email': 'not-stored'}})
    assert r.status_code == 200
    saved = json.loads(open(app.state.sessions.settings.auth_file).read())
    assert saved == {'token': 'another', 'refresh_token': 'refresh'}
    assert os.stat(app.state.sessions.settings.auth_file).st_mode & 0o777 == 0o600
    up.reject_user = True
    r = await api.post('/auth/import', headers=HEADERS, json={'token': 'bad'})
    assert r.status_code == 401
    assert app.state.sessions.data['token'] == 'another'


@pytest.mark.asyncio
async def test_errors_limits_and_no_secret_leak(env):
    api, app, up = env
    for code in [403, 429, 401, 500]:
        up.reject = code
        r = await api.post('/v1/chat/completions', headers=HEADERS, json={'model': 'chat-model', 'messages': [{'role': 'user', 'content': 'hi'}]})
        assert r.status_code == code
        assert 'upstream-only-key' not in r.text and 'private detail' not in r.text
        assert app.state.gateway.active == 0
    assert (await api.post('/v1/chat/completions', headers=HEADERS, content='x' * 5000)).status_code == 413
    assert (await api.post('/v1/chat/completions', headers=HEADERS, content='no json')).status_code == 400
    assert (await api.post('/v1/chat/completions', headers=HEADERS, json={'model': 'absent', 'messages': [{}]})).status_code == 404
    assert (await api.post('/v1/chat/completions', headers=HEADERS, json={'model': 'chat-model', 'messages': [], 'stream': 'true'})).status_code == 400


@pytest.mark.asyncio
async def test_concurrency_limit(env):
    api, app, up = env
    up.delay = .1
    app.state.gateway.settings.max_concurrency = 1
    body = {'model': 'chat-model', 'messages': [{'role': 'user', 'content': 'hi'}]}
    first = asyncio.create_task(api.post('/v1/chat/completions', headers=HEADERS, json=body))
    await asyncio.sleep(.02)
    second = await api.post('/v1/chat/completions', headers=HEADERS, json=body)
    assert second.status_code == 429
    assert (await first).status_code == 200
    assert app.state.gateway.active == 0


@pytest.mark.asyncio
async def test_stream_failure_and_incomplete(env):
    api, app, up = env
    body = {'model': 'responses-model', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}
    up.responses_events = [{'type': 'response.output_text.delta', 'delta': 'partial'}]
    r = await api.post('/v1/chat/completions', headers=HEADERS, json=body)
    assert 'upstream_stream_incomplete' in r.text
    assert r.text.endswith('data: [DONE]\n\n')
    assert app.state.gateway.active == 0
    up.responses_events = [{'type': 'response.incomplete', 'response': {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}}]
    r = await api.post('/v1/chat/completions', headers=HEADERS, json=body)
    assert '"finish_reason": "length"' in r.text


@pytest.mark.asyncio
async def test_refresh_rotation(env):
    api, app, up = env
    import base64
    token = 'a.' + base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=') + '.b'
    app.state.sessions.save({'token': token, 'refresh_token': 'old-refresh'})
    r = await api.get('/v1/models', headers=HEADERS)
    assert r.status_code == 200
    assert app.state.sessions.data == {'token': 'refreshed-token', 'refresh_token': 'rotated-refresh'}
    assert sum(x.url.path.endswith('/refresh') for x in up.calls) == 1


@pytest.mark.asyncio
async def test_download_and_missing_session(tmp_path):
    up = Upstream()
    s = Settings(api_mode='passthrough', api_key=SECRET, auth_url='https://session.example/auth.json', auth_url_bearer='download-secret', auth_file=str(tmp_path / 'auth.json'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(up)) as remote:
        app = create_app(s, remote)
        async with app.router.lifespan_context(app):
            assert app.state.sessions.data == {'token': 'downloaded-session'}
            assert up.calls[0].headers['authorization'] == 'Bearer download-secret'
    s = Settings(api_mode='passthrough', api_key=SECRET, auth_file=str(tmp_path / 'missing.json'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(up)) as remote:
        app = create_app(s, remote)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway.example') as api:
                assert (await api.get('/healthz')).status_code == 200
                assert (await api.get('/readyz')).status_code == 503
                assert (await api.get('/v1/models', headers=HEADERS)).status_code == 503


@pytest.mark.asyncio
async def test_unsupported_responses_parameters(env):
    api, app, up = env
    for extra in [{'n': 2}, {'logprobs': True}, {'messages': [{'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {}}]}]}]:
        body = {'model': 'responses-model', 'messages': [{'role': 'user', 'content': 'hi'}], **extra}
        r = await api.post('/v1/chat/completions', headers=HEADERS, json=body)
        assert r.status_code == 400
        assert app.state.gateway.active == 0


def test_settings_reject_insecure_defaults():
    with pytest.raises(ValueError):
        Settings(api_mode='passthrough', api_key='short').validate()
    with pytest.raises(ValueError):
        Settings(api_mode='passthrough', api_key=SECRET, auth_url='http://session.example/session').validate()
    with pytest.raises(ValueError):
        Settings(api_mode='passthrough', api_key=SECRET, public_url='https://example.com/path').validate()


@pytest.mark.asyncio
async def test_download_does_not_redirect_or_accept_oversize(tmp_path):
    for status, body in [(302, b''), (200, b'x' * 65537)]:
        seen = []
        async def handler(r):
            seen.append(r)
            return httpx.Response(status, content=body, headers={'Location': 'https://attacker.example/session'})
        s = Settings(api_mode='passthrough', api_key=SECRET, auth_url='https://session.example/private', auth_file=str(tmp_path / 'auth.json'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as remote:
            app = create_app(s, remote)
            async with app.router.lifespan_context(app):
                assert app.state.sessions.data is None
                assert len(seen) == 1


@pytest.mark.asyncio
async def test_missing_refresh_does_not_use_auto_login(env):
    api, app, up = env
    up.reject_user = True
    with pytest.raises(Exception):
        await app.state.sessions.request('/api/v1/user')
    assert not any('auto-login' in r.url.path for r in up.calls)


@pytest.mark.asyncio
async def test_bad_stream_options_are_400(env):
    api, app, up = env
    for options in ['bad', {'include_usage': 'yes'}, {'unknown': True}]:
        r = await api.post('/v1/chat/completions', headers=HEADERS, json={
            'model': 'responses-model', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True, 'stream_options': options})
        assert r.status_code == 400
