import json
from app.diagnostics import Diagnostics


def test_no_false_prompt_guard_inference():
    d = Diagnostics()
    for raw, content_type, reason in [(b'{"error":{"message":"Forbidden"}}', 'application/json', 'unknown'),
                                      (b'<html>Blocked</html>', 'text/html', 'html_rejection_or_waf'),
                                      (b'{"error":{"message":"Quota exceeded"}}', 'application/json', 'quota_or_rate_limit')]:
        d.add(status=403, model='m', route='responses', content_type=content_type, raw=raw)
        assert d.list()[0]['reason_hint'] == reason


def test_bounded_diagnostics_and_credential_patterns():
    d = Diagnostics()
    for _ in range(30):
        d.add(status=403, model='m', route='responses', content_type='text/plain', raw=b'Authorization=Bearer abc; api_key=secret; sk-abcdefghijklmnop; https://example.com/private?a=1')
    assert len(d.list()) == 20
    text = json.dumps(d.list())
    assert 'abcdefghijklmnop' not in text and 'example.com' not in text
    assert 'api_key=secret' not in text
