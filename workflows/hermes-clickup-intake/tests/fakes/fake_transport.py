"""Minimal scripted ClickUpTransport fake. The full fake ClickUp is S1's fake_clickup.py."""

from clickup_intake.contracts import Outcome, TransportResult


class FakeTransport:
    """Returns scripted results in order; DEFINITE_FAIL once the script is exhausted."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def request(self, method, path, params=None, body=None, multipart=None):
        self.calls.append((method, path, params, body, multipart))
        if self.results:
            return self.results.pop(0)
        return TransportResult(Outcome.DEFINITE_FAIL)
