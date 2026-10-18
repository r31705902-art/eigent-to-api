"""Real local TCP HTTP/SSE, still no real Eigent account or paid requests."""
import asyncio
import socket

import httpx
from openai import AsyncOpenAI
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app


class SlowStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"first"},"finish_reason":null}]}\n\n'
        await asyncio.sleep(30)
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed.set()


@pytest.mark.asyncio
async def test_tcp_sdk_cancel_closes_upstream(tmp_path):
    slow = SlowStream()
    async def handler(r):
        if r.url.path == '/api/v1/user':
            return httpx.Response(200, json={'id': 1})
        if r.url.path == '/api/v1/cloud-models':
            return httpx.Response(200, json={'models': [{'id': 'm', 'model_type': 'm', 'model_platform': 'openai', 'kind': 'chat'}]})
        if r.url.path == '/api/v1/user/key':
            return httpx.Response(200, json={'value': 'secret', 'api_url': 'https://model.example/v1'})
        return httpx.Response(200, stream=slow, headers={'Content-Type': 'text/event-stream'})

    settings = Settings(api_key='network-test-secret-long-enough', token='session', auth_file=str(tmp_path / 'auth.json'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as remote:
        app = create_app(settings, remote)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.01)
            assert server.started
            async with AsyncOpenAI(base_url=f'http://127.0.0.1:{port}/v1', api_key=settings.api_key, max_retries=0) as sdk:
                stream = await sdk.chat.completions.create(model='m', messages=[{'role': 'user', 'content': 'hello'}], stream=True)
                async for chunk in stream:
                    assert chunk.choices[0].delta.content == 'first'
                    break
                await stream.close()
            await asyncio.wait_for(slow.closed.wait(), timeout=3)
            for _ in range(100):
                if app.state.gateway.active == 0:
                    break
                await asyncio.sleep(.01)
            assert app.state.gateway.active == 0
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)
            sock.close()
