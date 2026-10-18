"""Explicit, limited Chat Completions <-> Responses conversion. No prompt injection."""
import json
import time
import uuid

from .errors import APIError


def invalid(message):
    return APIError(400, message, 'unsupported_parameter', 'invalid_request_error')


def to_responses(body):
    allowed = {'model', 'messages', 'stream', 'stream_options', 'temperature', 'top_p',
               'max_tokens', 'max_completion_tokens', 'tools', 'tool_choice', 'parallel_tool_calls',
               'response_format', 'reasoning_effort', 'user', 'store', 'metadata', 'n'}
    extra = set(body) - allowed
    if extra:
        raise invalid('Responses-backed model does not support: ' + ', '.join(sorted(extra)))
    if body.get('n', 1) != 1:
        raise invalid('Responses-backed models require n=1.')
    result = {k: body[k] for k in ('model', 'stream', 'temperature', 'top_p', 'parallel_tool_calls', 'user', 'store', 'metadata') if k in body}
    result['input'] = []
    for msg in body['messages']:
        if set(msg) - {'role', 'content', 'tool_calls', 'tool_call_id'}:
            raise invalid('Unsupported message fields for Responses-backed model.')
        role, content = msg.get('role'), msg.get('content')
        if role == 'tool':
            if not isinstance(msg.get('tool_call_id'), str) or not isinstance(content, str):
                raise invalid('Tool messages need string content and tool_call_id.')
            result['input'].append({'type': 'function_call_output', 'call_id': msg['tool_call_id'], 'output': content})
            continue
        if role not in {'system', 'developer', 'user', 'assistant'}:
            raise invalid('Unsupported message role.')
        if content is not None and content != '':
            if isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, dict):
                        raise invalid('Content parts must be objects.')
                    if part.get('type') == 'text' and isinstance(part.get('text'), str):
                        parts.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': part['text']})
                    elif part.get('type') == 'image_url' and role == 'user':
                        image = part.get('image_url', {})
                        if not isinstance(image, dict) or not isinstance(image.get('url'), str):
                            raise invalid('image_url must contain a URL.')
                        parts.append({'type': 'input_image', 'image_url': image['url'], **({'detail': image['detail']} if 'detail' in image else {})})
                    else:
                        raise invalid('Only text and user image_url content parts are supported.')
                content = parts
            elif not isinstance(content, str):
                raise invalid('Message content must be text, an array or null.')
            result['input'].append({'role': role, 'content': content})
        for call in msg.get('tool_calls') or []:
            if role != 'assistant' or call.get('type') != 'function':
                raise invalid('Only assistant function tool calls are supported.')
            f = call['function']
            result['input'].append({'type': 'function_call', 'call_id': call['id'], 'name': f['name'], 'arguments': f['arguments']})
    if not result['input']:
        raise invalid('messages must contain nonempty input.')
    limit = body.get('max_completion_tokens', body.get('max_tokens'))
    if limit is not None:
        result['max_output_tokens'] = limit
    if body.get('reasoning_effort'):
        result['reasoning'] = {'effort': body['reasoning_effort']}
    if 'response_format' in body:
        fmt = body['response_format']
        if fmt.get('type') == 'json_schema':
            fmt = {'type': 'json_schema', **fmt['json_schema']}
        result['text'] = {'format': fmt}
    if 'tools' in body:
        result['tools'] = []
        for tool in body['tools']:
            if tool.get('type') != 'function':
                raise invalid('Only function tools are supported.')
            result['tools'].append({'type': 'function', **tool['function']})
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if isinstance(choice, dict):
            if choice.get('type') != 'function':
                raise invalid('Only function tool_choice is supported.')
            choice = {'type': 'function', 'name': choice['function']['name']}
        result['tool_choice'] = choice
    return result


def usage(data):
    u = data or {}
    result = {'prompt_tokens': u.get('input_tokens', 0), 'completion_tokens': u.get('output_tokens', 0), 'total_tokens': u.get('total_tokens', 0)}
    for source, dest in [('input_tokens_details', 'prompt_tokens_details'), ('output_tokens_details', 'completion_tokens_details')]:
        if source in u:
            result[dest] = u[source]
    return result


def finish_reason(data, calls=False):
    if data.get('status') == 'incomplete':
        return 'length' if (data.get('incomplete_details') or {}).get('reason') == 'max_output_tokens' else 'content_filter'
    return 'tool_calls' if calls else 'stop'


def from_responses(data, model):
    if data.get('status') not in {'completed', 'incomplete'} or data.get('error'):
        raise APIError(502, 'Upstream Responses request failed.', 'upstream_response_failed')
    texts, calls, refusals = [], [], []
    for item in data.get('output', []):
        if item.get('type') == 'function_call':
            calls.append({'id': item['call_id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': item['arguments']}})
        elif item.get('type') == 'message':
            for part in item.get('content', []):
                if part.get('type') == 'output_text':
                    texts.append(part['text'])
                elif part.get('type') == 'refusal':
                    refusals.append(part['refusal'])
    msg = {'role': 'assistant', 'content': ''.join(texts) or None}
    if calls:
        msg['tool_calls'] = calls
    if refusals:
        msg['refusal'] = ''.join(refusals)
    return {'id': 'chatcmpl-' + uuid.uuid4().hex, 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'message': msg, 'finish_reason': finish_reason(data, bool(calls))}], 'usage': usage(data.get('usage'))}


def sse(data):
    return ('data: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode()


async def events(response):
    lines = []
    async for line in response.aiter_lines():
        if line == '':
            if lines:
                value = '\n'.join(lines)
                if value != '[DONE]':
                    yield json.loads(value)
                lines = []
        elif line.startswith('data:'):
            lines.append(line[5:].lstrip(' '))
    if lines and '\n'.join(lines) != '[DONE]':
        yield json.loads('\n'.join(lines))


async def stream_responses(response, model, include_usage=False):
    cid, created = 'chatcmpl-' + uuid.uuid4().hex, int(time.time())
    calls, terminal = {}, False
    def chunk(delta, finish=None, choices=True, u=None):
        out = {'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': model,
               'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}] if choices else []}
        if u is not None:
            out['usage'] = u
        return sse(out)
    yield chunk({'role': 'assistant', 'content': ''})
    async for event in events(response):
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            yield chunk({'content': event['delta']})
        elif kind == 'response.refusal.delta':
            yield chunk({'refusal': event['delta']})
        elif kind == 'response.output_item.added' and event['item'].get('type') == 'function_call':
            item = event['item']
            idx = len(calls)
            calls[event['output_index']] = idx
            yield chunk({'tool_calls': [{'index': idx, 'id': item['call_id'], 'type': 'function',
                                        'function': {'name': item['name'], 'arguments': item.get('arguments', '')}}]})
        elif kind == 'response.function_call_arguments.delta':
            idx = calls[event['output_index']]
            yield chunk({'tool_calls': [{'index': idx, 'function': {'arguments': event['delta']}}]})
        elif kind in {'response.completed', 'response.incomplete'}:
            data = event['response']
            yield chunk({}, finish_reason(data, bool(calls)))
            if include_usage:
                yield chunk({}, choices=False, u=usage(data.get('usage')))
            terminal = True
            break
        elif kind in {'error', 'response.failed', 'response.cancelled'}:
            raise APIError(502, 'Upstream Responses stream failed.', 'upstream_stream_failed')
    if not terminal:
        raise APIError(502, 'Upstream stream ended before completion.', 'upstream_stream_incomplete')
    yield b'data: [DONE]\n\n'
