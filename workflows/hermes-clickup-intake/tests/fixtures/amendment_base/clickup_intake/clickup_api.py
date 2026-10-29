"""ClickUp HTTP transport, token, request budget/health guard and attachment staging (S1, plan r5 §4.5, §8.2, §9, §10.1).

The token is decrypted per run from the DPAPI hex blob, held in memory only and sent only to
``https://api.clickup.com``. Redirects are never followed with the token. Attachment downloads never
carry the token.
"""

import ctypes
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .contracts import Outcome, TransportResult

API_HOST = "api.clickup.com"
API_ROOT = "https://api.clickup.com/api"
_PATH_RE = re.compile(r"^/v2/[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$")


# ----------------------------------------------------------------------------------------- token
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def unprotect_hex(blob_hex):
    """Decrypt a DPAPI blob given as hex text (current-user scope) and return the secret, in memory only.

    The installed credential stores the plaintext as UTF-16LE, so it is decoded as UTF-16LE.
    """
    raw = bytes.fromhex(blob_hex.strip())
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                                    ctypes.byref(blob_out)):
        raise OSError("DPAPI decryption failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-16-le").strip()
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def load_token(path):
    """Read the credential file (read-only) and decrypt it; the token never touches disk or logs."""
    with open(path, "r", encoding="ascii") as f:
        return unprotect_hex(f.read())


# ------------------------------------------------------------------------------------- transport
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would copy the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_api_opener():
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


_PRE_SEND = (socket.gaierror, ConnectionRefusedError, ssl.SSLError, TimeoutError)


