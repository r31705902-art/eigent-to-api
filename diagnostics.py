"""Bounded, authenticated diagnostics; never persist raw upstream response bodies."""
from collections import deque
from datetime import datetime, timezone
import json
import re
import uuid


class Diagnostics:
    def __init__(self):
        self.entries = deque(maxlen=20)

    def add(self, *, status, model, route, content_type, raw, secrets=()):
        protected = [str(s) for s in secrets if s and len(str(s)) >= 4]
        def clean(text):
            text = str(text)
            for value in sorted(protected, key=len, reverse=True):
                text = text.replace(value, '[REDACTED]')
            text = re.sub(r'https?://[^\s"<>]+', '[URL REDACTED]', text)
            text = re.sub(r'(?i)(bearer\s+|(?:api[_-]?key|token|secret|authorization)\s*[=:]\s*)[^\s,;"<>]+', r'\1[REDACTED]', text)
            text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[JWT REDACTED]', text)
            text = re.sub(r'\b(?:sk-|pck_)[A-Za-z0-9_-]{8,}\b', '[KEY REDACTED]', text)
            return text[:1500]
        message, upstream_code, upstream_type = '', None, None
        try:
            data = json.loads(raw)
            error = data.get('error', data) if isinstance(data, dict) else {}
            if isinstance(error, dict):
                message = error.get('message') or error.get('detail') or ''
                upstream_code = error.get('code')
                upstream_type = error.get('type')
            elif isinstance(error, str):
                message = error
        except (ValueError, TypeError):
            if 'html' not in content_type.lower():
                message = raw.decode('utf-8', errors='replace')[:4000]
        message = clean(message) if isinstance(message, str) else ''
        text = message.lower()
        reason = 'unknown'
        if any(x in text for x in ('prompt guard', 'prompt_guard', 'system prompt', 'agent prompt')):
            reason = 'prompt_policy'
        elif any(x in text for x in ('quota', 'credits', 'budget', 'rate limit')):
            reason = 'quota_or_rate_limit'
        elif any(x in text for x in ('plan', 'subscription', 'permission', 'not allowed', 'access denied')):
            reason = 'access_policy'
        elif 'html' in content_type.lower():
            reason = 'html_rejection_or_waf'
        entry = {'id': uuid.uuid4().hex, 'time': datetime.now(timezone.utc).isoformat(),
                 'upstream_status': status, 'model': clean(model), 'transport_route': route,
                 'reason_hint': reason, 'upstream_message': message or '(no safe structured message)',
                 'upstream_code': clean(upstream_code) if isinstance(upstream_code, (str, int)) else None,
                 'upstream_type': clean(upstream_type) if isinstance(upstream_type, str) else None}
        self.entries.appendleft(entry)
        return entry['id']

    def list(self):
        return list(self.entries)
