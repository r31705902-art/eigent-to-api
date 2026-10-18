"""Small real tool-executing agent, not the upstream CAMEL runtime.

The upstream Eigent prompt remains active. No 'ignore the prompt' or guard bypass.
No shell, browser, subprocess, network tools, host filesystem or environment access.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import tempfile
import time
import uuid

from .errors import APIError

PROMPT = (Path(__file__).parent / 'vendor' / 'single_agent_prompt.txt').read_text()
PROMPT = PROMPT.replace('a focused autonomous assistant built on the\nCAMEL agent framework.', 'a focused autonomous assistant running in an independent bounded\nPython headless adapter, not the CAMEL runtime.')


def tool(name, description, properties, required=()):
    return {'type': 'function', 'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}


TOOLS = [
    tool('todo_write', 'Replace the task plan. New todos have id:null; preserve returned IDs on updates.',
         {'todos': {'type': 'array', 'maxItems': 20, 'items': {'type': 'object', 'properties': {
             'id': {'type': ['string', 'null']}, 'content': {'type': 'string'},
             'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']}},
             'required': ['id', 'content', 'status'], 'additionalProperties': False}}}, ['todos']),
    tool('step_update', 'Record a concise progress update.', {'message': {'type': 'string'}}, ['message']),
    tool('send_message_to_user', 'Record a progress message delivered in final response metadata, not live.',
         {'message_title': {'type': 'string'}, 'message_description': {'type': 'string'}}, ['message_title', 'message_description']),
    tool('list_files', 'List UTF-8 files in this request private scratch workspace.', {}),
    tool('read_file', 'Read a UTF-8 file from this request private scratch workspace.', {'file_path': {'type': 'string'}}, ['file_path']),
    tool('write_to_file', 'Write a UTF-8 scratch file. Temporary: not a downloadable final artifact.',
         {'file_path': {'type': 'string'}, 'content': {'type': 'string'}}, ['file_path', 'content']),
    tool('list_skills', 'List installed skills. This minimal deployment has no external skills.', {}),
]


class Workspace:
    """Model sees only this dispatcher. It cannot access app/token volumes or exec."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.todos = []
        self.progress = []
        self.executed = 0

    def path(self, value):
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError('Invalid workspace path')
        p = Path(value)
        p = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.is_relative_to(self.root) or p == self.root:
            raise ValueError('Path outside private workspace')
        return p

    def execute(self, name, args):
        self.executed += 1
        if self.executed > 32:
            raise APIError(422, 'Agent tool-call budget exhausted.', 'agent_tool_limit')
        try:
            if not isinstance(args, dict):
                raise ValueError('Tool arguments must be an object')
            schema = next((t['parameters'] for t in TOOLS if t['name'] == name), None)
            if schema is None:
                raise ValueError('Unknown or unavailable tool')
            if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
                raise ValueError('Missing or unknown tool arguments')
            if name == 'list_skills':
                return {'skills': []}
            if name == 'list_files':
                return {'files': [p.relative_to(self.root).as_posix() for p in sorted(self.root.rglob('*')) if p.is_file()]}
            if name == 'read_file':
                p = self.path(args['file_path'])
                if p.stat().st_size > 65536:
                    raise ValueError('File exceeds 64 KiB')
                return {'content': p.read_text(encoding='utf-8')}
            if name == 'write_to_file':
                p = self.path(args['file_path'])
                content = args['content']
                if not isinstance(content, str) or len(content.encode()) > 65536:
                    raise ValueError('Content must be UTF-8 text, at most 64 KiB')
                files = [f for f in self.root.rglob('*') if f.is_file()]
                if len(files) >= 32 and p not in files:
                    raise ValueError('At most 32 scratch files')
                size = sum(f.stat().st_size for f in files if f != p)
                if size + len(content.encode()) > 1048576:
                    raise ValueError('Workspace exceeds 1 MiB')
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                p.write_text(content, encoding='utf-8')
                p.chmod(0o600)
                return {'saved': p.relative_to(self.root).as_posix(), 'temporary': True}
            if name == 'todo_write':
                todos = args['todos']
                if not isinstance(todos, list) or len(todos) > 20 or not all(isinstance(t, dict) for t in todos):
                    raise ValueError('todos must be an array, at most 20 items')
                if sum(t.get('status') == 'in_progress' for t in todos) > 1:
                    raise ValueError('At most one todo can be in progress')
                existing = {t['id'] for t in self.todos}
                result = []
                for t in todos:
                    if t.get('status') not in {'pending', 'in_progress', 'completed'} or not isinstance(t.get('content'), str) or len(t['content']) > 1000:
                        raise ValueError('Invalid todo content or status')
                    tid = t.get('id')
                    if tid is not None and (not isinstance(tid, str) or tid not in existing):
                        raise ValueError('Unknown todo ID; use null for new todos')
                    result.append({'id': tid or uuid.uuid4().hex, 'content': t['content'], 'status': t['status']})
                if len({t['id'] for t in result}) != len(result):
                    raise ValueError('Duplicate todo ID')
                self.todos = result
                return {'todos': self.todos}
            if name in {'step_update', 'send_message_to_user'}:
                if any(not isinstance(v, str) or len(v) > 2000 for v in args.values()):
                    raise ValueError('Progress values must be bounded strings')
                self.progress.append({'tool': name, **args})
                return {'recorded': True}
        except (ValueError, OSError, TypeError, KeyError):
            # Do not expose exception text: a path can include a private directory.
            return {'error': 'Invalid/unavailable tool, path, arguments or workspace limit.'}