class HttpTransport:
    """``ClickUpTransport`` over HTTPS to api.clickup.com only. Never raises; never exposes the token."""

    def __init__(self, token, timeout=30.0, opener=None):
        self.__token = token
        self._timeout = timeout
        self._opener = opener or build_api_opener()

    def __repr__(self):
        return "HttpTransport(api.clickup.com)"

    def request(self, method, path, params=None, body=None, multipart=None):
        if not (isinstance(path, str) and _PATH_RE.match(path)) or (body is not None and multipart is not None):
            return TransportResult(Outcome.DEFINITE_FAIL)
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != API_HOST:
            return TransportResult(Outcome.DEFINITE_FAIL)
        headers = {"Authorization": self.__token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif multipart is not None:
            data, ctype = _multipart(multipart)
            headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                status, raw, hdrs = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            try:
                payload = json.loads(e.read() or b"null")
            except Exception:
                payload = None
            return TransportResult(classify_http_error(e.code), e.code, payload, hdrs)
        except urllib.error.URLError as e:
            pre = isinstance(e.reason, _PRE_SEND)
            return TransportResult(Outcome.DEFINITE_FAIL if pre else Outcome.AMBIGUOUS)
        except Exception:
            return TransportResult(Outcome.AMBIGUOUS)  # read timeout / reset after send
        hdrs = {k.lower(): v for k, v in hdrs.items()}
        try:
            payload = json.loads(raw)
        except Exception:
            return TransportResult(Outcome.AMBIGUOUS, status, None, hdrs)
        return TransportResult(Outcome.OK, status, payload, hdrs)


def classify_http_error(code):
    """§10.1 outcome class of an HTTP error status."""
    if code in (401, 403):
        return Outcome.AUTH_FAIL
    if code == 429:
        return Outcome.RATE_LIMITED
    if code in (408, 409) or code >= 500 or code < 400:
        return Outcome.AMBIGUOUS
    return Outcome.DEFINITE_FAIL


def _multipart(mp):
    boundary = uuid.uuid4().hex
    name = re.sub(r'["\r\n\\]', "_", mp["filename"])
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    return head + mp["content"] + f"\r\n--{boundary}--\r\n".encode("ascii"), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------------------- budget and health
GET_BACKOFF_MIN = (1, 2, 5, 10, 30)
AUTH_RETRY_MS = 30 * 60_000
FAIL_ALERT_MS = 15 * 60_000
BUDGET_ALERT_MS = 30 * 60_000
STATE_KEY = "clickup_api_state"


class ApiGuard:
    """Per-scan request budget (§4.5) plus persisted health: 429 reset, GET 5xx/timeout backoff, 401/403 stop.

    Alerts are emitted once per incident with one recovery line. State lives in ``meta`` under
    ``clickup_api_state`` so it survives cron runs.
    """

    def __init__(self, store, config, now_ms):
        self.store, self.now = store, now_ms
        b = config.budgets
        self.scan_cap, self.reserve, self.task_cap = b["scan_requests"], b["rate_limit_reserve"], b["per_task_requests"]
        row = store.conn.execute("SELECT value FROM meta WHERE key=?", (STATE_KEY,)).fetchone()
        self.state = json.loads(row[0]) if row else {}
        self.used, self.per_task, self.stop = 0, {}, None
        self.alerts = []

    def transport(self, inner, task=None):
        return GuardedTransport(inner, self, task)

    def _save(self):
        with self.store.transaction() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (STATE_KEY, json.dumps(self.state)))

    @property
    def exhausted(self):
        """No further request can be sent this scan (any task)."""
        return self.gate(None) is not None

    def gate(self, task):
        """None if a request may be sent now, else the not-sent outcome."""
        s, now = self.state, self.now
        if s.get("auth_failed") and (now < s.get("auth_retry_ms", 0) or self.stop == "auth"):
            return Outcome.AUTH_FAIL
        if now < s.get("rate_reset_ms", 0) or now < s.get("backoff_until_ms", 0) or self.stop:
            return Outcome.RATE_LIMITED
        if self.used >= self.scan_cap:
            return Outcome.RATE_LIMITED
        if task is not None and self.per_task.get(task, 0) >= self.task_cap:
            return Outcome.RATE_LIMITED
        return None

    def record(self, method, result, task):
        s, now = self.state, self.now
        self.used += 1
        if task is not None:
            self.per_task[task] = self.per_task.get(task, 0) + 1
        changed = False
        if result.outcome == Outcome.AUTH_FAIL:
            if not s.get("auth_failed"):
                self.alerts.append("ClickUp authentication failed (401/403): all ClickUp calls stopped; "
                                   "operator action needed; silent retry every 30 min")
            s.update(auth_failed=True, auth_retry_ms=now + AUTH_RETRY_MS)
            self.stop, changed = "auth", True
        elif result.outcome == Outcome.RATE_LIMITED:
            try:
                reset_ms = int(float(result.headers.get("x-ratelimit-reset"))) * 1000
            except (TypeError, ValueError):
                reset_ms = now + 60_000
            s["rate_reset_ms"] = max(reset_ms, now)
            self.stop, changed = "rate", True
        elif method == "GET" and result.outcome in (Outcome.AMBIGUOUS, Outcome.DEFINITE_FAIL) \
                and (result.status is None or result.status >= 500 or result.status == 408):
            step = min(s.get("backoff_step", 0) + 1, len(GET_BACKOFF_MIN))
            s.update(backoff_step=step, backoff_until_ms=now + GET_BACKOFF_MIN[step - 1] * 60_000)
            s.setdefault("fail_since_ms", now)
            if now - s["fail_since_ms"] >= FAIL_ALERT_MS and not s.get("fail_alerted"):
                self.alerts.append("ClickUp API failing (5xx/timeout) for 15+ minutes; backing off")
                s["fail_alerted"] = True
            self.stop, changed = "backoff", True
        elif result.outcome == Outcome.OK:
            if s.get("auth_failed"):
                self.alerts.append("ClickUp authentication recovered")
            if s.get("fail_alerted"):
                self.alerts.append("ClickUp API recovered")
            for k in ("auth_failed", "auth_retry_ms", "backoff_step", "backoff_until_ms", "fail_since_ms",
                      "fail_alerted"):
                if k in s:
                    s.pop(k)
                    changed = True
        remaining = _remaining(result)
        if result.outcome != Outcome.RATE_LIMITED and remaining is not None and remaining < self.reserve:
            self.stop = self.stop or "reserve"
        if changed:
            self._save()

    def note_deferred(self, deferred):
        """Track due work deferred by the budget; alert once after 30 min, one recovery line."""
        s = self.state
        if deferred:
            s.setdefault("deferred_since_ms", self.now)
            if self.now - s["deferred_since_ms"] >= BUDGET_ALERT_MS and not s.get("deferred_alerted"):
                self.alerts.append("ClickUp request budget exhausted for 30+ minutes; observation is delayed")
                s["deferred_alerted"] = True
        else:
            if s.get("deferred_alerted"):
                self.alerts.append("ClickUp request budget recovered")
            s.pop("deferred_since_ms", None)
            s.pop("deferred_alerted", None)
        self._save()


