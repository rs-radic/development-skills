"""Stateful in-memory fake of the ClickUp API v2 subset used by the glue (S1, plan r5 §1.1, §4, §10).

Implements ``ClickUpTransport.request``. Supports scripted faults (including writes that commit
on the server only later), rate-limit headers, and a call log for request-count assertions.
"""

import itertools
import re

from clickup_intake.contracts import Outcome, TransportResult

BOT = 900000001
LIST_ID = "4444444444444444"

CORRECT_STATUSES = (
    ("reviewing feature", "open"),
    ("business requirements", "unstarted"),
    ("technical requirements", "unstarted"),
    ("waiting for approval", "unstarted"),
    ("implementing", "custom"),
    ("implemented", "done"),
    ("ready for deployment", "done"),
    ("deployed", "closed"),
)


class FakeClickUp:
    def __init__(self, clock, list_id=LIST_ID, statuses=CORRECT_STATUSES, page_size=100, comment_page=25):
        self.clock = clock
        self.list_id = list_id
        self.statuses = {list_id: [list(p) for p in statuses]}
        self.page_size = page_size
        self.comment_page = comment_page
        self.tasks = {}          # id -> task dict (attachments included)
        self.comments = {}       # task id -> list of root comment dicts
        self.replies = {}        # root id -> list of reply dicts
        self.calls = []          # (method, path, params)
        self.bodies = []         # JSON body of each call (None for GET/multipart)
        self.faults = []         # scripted one-shot faults
        self.pending = []        # (commit_at_ms, fn) writes that land later
        self.ignore_filters = False   # serve every task (other lists, archived, subtasks, any status)
        self.rate_remaining = None   # fixed X-RateLimit-Remaining override
        self.on_list_page = None     # hook(page_number) called before serving a list page
        self._ids = itertools.count(1000)

    # ------------------------------------------------------------------ fixtures
    def _id(self, prefix=""):
        return f"{prefix}{next(self._ids)}"

    def add_task(self, task_id=None, name="Request", status="reviewing feature", creator=900000101,
                 created_ms=None, description="", list_id=None, parent=None, archived=False):
        task_id = task_id or self._id("t")
        now = self.clock()
        self.tasks[task_id] = {
            "id": task_id, "name": name, "description": description, "markdown_description": description,
            "status": {"status": status, "type": self._type_of(status)},
            "date_created": str(created_ms if created_ms is not None else now), "date_updated": str(now),
            "creator": {"id": creator}, "list": {"id": list_id or self.list_id}, "parent": parent,
            "archived": archived, "attachments": [],
        }
        self.comments[task_id] = []
        return task_id

    def _type_of(self, status):
        for name, typ in self.statuses.get(self.list_id, []):
            if name == status:
                return typ
        return "custom"

    def _touch(self, task_id):
        self.tasks[task_id]["date_updated"] = str(self.clock())

    def set_status(self, task_id, status):
        self.tasks[task_id]["status"] = {"status": status, "type": self._type_of(status)}
        self._touch(task_id)

    def set_description(self, task_id, text):
        self.tasks[task_id]["description"] = text
        self.tasks[task_id]["markdown_description"] = text
        self._touch(task_id)

    def set_field(self, task_id, key, value):
        self.tasks[task_id][key] = value  # e.g. custom fields / assignees: not activity
        self._touch(task_id)

    def archive(self, task_id):
        self.tasks[task_id]["archived"] = True

    def move(self, task_id, list_id):
        self.tasks[task_id]["list"] = {"id": list_id}

    def delete_task(self, task_id):
        del self.tasks[task_id]

    @staticmethod
    def parts_for(text, mention=None):
        parts = []
        if mention is not None:
            parts.append({"type": "tag", "text": "@Developer AI", "user": {"id": mention}})
            text_full = "@Developer AI" + text
        else:
            text_full = text
        parts.append({"text": text})
        return parts, text_full

    def add_comment(self, task_id, user, text, mention=None, reply_to=None, parts=None, date_ms=None):
        if parts is None:
            parts, comment_text = self.parts_for(text, mention)
        else:
            comment_text = text
        c = {"id": self._id("c"), "comment": parts, "comment_text": comment_text, "user": {"id": user},
             "date": str(date_ms if date_ms is not None else self.clock()), "reply_count": 0, "reactions": []}
        if reply_to is None:
            self.comments[task_id].append(c)
            self.replies[c["id"]] = []
        else:
            self.replies[reply_to].append(c)
            root = self._find(reply_to)
            root["reply_count"] = len(self.replies[reply_to])
        self._touch(task_id)
        return c["id"]

    def _find(self, comment_id):
        for roots in self.comments.values():
            for c in roots:
                if c["id"] == comment_id:
                    return c
        for reps in self.replies.values():
            for c in reps:
                if c["id"] == comment_id:
                    return c
        raise KeyError(comment_id)

    def edit_comment(self, comment_id, text, mention=None):
        c = self._find(comment_id)
        c["comment"], c["comment_text"] = self.parts_for(text, mention)

    def delete_comment(self, comment_id):
        for roots in self.comments.values():
            roots[:] = [c for c in roots if c["id"] != comment_id]
        for root_id, reps in self.replies.items():
            before = len(reps)
            reps[:] = [c for c in reps if c["id"] != comment_id]
            if len(reps) != before:
                self._find(root_id)["reply_count"] = len(reps)

    def add_attachment(self, task_id, user, title="file.txt", size=10, url=None, hidden=False, deleted=False):
        a = {"id": self._id("a"), "title": title, "size": size, "user": {"id": user}, "deleted": deleted,
             "hidden": hidden, "url": url or f"https://t1.p.clickup-attachments.com/{title}",
             "date": str(self.clock())}
        self.tasks[task_id]["attachments"].append(a)
        self._touch(task_id)
        return a["id"]

    def remove_attachment(self, task_id, att_id):
        self.tasks[task_id]["attachments"] = [a for a in self.tasks[task_id]["attachments"] if a["id"] != att_id]

    # ------------------------------------------------------------------ faults
    def inject(self, method, path_pattern, outcome, status=None, commit=False, commit_at_ms=None, headers=None,
               json=None):
        """One-shot fault for the next matching request. ``commit`` applies the write anyway
        (immediately, or once the fake clock reaches ``commit_at_ms``)."""
        self.faults.append(dict(method=method, pattern=re.compile(path_pattern), outcome=outcome, status=status,
                                commit=commit, commit_at_ms=commit_at_ms, headers=headers or {}, json=json))

    def _apply_pending(self):
        now = self.clock()
        due = [p for p in self.pending if p[0] <= now]
        self.pending = [p for p in self.pending if p[0] > now]
        for _, fn in due:
            fn()

    def count(self, method=None, pattern=None):
        rx = re.compile(pattern) if pattern else None
        return sum(1 for m, p, _ in self.calls if (method is None or m == method) and (rx is None or rx.search(p)))

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    # ------------------------------------------------------------------ transport
    def request(self, method, path, params=None, body=None, multipart=None):
        self._apply_pending()
        self.calls.append((method, path, dict(params or {})))
        self.bodies.append(body)
        headers = {"x-ratelimit-limit": "100",
                   "x-ratelimit-remaining": str(self.rate_remaining if self.rate_remaining is not None else 99),
                   "x-ratelimit-reset": str(self.clock() // 1000 + 60)}
        for i, f in enumerate(self.faults):
            if f["method"] == method and f["pattern"].search(path):
                del self.faults[i]
                if f["commit"]:
                    fn = lambda: self._route(method, path, params or {}, body, multipart)  # noqa: E731
                    if f["commit_at_ms"] is None:
                        fn()
                    else:
                        self.pending.append((f["commit_at_ms"], fn))
                return TransportResult(f["outcome"], f["status"], f["json"], {**headers, **f["headers"]})
        status, payload = self._route(method, path, params or {}, body, multipart)
        if status >= 400:
            return TransportResult(Outcome.DEFINITE_FAIL, status, payload, headers)
        return TransportResult(Outcome.OK, status, payload, headers)

    def _route(self, method, path, params, body, multipart):
        m = re.fullmatch(r"/v2/list/([^/]+)", path)
        if m and method == "GET":
            if m.group(1) not in self.statuses:
                return 404, {"err": "List not found"}
            return 200, {"id": m.group(1), "override_statuses": True,
                         "statuses": [{"status": n, "type": t, "orderindex": i, "color": "#000"}
                                      for i, (n, t) in enumerate(self.statuses[m.group(1)])]}
        m = re.fullmatch(r"/v2/list/([^/]+)/task", path)
        if m and method == "GET":
            return 200, self._list_tasks(m.group(1), params)
        m = re.fullmatch(r"/v2/task/([^/]+)", path)
        if m:
            task = self.tasks.get(m.group(1))
            if task is None:
                return 404, {"err": "Task not found", "ECODE": "ITEM_013"}
            if method == "GET":
                return 200, _copy(task)
            if method == "PUT":
                self.set_status(task["id"], body["status"])
                return 200, _copy(self.tasks[task["id"]])
        m = re.fullmatch(r"/v2/task/([^/]+)/comment", path)
        if m:
            tid = m.group(1)
            if tid not in self.tasks:
                return 404, {"err": "Task not found"}
            if method == "GET":
                return 200, {"comments": self._comment_page(tid, params)}
            if method == "POST":
                cid = self.add_comment(tid, BOT, body["comment_text"])
                return 200, {"id": cid, "hist_id": "h" + cid, "date": int(self.clock())}
        m = re.fullmatch(r"/v2/comment/([^/]+)/reply", path)
        if m:
            root = m.group(1)
            if root not in self.replies:
                return 404, {"err": "Comment not found"}
            if method == "GET":
                return 200, {"comments": [_copy(c) for c in self.replies[root]]}
            if method == "POST":
                tid = next(t for t, roots in self.comments.items() if any(c["id"] == root for c in roots))
                self.add_comment(tid, BOT, body["comment_text"], reply_to=root)
                return 200, {}
        m = re.fullmatch(r"/v2/task/([^/]+)/attachment", path)
        if m and method == "POST":
            aid = self.add_attachment(m.group(1), BOT, title=multipart["filename"], size=len(multipart["content"]))
            return 200, {"id": aid, "title": multipart["filename"]}
        return 404, {"err": "Route not found"}

    def _list_tasks(self, list_id, params):
        if self.on_list_page:
            self.on_list_page(int(params.get("page", 0)))
        wanted = params.get("statuses[]")
        include_closed = str(params.get("include_closed", "false")).lower() == "true"
        rows = []
        for t in self.tasks.values():
            if self.ignore_filters:
                rows.append(t)
                continue
            if t["list"]["id"] != list_id:
                continue
            if t["archived"] and str(params.get("archived", "false")).lower() != "true":
                continue
            if t["parent"] and str(params.get("subtasks", "false")).lower() != "true":
                continue
            if wanted is not None and t["status"]["status"] not in wanted:
                continue
            if wanted is None and not include_closed and t["status"]["type"] == "closed":
                continue
            rows.append(t)
        rows.sort(key=lambda t: (int(t["date_created"]), t["id"]))
        page = int(params.get("page", 0))
        chunk = rows[page * self.page_size:(page + 1) * self.page_size]
        out = []
        for t in chunk:
            item = _copy(t)
            item.pop("attachments", None)
            item.pop("markdown_description", None)
            out.append(item)
        return {"tasks": out, "last_page": (page + 1) * self.page_size >= len(rows)}

    def _comment_page(self, task_id, params):
        ordered = sorted(self.comments[task_id], key=lambda c: (int(c["date"]), c["id"]), reverse=True)
        if "start" in params:
            key = (int(params["start"]), params["start_id"])
            ordered = [c for c in ordered if (int(c["date"]), c["id"]) < key]
        return [_copy(c) for c in ordered[:self.comment_page]]


def _copy(obj):
    import copy
    return copy.deepcopy(obj)
