class APIError(Exception):
    def __init__(self, status: int, message: str, code: str = 'upstream_error', kind: str = 'api_error', diagnostic_id: str | None = None):
        self.diagnostic_id = diagnostic_id
        self.status, self.message, self.code, self.kind = status, message, code, kind
        super().__init__(message)

    def body(self):
        body = {'error': {'message': self.message, 'type': self.kind, 'param': None, 'code': self.code}}
        if self.diagnostic_id:
            body['error']['diagnostic_id'] = self.diagnostic_id
        return body