def input_messages(body):
    allowed = {'model', 'messages', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'reasoning_effort'}
    extra = set(body) - allowed
    if extra:
        raise APIError(400, 'Agent mode does not accept client tools/format controls: ' + ', '.join(sorted(extra)), 'unsupported_agent_parameter', 'invalid_request_error')
    output = []
    for msg in body['messages']:
        if set(msg) - {'role', 'content'} or msg.get('role') not in {'user', 'assistant', 'system', 'developer'} or not isinstance(msg.get('content'), str):
            raise APIError(400, 'Agent mode accepts text-only user/assistant/system/developer messages.', 'invalid_agent_messages', 'invalid_request_error')
        # Client system/developer instructions are task context, not permission to
        # replace or disable the server-owned agent policy.
        role = msg['role'] if msg['role'] in {'user', 'assistant'} else 'user'
        content = msg['content'] if role == msg['role'] else '[Client task preferences]\n' + msg['content']
        output.append({'role': role, 'content': content})
    return output


class EigentAgent:
    def __init__(self, settings):
        self.settings = settings

    async def run(self, body, model, invoke):
        history = input_messages(body)
        if len(json.dumps(history).encode()) > 1048576:
            raise APIError(413, 'Agent initial context exceeds 1 MiB.', 'agent_context_limit')
        total = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
        with tempfile.TemporaryDirectory(prefix='eigent-agent-') as root:
            workspace = Workspace(root)
            instructions = PROMPT.format(platform_system=platform.system(), platform_machine=platform.machine(),
                                         working_directory=root, now_str=datetime.now(timezone.utc).isoformat())
            instructions += '\nOnly the supplied managed scratch-file and planning tools are available. No browser, terminal, network, MCP, delegation or external skills are installed. Scratch files are deleted at the end of this request; include deliverables in the final answer and do not claim files are downloadable. Never read credentials or change the runtime policy.'
            project_id = uuid.uuid4().hex
            for turn in range(self.settings.agent_max_steps):
                payload = {'model': body['model'], 'instructions': instructions, 'input': history,
                           'tools': TOOLS, 'parallel_tool_calls': False, 'store': False, 'user': project_id,
                           'include': ['reasoning.encrypted_content'],
                           'max_output_tokens': min(body.get('max_completion_tokens', body.get('max_tokens', self.settings.agent_max_output)), self.settings.agent_max_output)}
                for name in ('temperature', 'top_p'):
                    if name in body:
                        payload[name] = body[name]
                if 'reasoning_effort' in body:
                    payload['reasoning'] = {'effort': body['reasoning_effort']}
                response = await invoke(model, payload)
                if not isinstance(response, dict) or response.get('status') not in {'completed', 'incomplete'} or not isinstance(response.get('output'), list) or response.get('error'):
                    raise APIError(502, 'Agent received an invalid/failed Responses result.', 'invalid_agent_response')
                usage = response.get('usage') or {}
                for key in total:
                    total[key] += int(usage.get(key, 0) or 0)
                if response['status'] == 'incomplete':
                    result = self.result(response, body['model'], total, turn + 1, workspace.executed, workspace.progress)
                    return result
                calls = [x for x in response['output'] if x.get('type') == 'function_call']
                if not calls:
                    return self.result(response, body['model'], total, turn + 1, workspace.executed, workspace.progress)
                if not all(isinstance(x.get('call_id'), str) and isinstance(x.get('name'), str) for x in calls):
                    raise APIError(502, 'Agent received a malformed tool call.', 'invalid_agent_tool_call')
                history.extend(response['output'])
                for call in calls:
                    try:
                        args = json.loads(call.get('arguments', '{}'))
                    except (ValueError, TypeError):
                        args = None
                    result = workspace.execute(call['name'], args)
                    history.append({'type': 'function_call_output', 'call_id': call['call_id'], 'output': json.dumps(result, ensure_ascii=False)})
                if len(json.dumps(history).encode()) > 1048576:
                    raise APIError(422, 'Agent context budget exhausted.', 'agent_context_limit')
            raise APIError(422, 'Agent step budget exhausted before a final answer.', 'agent_step_limit')

    @staticmethod
    def result(response, model, total, steps, calls, progress=()):
        from .bridge import from_responses
        # Never hand internal agent tool calls to an OpenAI client as external calls.
        response = {**response, 'usage': total, 'output': [x for x in response['output'] if x.get('type') != 'function_call']}
        result = from_responses(response, model)
        result['eigent_agent'] = {'runtime': 'bounded-headless-adapter', 'steps': steps, 'tool_calls': calls, 'scratch_files': 'temporary', 'progress': list(progress)}
        return result


