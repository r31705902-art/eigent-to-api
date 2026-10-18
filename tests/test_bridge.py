import httpx
import pytest

from app.bridge import events, finish_reason, from_responses, stream_responses, to_responses


def test_tools_images_and_json_schema():
    body = {'model': 'm', 'messages': [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'see'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,abc', 'detail': 'low'}}]},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"x"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'found'}],
        'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}, 'strict': True}}],
        'tool_choice': {'type': 'function', 'function': {'name': 'lookup'}},
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'answer', 'schema': {'type': 'object'}, 'strict': True}}}
    out = to_responses(body)
    assert out['input'][0]['content'][1]['type'] == 'input_image'
    assert out['input'][1]['type'] == 'function_call'
    assert out['input'][2]['type'] == 'function_call_output'
    assert out['tools'][0]['name'] == 'lookup'
    assert out['tool_choice'] == {'type': 'function', 'name': 'lookup'}
    assert out['text']['format']['type'] == 'json_schema'


def test_tool_response_and_refusal():
    data = {'status': 'completed', 'output': [
        {'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup', 'arguments': '{}'},
        {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No.'}]}]}
    out = from_responses(data, 'm')
    assert out['choices'][0]['finish_reason'] == 'tool_calls'
    assert out['choices'][0]['message']['refusal'] == 'No.'
    assert finish_reason({'status': 'incomplete', 'incomplete_details': {'reason': 'content_filter'}}) == 'content_filter'


@pytest.mark.asyncio
async def test_tool_stream_deltas():
    import json
    es = [
        {'type': 'response.output_item.added', 'output_index': 2, 'item': {'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup', 'arguments': ''}},
        {'type': 'response.function_call_arguments.delta', 'output_index': 2, 'delta': '{"q":'},
        {'type': 'response.function_call_arguments.delta', 'output_index': 2, 'delta': '"x"}'},
        {'type': 'response.completed', 'response': {'status': 'completed'}}]
    response = httpx.Response(200, content=''.join('data: ' + json.dumps(e) + '\n\n' for e in es).encode())
    chunks = [x async for x in stream_responses(response, 'm')]
    data = [json.loads(c.decode()[6:]) for c in chunks[:-1]]
    calls = [d['choices'][0]['delta']['tool_calls'][0] for d in data if d['choices'][0]['delta'].get('tool_calls')]
    assert calls[0]['id'] == 'call_1'
    assert ''.join(c['function'].get('arguments', '') for c in calls) == '{"q":"x"}'
    assert all(c['index'] == 0 for c in calls)
    assert data[-1]['choices'][0]['finish_reason'] == 'tool_calls'
