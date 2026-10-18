class APIError(Exception):
    def __init__(self, status: int, message: str, code: str = 'upstream_error', kind: str = 'api_error'):
        self.status, self.message, self.code, self.kind = status, message, code, kind
        super().__init__(message)

    def body(self):
        return {'error': {'message': self.message, 'type': self.kind, 'param': None, 'code': self.code}}