def _remaining(result):
    try:
        return int(result.headers.get("x-ratelimit-remaining"))
    except (TypeError, ValueError, AttributeError):
        return None


class GuardedTransport:
    """``ClickUpTransport`` view that counts requests (optionally for one task) and refuses beyond the budget.

    A refused request is not sent and returns RATE_LIMITED or AUTH_FAIL with ``status=None``.
    """

    def __init__(self, inner, guard, task=None):
        self.inner, self.guard, self.task = inner, guard, task

    def request(self, method, path, params=None, body=None, multipart=None):
        blocked = self.guard.gate(self.task)
        if blocked is not None:
            return TransportResult(blocked)
        result = self.inner.request(method, path, params=params, body=body, multipart=multipart)
        self.guard.record(method, result, self.task)
        return result


# ----------------------------------------------------------------------------- attachment staging
MAX_EACH_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_download_opener():
    return urllib.request.build_opener(_HttpsOnlyRedirect(),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def stage_attachments(attachments, dest_dir, bot_id, opener=None, timeout=60.0,
                      max_each=MAX_EACH_BYTES, max_total=MAX_TOTAL_BYTES):
    """§8.2: download visible human attachments (https only, NO token, caps) into ``dest_dir``.

    Returns one metadata record per attachment; ``staged`` is the file name or None with a ``reason``.
    """
    opener = opener or build_download_opener()
    total, out = 0, []
    for a in attachments:
        user = (a.get("user") or {}).get("id")
        rec = {"id": str(a.get("id")), "title": a.get("title"), "size": a.get("size"), "user_id": user,
               "date": a.get("date"), "staged": None, "reason": None}
        out.append(rec)
        url = a.get("url") or ""
        try:
            declared = int(a.get("size") or 0)
        except (TypeError, ValueError):
            declared = 0
        if a.get("deleted"):
            rec["reason"] = "deleted"
        elif a.get("hidden"):
            rec["reason"] = "hidden"
        elif user == bot_id:
            rec["reason"] = "bot"
        elif urllib.parse.urlsplit(url).scheme != "https":
            rec["reason"] = "not_https"
        elif declared > max_each:
            rec["reason"] = "too_large"
        elif total + declared > max_total:
            rec["reason"] = "total_cap"
        if rec["reason"]:
            continue
        try:
            with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
                data = resp.read(max_each + 1)
        except Exception:
            rec["reason"] = "download_failed"
            continue
        if len(data) > max_each:
            rec["reason"] = "too_large"
            continue
        if total + len(data) > max_total:
            rec["reason"] = "total_cap"
            continue
        name = rec["id"] + "-" + (re.sub(r"[^A-Za-z0-9._-]", "_", str(a.get("title") or "file"))[:80] or "file")
        with open(os.path.join(dest_dir, name), "wb") as f:
            f.write(data)
        total += len(data)
        rec.update(staged=name, reason="staged")
    return out
