import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .agent import EigentAgent, input_messages, agent_to_chat, chat_to_agent_response
from .bridge import from_responses, sse, stream_responses, to_responses
from .config import Settings
from .errors import APIError
from .gateway import Gateway, transport
from .session import SessionStore, normalize_session

log = logging.getLogger('eigent-to-api')
# Access logs are disabled in the entrypoint: callbacks carry credentials in the URL.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


def create_app(settings=None, client=None):
    settings = settings or Settings()
    settings.validate()
    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout, connect=15), follow_redirects=False,
                                          limits=httpx.Limits(max_connections=settings.max_concurrency + 12), trust_env=False)
    sessions = SessionStore(settings, client)
    gateway = Gateway(settings, client, sessions)

    @asynccontextmanager
    async def lifespan(app):
        try:
            try:
                await sessions.load()
                if sessions.data:
                    await sessions.request('/api/v1/user')
                    log.info('Eigent session loaded and validated.')
            except Exception as e:
                # Do not log exception text: it can contain tokens or signed URLs.
                sessions.data = None
                gateway.invalidate()
                log.warning('Session initialization failed (%s). Import a valid session or sign in.', type(e).__name__)
            if settings.public_url:
                log.warning('Login on your own computer: python login.py --server %s', settings.public_url)
            elif not sessions.data:
                log.warning('No session. Set EIGENT_AUTH_URL, or run login.py locally and POST /auth/import.')
            yield
        finally:
            if owned_client:
                await client.aclose()

    app = FastAPI(title='Eigent to OpenAI API', version='0.2.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions, app.state.gateway = sessions, gateway

    @app.exception_handler(APIError)
    async def api_error(request, e):
        return JSONResponse(e.body(), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, e):
        # Never return validation input: it can be a session credential.
        return JSONResponse(APIError(400, 'Invalid request parameters.', 'invalid_request', 'invalid_request_error').body(), status_code=400)

    @app.exception_handler(httpx.HTTPError)
    async def network_error(request, e):
        status = 504 if isinstance(e, httpx.TimeoutException) else 502
        return JSONResponse(APIError(status, 'Eigent network request failed; no automatic generation retry was made.', 'upstream_network_error').body(), status_code=status)

    @app.exception_handler(Exception)
    async def unexpected_error(request, e):
        log.error('Unhandled error (%s); details suppressed to protect credentials.', type(e).__name__)
        return JSONResponse(APIError(500, 'Internal server error.', 'internal_error').body(), status_code=500)

    @app.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'; form-action 'none'"
        return response

    async def authorize(request: Request):
        provided = request.headers.get('authorization', '')
        if not hmac.compare_digest(provided.encode(), ('Bearer ' + settings.api_key).encode()):
            raise APIError(401, 'Invalid API key.', 'invalid_api_key', 'authentication_error')

    async def read_json(request):
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > settings.max_body:
                raise APIError(413, 'Request body exceeds MAX_REQUEST_BYTES.', 'request_too_large', 'invalid_request_error')
        try:
            body = json.loads(data)
        except (ValueError, UnicodeError):
            raise APIError(400, 'Body must be valid JSON.', 'invalid_json', 'invalid_request_error')
        if not isinstance(body, dict):
            raise APIError(400, 'Body must be a JSON object.', 'invalid_json', 'invalid_request_error')
        return body

    @app.get('/healthz')
    async def health():
        return {'status': 'ok', 'version': '0.2.0'}

    @app.get('/readyz')
    async def ready():
        # Readiness checks for configured session only; it does not spend model credits.
        return JSONResponse({'ready': bool(sessions.data)}, status_code=200 if sessions.data else 503)

    @app.get('/auth/status', dependencies=[Depends(authorize)])
    async def auth_status():
        return {'authenticated': bool(sessions.data), 'refresh_available': bool(sessions.data and sessions.data.get('refresh_token'))}

    @app.get('/admin/diagnostics', dependencies=[Depends(authorize)])
    async def diagnostics():
        return {'version': '0.2.0', 'api_mode': settings.api_mode, 'errors': gateway.diagnostics.list()}

    @app.post('/auth/import', dependencies=[Depends(authorize)])
    async def auth_import(request: Request):
        try:
            data = normalize_session(await read_json(request))
        except ValueError:
            raise APIError(400, 'Session JSON must contain token or access_token.', 'invalid_session', 'invalid_request_error')
        await sessions.validate_token(data['token'])
        async with sessions.lock:
            sessions.save(data)
            gateway.invalidate()
        return {'authenticated': True}

    @app.get('/v1/models', dependencies=[Depends(authorize)])
    async def models():
        rows = await gateway.catalog()
        return {'object': 'list', 'data': [{'id': m['id'], 'object': 'model', 'created': 0, 'owned_by': 'eigent'} for m in rows]}

    async def completion(request, responses_api=False):
        body = await read_json(request)
        name = body.get('model')
        if not isinstance(name, str) or not name:
            raise APIError(400, 'model is required.', 'invalid_model', 'invalid_request_error')
        if not isinstance(body.get('stream', False), bool):
            raise APIError(400, 'stream must be a boolean.', 'invalid_stream', 'invalid_request_error')
        if 'stream_options' in body and (not isinstance(body['stream_options'], dict) or set(body['stream_options']) - {'include_usage'} or ('include_usage' in body['stream_options'] and not isinstance(body['stream_options']['include_usage'], bool))):
            raise APIError(400, 'stream_options only supports boolean include_usage.', 'invalid_stream_options', 'invalid_request_error')
        if not responses_api:
            messages = body.get('messages')
            if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
                raise APIError(400, 'messages must be a nonempty array of objects.', 'invalid_messages', 'invalid_request_error')
        elif 'input' not in body:
            raise APIError(400, 'input is required.', 'invalid_input', 'invalid_request_error')
        # Fast fail instead of an unbounded queue. Single-process by design.
        if gateway.active >= settings.max_concurrency:
            raise APIError(429, 'Gateway concurrency limit reached.', 'gateway_busy', 'rate_limit_error')
        gateway.active += 1
        upstream = None
        transferred = False
        try:
            model = await gateway.model(name)
            is_responses = transport(model) == 'responses'
            if responses_api and not is_responses:
                raise APIError(400, 'This model does not advertise Responses transport.', 'unsupported_transport', 'invalid_request_error')
            if settings.api_mode == 'eigent-agent' and not responses_api:
                input_messages(body)  # Reject unsupported fields before starting SSE.
                for limit_name in ('max_tokens', 'max_completion_tokens'):
                    if limit_name in body and (not isinstance(body[limit_name], int) or isinstance(body[limit_name], bool) or body[limit_name] < 16):
                        raise APIError(400, 'Agent output token limits must be integers >=16.', 'invalid_token_limit', 'invalid_request_error')
                async def invoke(agent_model, payload):
                    protocol = transport(agent_model)
                    routed = payload if protocol == 'responses' else agent_to_chat(payload, agent_model)
                    r = await gateway.send(agent_model, routed, 'responses' if protocol == 'responses' else 'chat/completions', stream=False)
                    try:
                        data = r.json()
                        return data if protocol == 'responses' else chat_to_agent_response(data)
                    except ValueError:
                        raise APIError(502, 'Agent model returned invalid JSON.', 'invalid_agent_response')
                    finally:
                        await r.aclose()
                async def run():
                    try:
                        async with asyncio.timeout(settings.timeout):
                            return await EigentAgent(settings).run(body, model, invoke)
                    except TimeoutError:
                        raise APIError(504, 'Agent wall-clock timeout exceeded.', 'agent_timeout')
                if not body.get('stream'):
                    return await run()
                async def agent_stream():
                    task = asyncio.create_task(run())
                    try:
                        while not task.done():
                            done, _ = await asyncio.wait({task}, timeout=10)
                            if not done:
                                yield b': agent working\n\n'
                        result = await task
                        common = {k: result[k] for k in ('id', 'created', 'model')}
                        common['object'] = 'chat.completion.chunk'
                        def chunk(delta, finish=None):
                            return sse({**common, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})
                        yield chunk({'role': 'assistant', 'content': ''})
                        message = result['choices'][0]['message']
                        delta = {k: message[k] for k in ('content', 'refusal') if message.get(k) is not None}
                        yield chunk(delta)
                        yield chunk({}, result['choices'][0]['finish_reason'])
                        if (body.get('stream_options') or {}).get('include_usage'):
                            yield sse({**common, 'choices': [], 'usage': result['usage']})
                        yield b'data: [DONE]\n\n'
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        if isinstance(e, APIError):
                            err = e
                        elif isinstance(e, httpx.TimeoutException):
                            err = APIError(504, 'Agent model request timed out.', 'upstream_network_error')
                        else:
                            err = APIError(502, 'Agent execution failed.', 'agent_execution_failed')
                        yield sse(err.body())
                        yield b'data: [DONE]\n\n'
                    finally:
                        task.cancel()
                        from contextlib import suppress
                        with suppress(asyncio.CancelledError, Exception):
                            await task
                        gateway.active -= 1
                transferred = True
                return StreamingResponse(agent_stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})
            route = 'responses' if is_responses else 'chat/completions'
            if is_responses and not responses_api:
                try:
                    payload = to_responses(body)
                except (KeyError, TypeError, AttributeError):
                    raise APIError(400, 'Invalid tool or content structure.', 'invalid_messages', 'invalid_request_error')
            else:
                payload = body
            stream = body.get('stream', False)
            upstream = await gateway.send(model, payload, route, stream=stream)
            if not stream:
                try:
                    data = upstream.json()
                except ValueError:
                    raise APIError(502, 'Model returned invalid JSON.', 'invalid_model_response')
                if not isinstance(data, dict):
                    raise APIError(502, 'Model returned invalid JSON shape.', 'invalid_model_response')
                if data.get('error'):
                    raise APIError(502, 'Model returned an error envelope.', 'upstream_model_error')
                if is_responses and not responses_api:
                    try:
                        return from_responses(data, name)
                    except (KeyError, TypeError, AttributeError):
                        raise APIError(502, 'Invalid upstream Responses shape.', 'invalid_model_response')
                data['model'] = name
                return JSONResponse(data)
            if 'text/event-stream' not in upstream.headers.get('content-type', ''):
                raise APIError(502, 'Expected upstream SSE stream.', 'invalid_model_stream')
            async def generate():
                try:
                    if is_responses and not responses_api:
                        options = body.get('stream_options') or {}
                        async for chunk in stream_responses(upstream, name, bool(options.get('include_usage'))):
                            yield chunk
                    else:
                        # Preserve native tool-call deltas, usage and [DONE].
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    err = e if isinstance(e, APIError) else APIError(502, 'Upstream stream failed.', 'upstream_stream_failed')
                    yield sse(err.body())
                    yield b'data: [DONE]\n\n'
                finally:
                    await upstream.aclose()
                    gateway.active -= 1
            transferred = True
            return StreamingResponse(generate(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})
        finally:
            if not transferred:
                if upstream is not None:
                    await upstream.aclose()
                gateway.active -= 1

    @app.post('/v1/chat/completions', dependencies=[Depends(authorize)])
    async def chat(request: Request):
        return await completion(request)

    @app.post('/v1/responses', dependencies=[Depends(authorize)])
    async def responses(request: Request):
        return await completion(request, responses_api=True)

    return app
