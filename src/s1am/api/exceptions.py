import json


class SentinelOneError(RuntimeError):
    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    def __str__(self):
        base = super().__str__()
        if self.payload is None:
            return base
        if isinstance(self.payload, (dict, list)):
            detail = json.dumps(self.payload, separators=(",", ":"))
        else:
            detail = str(self.payload)
        return f"{base} — {detail}"
