from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit


def https_url(value: str, label: str, allow_local: bool = False) -> str:
    p = urlsplit(value)
    local = p.hostname in {'localhost', '127.0.0.1', '::1'}
    if (p.scheme != 'https' and not (allow_local and local and p.scheme == 'http')) or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError(f'{label} must be an HTTPS URL without embedded credentials or fragment')
    return value.rstrip('/')


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv('API_KEY', ''))
    public_url: str = field(default_factory=lambda: os.getenv('PUBLIC_URL', '') or (
        'https://' + os.environ['RAILWAY_PUBLIC_DOMAIN'] if os.getenv('RAILWAY_PUBLIC_DOMAIN') else ''))
    api_base: str = field(default_factory=lambda: os.getenv('EIGENT_API_BASE', 'https://dev.eigent.ai'))
    auth_url: str = field(default_factory=lambda: os.getenv('EIGENT_AUTH_URL', ''))
    auth_url_bearer: str = field(default_factory=lambda: os.getenv('EIGENT_AUTH_URL_BEARER', ''))
    auth_json: str = field(default_factory=lambda: os.getenv('EIGENT_AUTH_JSON', ''))
    token: str = field(default_factory=lambda: os.getenv('EIGENT_TOKEN', ''))
    auth_file: str = field(default_factory=lambda: os.getenv('AUTH_FILE', '/data/session/auth.json'))
    timeout: float = field(default_factory=lambda: float(os.getenv('UPSTREAM_TIMEOUT', '300')))
    max_body: int = field(default_factory=lambda: int(os.getenv('MAX_REQUEST_BYTES', '2097152')))
    max_concurrency: int = field(default_factory=lambda: int(os.getenv('MAX_CONCURRENCY', '8')))
    key_ttl: float = field(default_factory=lambda: float(os.getenv('KEY_CACHE_SECONDS', '60')))

    def validate(self):
        if len(self.api_key) < 24:
            raise ValueError('Set API_KEY to a random secret of at least 24 characters')
        self.api_base = https_url(self.api_base, 'EIGENT_API_BASE')
        if self.auth_url:
            https_url(self.auth_url, 'EIGENT_AUTH_URL')
        if self.public_url:
            self.public_url = https_url(self.public_url, 'PUBLIC_URL')
            if urlsplit(self.public_url).path or urlsplit(self.public_url).query:
                raise ValueError('PUBLIC_URL must be an origin, without a path or query')
        if self.max_body < 1024 or self.max_concurrency < 1 or self.timeout <= 0 or self.key_ttl < 0:
            raise ValueError('Invalid resource limit')
