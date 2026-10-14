"""Minimal in-memory KanbanPort fake: records calls, idempotent create, scripted overrides."""

from clickup_intake.contracts import KanbanResult, Outcome


class FakeKanban:
    def __init__(self):
        self.calls = []
        self.cards = {}
        self.comments = {}
        self._keys = {}
        self._overrides = {}

    def respond(self, method, result):
        """Make the next call of ``method`` return ``result`` instead of the default."""
        self._overrides.setdefault(method, []).append(result)

    def _call(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        queued = self._overrides.get(method)
        return queued.pop(0) if queued else None

    def _status(self, card_id, status):
        if card_id not in self.cards:
            return KanbanResult(Outcome.DEFINITE_FAIL)
        self.cards[card_id]["status"] = status
        return KanbanResult(Outcome.OK, {"id": card_id, "status": status})

    def create(self, title, *, body_file, idempotency_key, assignee=None, skills=(), initial_status="blocked"):
        r = self._call("create", title, body_file=body_file, idempotency_key=idempotency_key, assignee=assignee,
                       skills=tuple(skills), initial_status=initial_status)
        if r:
            return r
        if idempotency_key not in self._keys:
            card_id = "t_fake%04d" % (len(self.cards) + 1)
            self._keys[idempotency_key] = card_id
            self.cards[card_id] = {"id": card_id, "title": title, "assignee": assignee, "status": initial_status}
            self.comments[card_id] = []
        return KanbanResult(Outcome.OK, {"id": self._keys[idempotency_key]})

    def comment(self, card_id, body):
        r = self._call("comment", card_id, body)
        if r:
            return r
        if card_id not in self.cards:
            return KanbanResult(Outcome.DEFINITE_FAIL)
        self.comments[card_id].append({"author": "clickup-intake", "body": body})
        return KanbanResult(Outcome.OK, {})

    def attach(self, card_id, path, name):
        return self._call("attach", card_id, path, name) or KanbanResult(Outcome.OK, {})

    def attachments(self, card_id):
        return self._call("attachments", card_id) or KanbanResult(Outcome.OK, [])

    def show(self, card_id):
        r = self._call("show", card_id)
        if r:
            return r
        if card_id not in self.cards:
            return KanbanResult(Outcome.DEFINITE_FAIL)
        return KanbanResult(Outcome.OK, {"task": dict(self.cards[card_id]), "parents": [], "children": [],
                                         "comments": list(self.comments[card_id]), "events": [], "runs": []})

    def unblock(self, card_id):
        return self._call("unblock", card_id) or self._status(card_id, "ready")

    def complete(self, card_id, summary, metadata=None):
        return self._call("complete", card_id, summary, metadata) or self._status(card_id, "done")

    def archive(self, card_id):
        return self._call("archive", card_id) or self._status(card_id, "archived")

    def subscribe(self, card_id):
        return self._call("subscribe", card_id) or KanbanResult(Outcome.OK, {})