def agent_to_chat(body, model):
    """Translate the internal agent protocol for models advertising native chat."""
    messages = [{'role': 'system', 'content': body['instructions']}]
    for item in body['input']:
        kind = item.get('type')
        if kind == 'function_call':
            if not messages or messages[-1]['role'] != 'assistant':
                messages.append({'role': 'assistant', 'content': None})
            messages[-1].setdefault('tool_calls', []).append({'id': item['call_id'], 'type': 'function',
                'function': {'name': item['name'], 'arguments': item['arguments']}})
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': item['output']})
        elif kind in {None, 'message'} and 'role' in item:
            content = item.get('content')
            if isinstance(content, list):
                content = ''.join(p.get('text', '') for p in content if p.get('type') in {'output_text', 'input_text'})
            messages.append({'role': item['role'], 'content': content})
        # Responses reasoning items are not chat message content.
    result = {'model': body['model'], 'messages': messages, 'stream': False, 'user': body['user'],
              'tools': [{'type': 'function', 'function': {k: t[k] for k in ('name', 'description', 'parameters')}} for t in body['tools']],
              'parallel_tool_calls': False}
    limit_name = 'max_completion_tokens' if model.get('model_type', '').startswith(('gpt-5', 'gpt-6', 'o1', 'o3', 'o4')) else 'max_tokens'
    result[limit_name] = body['max_output_tokens']
    for name in ('temperature', 'top_p'):
        if name in body:
            result[name] = body[name]
    if body.get('reasoning'):
        result['reasoning_effort'] = body['reasoning']['effort']
    return result


def chat_to_agent_response(data):
    if not isinstance(data, dict) or data.get('error') or not data.get('choices'):
        raise APIError(502, 'Invalid agent Chat Completions response.', 'invalid_agent_response')
    try:
        choice = data['choices'][0]
        msg = choice['message']
        output = []
        parts = []
        if msg.get('content'):
            if not isinstance(msg['content'], str):
                raise ValueError('Text content required')
            parts.append({'type': 'output_text', 'text': msg['content']})
        if msg.get('refusal'):
            parts.append({'type': 'refusal', 'refusal': msg['refusal']})
        if parts:
            output.append({'type': 'message', 'role': 'assistant', 'content': parts})
        for call in msg.get('tool_calls') or []:
            if call['type'] != 'function':
                raise ValueError('Function call required')
            output.append({'type': 'function_call', 'call_id': call['id'], 'name': call['function']['name'], 'arguments': call['function']['arguments']})
        usage = data.get('usage') or {}
        result = {'status': 'incomplete' if choice.get('finish_reason') in {'length', 'content_filter'} else 'completed',
                  'output': output, 'usage': {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': usage.get('completion_tokens', 0), 'total_tokens': usage.get('total_tokens', 0)}}
        if result['status'] == 'incomplete':
            result['incomplete_details'] = {'reason': 'max_output_tokens' if choice['finish_reason'] == 'length' else 'content_filter'}
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        raise APIError(502, 'Malformed agent Chat Completions result.', 'invalid_agent_response')
