"""S4 composition: scan loop, outbox executor, lock, alerts and operator subcommands (plan r5 §3, §4.1, §8, §10, §11).

One ``run`` is one cron scan in the §4.1 order: lock + config, status-set preflight (a mismatch ends the run
with no task reads, no evaluation, no outbox execution and no renewal), discovery and task passes (S1),
Kanban observation (``show --json``), engine evaluation (S2), outbox execution (S1 ClickUp effects, S3 Kanban
effects), then deduped operator alerts on stdout. ``ticket-check`` is read-only: it opens the state file of
its own installation and never loads the ClickUp token.

The executor adapts S2 outbox rows to the S1/S3 executors without changing either:
- ``cu_*`` rows run through ``clickup_effects.execute`` on a mirror row ``<key>#cu`` (S1 kinds and body);
  the S2 row copies the mirror's state and ``result_ref``.
- ``kb_*`` rows run through ``kanban_port.run_kanban_effect`` with the S3 kind and payload. A ``kb_unblock``
  of a blocked, never-run card (initial release, ``expect_status=blocked``) uses ``port.unblock`` plus a
  ready/todo/running readback; relay wakes keep the §10.3 wake guard.
- Before every send and retry (``send_gate``): ``rules.preconditions_ok`` (False cancels), a reply root must
  be an observed top-level comment of the row's own task (else cancelled + alert), then
  ``rules.send_barrier_ok`` for quiet-expired rows (False defers; not an attempt).
- Context snapshots are read resumably across scans (``read_snapshot``), and orchestrator/gate ``card_role``
  rows attach exactly one context or ledger file each.
- A planner output the §9 outbound filter rejects is never posted; it goes back to its own request card as a
  ``CLICKUP-FILTER v1`` comment naming the rule (never the matched text) plus a wake, so the planner posts a
  corrected draft that passes the same filter. After FILTER_FEEDBACK_MAX corrections per card and generation,
  or when the card cannot take one, the task is held with an operator alert (OUTBOUND-PROSE-F1). A woken card
  whose run ends without a corrected output also holds the task with one durable operator alert. Each such hold
  commits with the outcome that caused it and its durable alert row, so a restart cannot lose it.
"""

import argparse
import dataclasses
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time

from . import clickup_api, clickup_effects, engine, observe, render, rules, stalled, progress
from . import kanban_port as kp
from .config import load_config
from .contracts import EffectIntent, Outcome, Preconditions, RowState, TransportResult, load_preconditions
from .parse import parse_comment
from .store import Store, load_recovery_snapshot, load_ticket_snapshot, open_readonly, recovery_snapshot, \
    revoke_live_tickets
from .ticket import ticket_verdict

HERMES_EXE = "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe"
CREDENTIAL = "C:/Hermes/secrets/dedicated-panel-clickup/api-key.dpapi"
CONFIG_REL = ("integrations", "clickup-dedicated-panel", "config.json")

FINAL = ("done", "cancelled_stale", "failed", "held_ambiguous")
DEAD = ("cancelled_stale", "failed", "held_ambiguous")
CU_KINDS = {"cu_comment": "clickup_comment", "cu_reply": "clickup_reply", "cu_status": "clickup_status",
            "cu_attach": "clickup_attachment"}
KB_KINDS = {"kb_create": kp.CREATE, "kb_comment": kp.COMMENT, "kb_attach": kp.ATTACH, "kb_unblock": kp.UNBLOCK,
            "kb_complete": kp.COMPLETE, "kb_archive": kp.ARCHIVE, "kb_subscribe": kp.SUBSCRIBE}
KB_BACKOFF_MIN = clickup_effects.SEND_BACKOFF_MIN
MAX_ATTEMPTS = clickup_effects.MAX_ATTEMPTS
PREFLIGHT_KEY = "incident_preflight"
OPEN_ALERTS_KEY = "open_alerts"
CLOSED_CARDS_KEY = "closed_cards"
WATCHED_CLOSED_KEY = "blocker_closed_cards"  # blocker plan r6 §4.2
RECOVERY_CLOSED_KEY = "recovery_closed_cards"  # technical-recovery plan r3 §4.1
PREPASS_KEY = "prepass_outbox_mark"
COMMENT_PAGE = 25
# OUTBOUND-PROSE-F1: corrected drafts asked of one request card per input generation; the next rejection holds
FILTER_FEEDBACK_MAX = 2
_OUTPUT_KEY = re.compile(r"clickup-dp:(?P<task>[^:]+):(?:out|q:business|q:technical):(?P<ref>[0-9a-f]{12})")
_HEADER_VALUE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
NO_CORRECTION = ("done", "archived", "triage", "blocked")
FILTER_RULES = {
    "token": "the integration's own ClickUp API token",
    "api_key": "an API key (pk_...)",
    "private_key": "a PEM private key block",
    "connection_string": "a connection string that carries a password",
    "password": "a credential-shaped value after a password/passwd/pwd label (the label itself and prose about "
                "passwords are fine)",
    "credential_url": "a URL with embedded user:password@ credentials",
    "secret_path": "a path into a secret store (.dpapi files, Hermes secrets)",
}


def _sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _ok(r):
    return r is not None and r.outcome is Outcome.OK


PLAN_NAME = re.compile(r"clickup-plan-r[1-9][0-9]*\.md")
MARKER_SCAN_MAX = 1024 * 1024  # workflow repair r3 §3.1: files over 1 MiB are not scanned for markers
ROLLBACK_TARGET = "45f7c252df27"  # public release of source 149042d: the built-in rollback target of this instance


def _observed_attachment(a):
    """``{name, sha256}`` of a stored attachment; a plan file also carries its r3 §3.1 ``incorporates`` markers."""
    out = {"name": a.get("filename"), "sha256": a.get("sha256")}
    data = a.get("data")
    if isinstance(out["name"], str) and PLAN_NAME.fullmatch(out["name"]) and isinstance(data, bytes):
        if len(data) > MARKER_SCAN_MAX:
            out["incorporates_oversize"] = True
        else:
            out["incorporates"], ignored = rules.parse_incorporates(data.decode("utf-8", "replace"))
            if ignored:
                out["incorporates_ignored"] = ignored
    return out


# A fixed allowance counts attempted native reads, including failures, across the whole
# target scan. Incomplete source/dependency discovery is checkpointed, not discarded.
PREWORKER_READ_LIMIT = 64


class _PreworkerReads:
    def __init__(self, port, clock):
        self.port, self.clock = port, clock
        self.remaining = PREWORKER_READ_LIMIT
        self.cache, self.current = {}, set()
        self.exhausted = False
        self.proof = None
        self.binding = None

    def seed(self, observations):
        for key, value in observations.items():
            if key not in self.cache:  # discovery only; never evidence at admission/effect
                self.cache[key] = value

    def saved(self):
        return {k: v for k, v in self.cache.items() if v['value'] is not None}

    def take(self):
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True

    def invalidate(self, cards):
        for card in cards:
            for kind in ('show', 'attachments'):
                key = kind + ':' + card
                self.cache.pop(key, None)
                self.current.discard(key)

    def read(self, kind, card, fresh=False):
        key = kind + ':' + card
        if fresh and key not in self.current:
            self.cache.pop(key, None)
        if key in self.cache:
            return self.cache[key]['value']
        if not self.take():
            return None
        started = self.clock()
        result = getattr(self.port, kind)(card)
        value = result.json if _ok(result) else None
        if self.clock() - started > 5 * 60_000:
            value = None  # one overlong read is not a renewable complete observation
        if kind == 'show':
            if not isinstance(value, dict) or (value.get('task') or {}).get('id') != card:
                value = None
        elif not isinstance(value, list):
            value = None
        else:
            observed = []
            for att in value:
                if not isinstance(att, dict) or not PLAN_NAME.fullmatch(str(att.get('filename', ''))):
                    continue
                try:
                    with open(att['stored_path'], 'rb') as f:
                        data = f.read(MARKER_SCAN_MAX + 1)
                except (KeyError, OSError, TypeError):
                    continue
                if len(data) <= MARKER_SCAN_MAX:
                    observed.append(_observed_attachment(dict(att, data=data, sha256=_sha(data))))
            value = observed
        self.current.add(key)
        self.cache[key] = dict(at=self.clock(), value=value)
        return value


class _PreworkerTransport:
    def __init__(self, inner, reader):
        self.inner, self.reader = inner, reader

    def request(self, *args, **kwargs):
        if not self.reader.take():
            return TransportResult(Outcome.RATE_LIMITED)
        return self.inner.request(*args, **kwargs)


def _preworker_binding(task, approval, state):
    return _sha(json.dumps([approval, {k: task[k] for k in ('input_gen', 'scope_gen', 'classified_gen',
        'published_rev', 'published_sha256', 'terminal', 'hold_reason')},
        {k: state[k] for k in ('requests', 'sources', 'blockers', 'fences', 'stage_rows')}], sort_keys=True))


def _preworker_discover(task, approval, state, reader, extra=(), recheck=False):
    """Resolve the exact PASS source, then traverse every real required parent edge.

    Cached continuation is approval/input-bound DISCOVERY ONLY. No cached show
    authorizes an effect: _preworker_collect re-derives the full current closure.
    """
    from . import preworker
    cards, atts, source = {}, {}, None
    # A delivered publication is a locator, never PASS evidence. Revalidate its exact
    # request/reviewer/attachment contract; do not reread unrelated historical requests.
    hints = [p['card'] for p in state['sources'] if p.get('sha256') == approval['plan_sha256']
             and p.get('card') in state['requests']]
    candidates = list(dict.fromkeys(hints + state['requests']))
    for card in candidates:
        shown, attachments = reader.read('show', card), reader.read('attachments', card)
        if shown is None or attachments is None:
            return cards, atts, False
        ev = preworker.source_evidence(shown, attachments, task['clickup_task_id'])
        if ev and ev['rev'] == approval['rev'] and ev['sha256'] == approval['plan_sha256']:
            if recheck:
                reader.invalidate({card})
            shown = reader.read('show', card, fresh=True)
            attachments = reader.read('attachments', card, fresh=True)
            ev = preworker.source_evidence(shown, attachments, task['clickup_task_id'])
            if not ev or ev['rev'] != approval['rev'] or ev['sha256'] != approval['plan_sha256']:
                return cards, atts, False
            source = card
            cards[card], atts[card] = shown, attachments
            break
    if source is None:
        return cards, atts, False
    roots = preworker.targets(approval, state) | {b['question_card'] for b in state['blockers']} | set(extra)
    roots |= {f['fence_card'] for f in state['fences'] if f['state'] in ('open', 'releasing')}
    if recheck:
        reader.invalidate(roots)
    if recheck:
        return cards, atts, True  # complete closure comes from the read-only snapshot
    pending, seen = roots | {source}, set()
    while pending:
        card = min(pending)
        pending.remove(card)
        if card in seen:
            continue
        seen.add(card)
        shown = reader.read('show', card, fresh=card in roots)
        if shown is None or not isinstance(shown.get('parents'), list):
            return cards, atts, False
        cards[card] = shown
        parents = shown['parents']
        if any(not isinstance(p, str) for p in parents):
            return cards, atts, False
        pending.update(set(parents) - seen)
        if card == approval['orchestrator_card']:
            gates = rules.gates_of(shown, approval['rev'], approval['plan_sha256'])
            if gates:
                pending.update({gates['promotion']} - seen)
    return cards, atts, True


def _preworker_collect(task, approval, state, reader, extra=(), recheck=False):
    """Bounded discovery is not evidence: every accepted closure gets a new native snapshot."""
    from . import preworker
    direct = isinstance(reader.port, kp.KanbanCli)
    if not direct:
        _, _, complete = _preworker_discover(task, approval, state, reader, extra, recheck)
        if not complete:
            return {}, {}, False
    factory = getattr(reader.port, '_preworker_snapshot', None)
    if factory is None:
        return {}, {}, False
    roots = preworker.targets(approval, state) | {b['question_card'] for b in state['blockers']} | set(extra)
    roots |= {f['fence_card'] for f in state['fences'] if f['state'] in ('open', 'releasing')}
    if reader.proof is not None:
        reader.proof.close()
    reader.proof = proof = factory(roots | set(state['requests']), reader.take, reader.clock)
    if proof is None:
        return {}, {}, False
    cards, atts, source = {}, {}, None
    hints = [p['card'] for p in state['sources'] if p.get('sha256') == approval['plan_sha256']
             and p.get('card') in state['requests']]
    for card in dict.fromkeys(hints + state['requests']):
        shown = proof.shows.get(card)
        if shown is None:
            continue
        attachments = []
        for att in proof.attachments.get(card, []):
            if not PLAN_NAME.fullmatch(str(att.get('filename', ''))):
                continue
            try:
                with open(att['stored_path'], 'rb') as stream:
                    data = stream.read(MARKER_SCAN_MAX + 1)
            except (KeyError, OSError, TypeError):
                continue
            if len(data) <= MARKER_SCAN_MAX:
                attachments.append(_observed_attachment(dict(att, data=data, sha256=_sha(data))))
        ev = preworker.source_evidence(shown, attachments, task['clickup_task_id'])
        if ev and ev['rev'] == approval['rev'] and ev['sha256'] == approval['plan_sha256']:
            for att in proof.attachments.get(card, []):
                match = next((a for a in attachments if a['name'] == att.get('filename')
                              and a['sha256'] == ev['sha256']), None)
                if match is not None:
                    proof.files[att['stored_path']] = match['sha256']
            source, atts[card] = card, attachments
            break
    if source is None:
        return {}, {}, False
    pending, seen = roots | {source}, set()
    while pending:
        card = pending.pop()
        if card in seen:
            continue
        seen.add(card)
        shown = proof.shows.get(card)
        if (not isinstance(shown, dict) or (shown.get('task') or {}).get('id') != card
                or not isinstance(shown.get('parents'), list)
                or any(not isinstance(p, str) for p in shown['parents'])):
            return {}, {}, False
        cards[card] = shown
        pending.update(set(shown['parents']) - seen)
        if card == approval['orchestrator_card']:
            gates = rules.gates_of(shown, approval['rev'], approval['plan_sha256'])
            if gates:
                pending.update({gates['promotion']} - seen)
    reader.binding = _preworker_binding(task, approval, state)
    return cards, atts, proof.valid()


# ----------------------------------------------------------------------------------------- capture
class _Capture:
    """ClickUp transport view that records this scan's GET results for the engine (no persistence)."""

    def __init__(self, inner, glue):
        self.inner, self.glue = inner, glue

    def request(self, method, path, params=None, body=None, multipart=None):
        r = self.inner.request(method, path, params=params, body=body, multipart=multipart)
        if method == "GET" and r.outcome is Outcome.OK and isinstance(r.json, dict):
            self.glue.capture(path, r.json)
        return r


# ----------------------------------------------------------------------------------------- the glue
class Glue:
    def __init__(self, cfg, store, transport, port, now, *, secrets=(), opener=None, log=None):
        self.cfg, self.store, self.c, self.inner, self.port, self.now = cfg, store, store.conn, transport, port, now
        self.secrets = tuple(s for s in secrets if s)
        self.opener = opener
        self.log = log or logging.getLogger("clickup_intake.null")
        self.guard = clickup_api.ApiGuard(store, cfg, now)
        self.staging = os.path.join(cfg.runtime_dir, "staging")
        self.out = []          # stdout lines (operator alerts)
        self.alerts = []       # incident alerts of this scan (deduped across scans)
        self.comments, self.confirmed, self.roots = {}, set(), {}
        self.obs = engine.Observation(scan_started_ms=now)
        self._att = {}
        self._att_failed = set()
        self._diagnostic_refresh = {}  # at most one bounded target-local L per task per scan
        self._preworker_reads = {}  # one shared native read allowance per task/scan
        self.dependency_members = {}


    # -- meta
    def meta(self, key, default=None):
        r = self.c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key, value):
        with self.store.transaction() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # -- capture of this scan's reads
    def capture(self, path, data):
        m = re.fullmatch(r"/v2/task/([^/]+)", path)
        if m:
            lst = data.get("list")
            if isinstance(lst, dict) and str(lst.get("id")) == str(self.cfg.list_id):
                self.confirmed.add(m.group(1))
            return
        m = re.fullmatch(r"/v2/task/([^/]+)/comment", path)
        if m and isinstance(data.get("comments"), list):
            for cm in data["comments"]:
                if isinstance(cm, dict) and cm.get("id") is not None:
                    self.comments[(m.group(1), "comment", str(cm["id"]))] = (cm.get("comment"), cm.get("comment_text"))
                    self.roots[str(cm["id"])] = m.group(1)
            return
        m = re.fullmatch(r"/v2/comment/([^/]+)/reply", path)
        if m and isinstance(data.get("comments"), list):
            root = m.group(1)
            tid = self.roots.get(root)
            if tid is None:
                r = self.c.execute("SELECT task FROM items WHERE kind='comment' AND id=?", (root,)).fetchone()
                tid = r[0] if r else None
            if tid is None:
                return
            for cm in data["comments"]:
                if isinstance(cm, dict) and cm.get("id") is not None:
                    self.comments[(tid, "reply", str(cm["id"]))] = (cm.get("comment"), cm.get("comment_text"))

    # ------------------------------------------------------------------------------------- §4.1 scan
    def preflight(self):
        """§4.1 step 2. Returns True only on an exact status-set match."""
        pf = observe.preflight_statuses(self.guard.transport(self.inner), self.cfg)
        incident = self.meta(PREFLIGHT_KEY)
        if pf.status == "MISMATCH":
            text = "ClickUp intake stopped: " + pf.describe() + "; no task polling and no writes until it matches"
            if incident != text:
                self.out.append(text)
                self.set_meta(PREFLIGHT_KEY, text)
            self.log.info(json.dumps({"event": "preflight", "status": "MISMATCH"}))
        elif pf.ok and incident is not None:
            self.out.append("ClickUp intake: status-set preflight matches again; polling resumed")
            self.set_meta(PREFLIGHT_KEY, None)
        if not pf.ok:
            self.alerts.extend(self.guard.alerts)
        return pf.ok

    def scan(self, shadow=False):
        if not self.preflight():
            self.finish()
            return
        res = observe.observe_scan(_Capture(self.inner, self), self.store, self.cfg, self.now, guard=self.guard)
        n_guard = len(self.guard.alerts)
        self.alerts.extend(res.alerts)
        self.early_progress(shadow=shadow)
        self.observe_cards()
        self.recovery_readbacks()
        self.progress_pass()
        engine.evaluate(self.store, self.now, self.obs, self.cfg)
        self._refresh_arriving_fences(res)
        if shadow:
            self.shadow_report(res)
        else:
            self.uncorrected_feedback()
            self.execute()
        self.alerts.extend(self.guard.alerts[n_guard:])
        self.close_cards()
        self.sweep_staging()
        self.finish()

    def early_progress(self, shadow=False):
        """Current approved staging must not wait for unrelated historical native cards.

        Only a completed L in this scan qualifies. Source search and dependency expansion
        continue locally under a shared request allowance, never through unrelated history.
        """
        from . import preworker
        self.obs = dataclasses.replace(self.obs, comments=dict(self.comments), confirmed=frozenset(self.confirmed))
        apps = [dict(a) for a in self.c.execute("SELECT * FROM approvals WHERE state IN ('recorded','staged') ORDER BY id")]
        if self.meta(engine.RECOVERY_KEY) is not None:
            apps += [dict(a) for a in self.c.execute("SELECT a.* FROM approvals a WHERE a.state IN (%s) AND EXISTS"
                     " (SELECT 1 FROM recoveries r WHERE r.approval_id=a.id AND r.class=? AND r.state='open')"
                     % engine._in(engine.WATCHED_STATES), (preworker.CLASS,))]
        for a in apps:
            tid = a['task']
            t = dict(self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone())
            if (tid not in self.confirmed or t['last_light_ms'] != self.now or t['terminal'] or t['hold_reason']
                    or t['classified_gen'] != t['input_gen'] or not preworker.current_plan(t, a)
                    or t['phase'] not in preworker.PHASES or not rules.barrier_ok(t, 0, self.store.clock(), self.now)
                    or self.c.execute("SELECT 1 FROM approvals WHERE task=? AND id>? AND state IN (%s)" %
                                      engine._in(engine.ACTIVE + engine.WATCHED_STATES), (tid, a['id'])).fetchone()):
                continue
            state = preworker.context(self.c, tid)
            want = set()
            if self.meta(engine.RECOVERY_KEY) is not None:
                for inc in self.c.execute("SELECT * FROM recoveries WHERE approval_id=? AND class=? AND state='open'",
                                          (a['id'], preworker.CLASS)):
                    want |= {r[0] for r in self.c.execute("SELECT result_ref FROM outbox WHERE kind='kb_create'"
                               " AND state='done' AND idem_key LIKE ? AND result_ref IS NOT NULL",
                               (engine._rcv_key(tid, inc['card'], 0, 0)[:-1] + '%',))}
            reader = self.preworker_reader(tid)
            key = 'preworker_reads:%d' % a['id']
            binding = _preworker_binding(t, a, state)
            prior = self.meta(key) or {}
            if prior.get('binding') == binding:
                reader.seed(prior.get('observations', {}))
            cards, atts, complete = _preworker_collect(t, a, state, reader, extra=want)
            self.set_meta(key, None if complete else dict(binding=binding, observations=reader.saved()))
            if not complete:
                continue
            now = self.store.clock()
            if now - t['last_light_ms'] > 5 * 60_000:
                if not self.diagnostic_input(tid, reader=reader):
                    continue
                # Only current roots need renewal; historical source candidates are not
                # required obligations. A changed root adds its complete parent closure.
                reader.invalidate(set(atts) | preworker.targets(a, state) |
                                  {b['question_card'] for b in state['blockers']} |
                                  {f['fence_card'] for f in state['fences'] if f['state'] in ('open', 'releasing')})
                cards, atts, complete = _preworker_collect(t, a, state, reader, extra=want)
                if not complete:
                    continue
            latest = dict(self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone())
            current_a = dict(self.c.execute('SELECT * FROM approvals WHERE id=?', (a['id'],)).fetchone())
            if (_preworker_binding(latest, current_a, preworker.context(self.c, tid)) != binding
                    or tid not in self.confirmed):
                continue
            t, a, now = latest, current_a, self.store.clock()
            self.obs = dataclasses.replace(self.obs, cards=cards, attachments=atts,
                comments=dict(self.comments), confirmed=frozenset(self.confirmed),
                workers={c: p for c, w in cards.items() if (p := kp.worker_progress(w, now)) is not None})
            ctx = engine._Ctx(self.store, now, self.obs, self.cfg)
            if (now - t['last_light_ms'] > 5 * 60_000
                    or reader.proof is None or not reader.proof.valid()
                    or engine._pass_source(ctx, tid, a['rev'], a['plan_sha256']) is None):
                continue
            if a['state'] in preworker.STATES:
                if preworker.inspect(t, a, state, cards, atts, now, self.now) is None:
                    continue
                engine._approvals(ctx, tid, early_only=a['id'])
            current = ctx.approval(a['id'])
            if current['state'] == 'released' and self.meta(engine.RECOVERY_KEY) is not None and self.c.execute(
                    "SELECT 1 FROM recoveries WHERE approval_id=? AND class=? AND state='open'",
                    (a['id'], preworker.CLASS)).fetchone():
                # Only an already admitted early incident is monitored here. The existing
                # start handler verifies completed orchestration/guarded promotion, not READY.
                engine._start(ctx, tid, current)
            self.obs = dataclasses.replace(self.obs, early_approvals=self.obs.early_approvals | {a['id']})
            if self.meta(engine.RECOVERY_KEY) is not None:
                for inc in map(dict, self.c.execute("SELECT * FROM recoveries WHERE approval_id=? AND class=?"
                                                    " AND state='open'", (a['id'], preworker.CLASS)).fetchall()):
                    with self.store.transaction():
                        preworker.step(ctx, tid, ctx.task(tid), inc)
                with self.store.transaction():
                    preworker.detect(ctx, tid)
            bases = engine._keys(tid, a)
            notice = 'clickup-dp:%s:recovery-notice:%s:0' % (tid, preworker.anchor(a))
            subscriptions = {'clickup-dp:%s:subscribe:%s' % (tid, card)
                             for card in preworker.targets(a, preworker.context(self.c, tid))}
            keys = {r['idem_key'] for r in map(dict, self.c.execute('SELECT * FROM outbox WHERE task=?', (tid,)))
                    if any(r['idem_key'] == b or r['idem_key'].startswith(b + ':') for b in bases)
                    or r['idem_key'] in subscriptions
                    or r['idem_key'] == notice or r['idem_key'].startswith(notice + ':')
                    or r['idem_key'].startswith(engine._rcv_key(tid, preworker.anchor(a), 0, 0)[:-1])}
            if not shadow:
                self.execute(only=keys)

    def preworker_reader(self, tid):
        if tid not in self._preworker_reads:
            self._preworker_reads[tid] = _PreworkerReads(self.port, self.store.clock)
        return self._preworker_reads[tid]

    def _refresh_arriving_fences(self, observation):
        """One real target-local F then L for an answered fence newer than this scan's coverage.

        Keep scan membership and old timestamps intact. Resume an existing full cursor as-is;
        only a completed post-anchor full and its later clean light can authorize release.
        """
        ctx = engine._Ctx(self.store, self.store.clock(), self.obs, self.cfg)
        cut = engine._cutover_ms(ctx)
        if cut is None:
            return
        attempted = set()
        fences = self.c.execute("SELECT * FROM fences WHERE kind='fence' AND state='open'"
                                " AND anchor_ms>? ORDER BY id", (self.obs.scan_started_ms,)).fetchall()
        for f in map(dict, fences):
            tid, anchor = f['task'], f['anchor_ms']
            now = self.store.clock()
            t = ctx.task(tid)
            if (tid in attempted or now < anchor or t['terminal'] or t['hold_reason']
                    or tid not in self.obs.confirmed or t['classified_gen'] != t['input_gen']
                    or now < (t['quiet_until_ms'] or 0)):
                continue
            w, parent = self.obs.cards.get(f['card']), self.obs.cards.get(f['fence_card'])
            if (not w or not parent or kp.protocol_parent(f['card'], w, parent, cut) != ('fence', 'record')):
                continue
            blockers = [dict(b) for b in self.c.execute(
                "SELECT * FROM blockers WHERE card=? AND state NOT IN (%s)" % engine._in(engine.BLOCKER_FINAL),
                (f['card'],))]
            if any(self.obs.cards.get(b['question_card']) is None for b in blockers):
                continue
            answered = [b for b in blockers if rules.status(self.obs.cards[b['question_card']]) == 'done']
            if (not answered or any(b['state'] != 'delivered' or not engine._settled(ctx, t, b) for b in answered)
                    or rules.barrier_ok(t, anchor, now, self.obs.scan_started_ms)):
                continue
            attempted.add(tid)
            transport = self.guard.transport(_Capture(self.inner, self), task=tid)
            full = observe.full_pass(transport, self.store, self.cfg, tid, now)
            observation.passes.append(full)
            self.alerts.extend(full.alerts)
            t = ctx.task(tid)
            if (not full.complete or full.activity or full.hold or full.terminal or full.not_admitted
                    or (t['full_started_ms'] or 0) < max(anchor, t['quiet_until_ms'] or 0)):
                observation.deferred = True
                continue
            light = observe.light_pass(transport, self.store, self.cfg, tid, self.store.clock())
            observation.passes.append(light)
            self.alerts.extend(light.alerts)
            if not light.complete or light.activity or light.hold or light.terminal or light.not_admitted:
                observation.deferred = True
                continue
            self.obs = dataclasses.replace(self.obs, comments=dict(self.comments), confirmed=frozenset(self.confirmed))
            # Only the existing fence evaluator is repeated, not task commands or the global scan.
            engine._fences(engine._Ctx(self.store, self.store.clock(), self.obs, self.cfg), tid, discovered_now=True)
        if attempted:
            self.guard.note_deferred(observation.deferred)

    def recovery_readbacks(self):
        """Held tasks are not ordinarily polled. Reuse bounded transport readback for an accepted own-hold notice.
        This reads only; it neither retries a write nor clears the failed carrier's execution hold."""
        rows = self.c.execute("SELECT o.* FROM outbox o JOIN tasks t ON t.clickup_task_id=o.task WHERE"
                              " o.kind='cu_comment' AND o.state='done' AND o.idem_key LIKE '%:recovery-notice:%'"
                              " AND t.terminal=0 AND t.hold_reason IS NOT NULL").fetchall()
        for row in map(dict, rows):
            p, key = json.loads(row["body"]), row["idem_key"]
            hold = self.c.execute("SELECT hold_reason FROM tasks WHERE clickup_task_id=?", (row["task"],)).fetchone()[0]
            if not p.get("recovery_delivery_hold") or p["recovery_delivery_hold"] != hold or self.meta("recovery_verified:" + key):
                continue
            mirror = self.row(key + "#cu")
            if mirror is None or mirror["state"] != "done":
                continue
            effect = clickup_effects._Effect(self.guard.transport(self.inner, task=row["task"]), self.store,
                                              self.cfg, key + "#cu", self.now, lambda _: False)
            found, complete = effect._find()
            if complete and found is not None and found == row["result_ref"]:
                self.set_meta("recovery_verified:" + key, {"ref": found, "sha256": row["payload_sha256"]})

    def progress_pass(self):
        """S2 consumes worker outputs before it records delivered relays. A first pass without cards or
        comments records this run's earlier outbox results, so a worker that parked since then is matched
        to its relay. Without cards that pass can raise false alerts (e.g. a plan source "not observable"),
        so the alert rows it added are dropped; the full pass queues every genuine one again. Kept are the r9 §7
        per-question disable alerts (NA1): ``engine._disabled_blockers`` decides them from the outbox alone
        and queues each once with its blocker's transition, which the full pass does not repeat. The outbox
        rowid mark is kept in meta until the drop commits, so a crash in between drops them on the rerun."""
        mark = self.meta(PREPASS_KEY)
        if mark is None:
            mark = self.c.execute("SELECT COALESCE(MAX(rowid), 0) FROM outbox").fetchone()[0]
            self.set_meta(PREPASS_KEY, mark)
        engine.evaluate(self.store, self.now,
                        dataclasses.replace(self.obs, comments={}, cards={}, attachments={}, workers={},
                                            progress_scan=False), self.cfg)
        with self.store.transaction() as c:
            c.execute("DELETE FROM outbox WHERE kind='alert' AND state='pending' AND rowid>? AND idem_key NOT LIKE ?"
                      " AND idem_key NOT LIKE ?", (mark, "clickup-dp:alert:blocker:%:disabled:delivered",
                                                   "clickup-dp:alert:blocker:%:disabled:open"))
            c.execute("DELETE FROM meta WHERE key=?", (PREPASS_KEY,))

    def finish(self):
        """Deduped incident alerts: an alert repeated by consecutive scans is printed once."""
        prev = set(self.meta(OPEN_ALERTS_KEY, []))
        cur = list(dict.fromkeys(self.alerts))
        self.out.extend(a for a in cur if a not in prev)
        if set(cur) != prev:
            self.set_meta(OPEN_ALERTS_KEY, cur or None)

    def shadow_report(self, res):
        rows = self.c.execute("SELECT idem_key, kind, target FROM outbox WHERE state NOT IN (%s) AND kind<>'alert'"
                              " AND kind NOT LIKE 'clickup_%%' ORDER BY rowid" % ",".join("?" * len(FINAL)),
                              FINAL).fetchall()
        for key, kind, target in rows:
            self.log.info(json.dumps({"event": "would", "kind": kind, "target": target, "key": key}))
        self.execute(alerts_only=True)
        d = res.discovery
        self.out.append("shadow: preflight OK; discovery pages=%d complete=%s admitted=%d; task passes=%d; "
                        "would-effects=%d" % (d.pages, d.complete, len(d.admitted), len(res.passes), len(rows)))

    # ------------------------------------------------------------------------------- §4.1 step 5
    def _show(self, card):
        r = self.port.show(card)
        return r.json if _ok(r) and isinstance(r.json, dict) else None

    def observe_cards(self):
        """``show --json`` of glue-owned cards and ``clickup_gates`` ids; ``attachments`` of done request cards."""
        c = self.c
        terminal = {r[0] for r in c.execute("SELECT clickup_task_id FROM tasks WHERE terminal=1")}
        closed = set(self.meta(CLOSED_CARDS_KEY, []))
        want = {}
        for card, task, role in c.execute("SELECT card_id, task, role FROM cards ORDER BY rowid"):
            if task in terminal and (role != "request" or card in closed):
                continue
            want[card] = role
        for key, task, ref in c.execute("SELECT idem_key, task, result_ref FROM outbox WHERE kind='kb_create'"
                                        " AND state='done' AND result_ref IS NOT NULL ORDER BY rowid"):
            if ref and task not in terminal and ":rcv:" not in key:  # recovery carriers are never re-shown (r3 §4.4)
                want.setdefault(ref, "request" if ":req:" in key else "other")
        cards = {}
        for card in want:
            shown = self._show(card)
            if shown is not None:
                cards[card] = shown
        for a in map(dict, c.execute("SELECT * FROM approvals ORDER BY id")):
            if a["task"] in terminal:
                continue
            g = json.loads(a["clickup_gates"]) if a["clickup_gates"] else None
            if g is None and a["state"] == "released" and a["orchestrator_card"] in cards:
                g = rules.gates_of(cards[a["orchestrator_card"]], a["rev"], a["plan_sha256"])
            for card in ((g or {}).get("final_e2e"), (g or {}).get("promotion")):
                if card and card not in cards:
                    shown = self._show(card)
                    if shown is not None:
                        cards[card] = shown
        self._observe_watched(cards, terminal)
        self._observe_recovery(cards, terminal)
        atts = {}
        for card, role in want.items():
            if role == "request" and rules.status(cards.get(card)) == "done":
                atts[card] = [_observed_attachment(a) for a in self.attachments(card)]
                if card in self._att_failed:
                    del atts[card]
        self._observe_progress(cards, atts)
        workers = {card: proof for card, shown in cards.items()
                   if (proof := kp.worker_progress(shown, self.store.clock())) is not None}
        self.obs = engine.Observation(scan_started_ms=self.now, comments=dict(self.comments), cards=cards,
                                      attachments=atts, confirmed=frozenset(self.confirmed), workers=workers,
                                      early_approvals=self.obs.early_approvals, progress_scan=True,
                                      dependency_members=self.dependency_members)

    def _observe_progress(self, cards, atts):
        """Observe settled handoffs and post-completion work with the shared bounded native reader."""
        from . import progress
        if self.meta(engine.RECOVERY_KEY) is None:
            return
        for tid in sorted(self.confirmed):
            task = dict(self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone())
            state = progress.context(self.c, tid)
            admitted = self.c.execute("SELECT 1 FROM recoveries WHERE task=? AND class=? AND state='open'",
                                      (tid, progress.CLASS)).fetchone() is not None
            if not state['requests'] or (kp.card_status(cards.get(state['requests'][0])) not in ('done', 'blocked', 'triage')
                                         and not admitted):
                continue
            if state['requests'][0] in self._att_failed:
                continue  # a failed source read must not be laundered through a second observation path
            kind, _ = progress.projection(task, state, cards, atts, self.now, self.obs.scan_started_ms, admitted=admitted)
            _, outer = progress.outer(task, state, cards, atts, self.now, self.obs.scan_started_ms)
            if kind != 'unknown' and not state['completed_incidents'] and not (outer or {}).get('native_unavailable'):
                continue
            # Initial and retired workflows need the same closure as admission,
            # even when the ordinary settled-source projection is unchanged.
            before = progress.binding(task, state)
            reader = self.preworker_reader(tid)
            observed, attachments, complete = _progress_collect(state, reader)
            latest = dict(self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone())
            # Leave the nine-read fresh snapshot plus its two post-I/O validity
            # checks available. A scan that cannot repeat its proof at admission
            # must keep the same unknown diagnosis as that admission.
            if (complete and before == progress.binding(latest, progress.context(self.c, tid))
                    and reader.proof.valid() and reader.remaining >= 11):
                cards.update(observed)
                atts.update(attachments)

    def protocol_targets(self):
        """r9 §7 D1-A: (task, card) for each watched card (never the gate) of non-terminal, unheld tasks whose fresh
        ``show`` is neither done nor archived."""
        out = []
        for (tid,) in self.c.execute("SELECT clickup_task_id FROM tasks WHERE terminal=0 AND (hold_reason IS NULL"
                                     " OR hold_reason='') ORDER BY clickup_task_id").fetchall():
            for card in engine._current_watched(self, tid):
                shown = self._show(card)
                if shown is not None and rules.status(shown) not in ("done", "archived"):
                    out.append((tid, card))
        return out

    def _observe_watched(self, cards, terminal):
        """Blocker plan observation: the watched cards while enabled (``_observe_live_watched``), then the two cards
        of every recorded open/releasing fence or park and releasing blocker of an unheld task, and while enabled
        the question card of each non-final blocker of an unheld terminal task and of each live task's blocker
        that ``engine._dispose`` may make terminal or superseded."""
        enabled = self.meta(engine.CUTOVER_KEY) is not None
        if not enabled:
            if not self.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fences'").fetchone():
                return
        else:
            self._observe_live_watched(cards, terminal)
        # r12 §3.3/§9, r11 §9: recorded open/releasing fences and parks keep their two cards observed (also after
        # disable and for a terminal task) until they are final; r9 §7: so do a live task's releasing blockers, whose
        # kept rows are finished after disable; r9 §4.4 / r11 §3.5 (NA1): so does a terminal task's open
        # blocker's Q, read by _closed_task_blockers until the blocker is final. Held tasks wait.
        unheld = " JOIN tasks t ON t.clickup_task_id=x.task WHERE (t.hold_reason IS NULL OR t.hold_reason='') AND "
        for pair in self.c.execute(
                "SELECT x.fence_card, x.card FROM fences x" + unheld + "x.state IN ('open', 'releasing') UNION ALL"
                " SELECT x.question_card, x.card FROM blockers x" + unheld + "t.terminal=0 AND x.state='releasing'"
                + (" UNION ALL SELECT x.question_card, x.question_card FROM blockers x" + unheld
                   + "t.terminal=1 AND x.state NOT IN (%s)" % engine._in(engine.BLOCKER_FINAL) if enabled else "")
                ).fetchall():
            for x in pair:
                if x not in cards:
                    shown = self._show(x)
                    if shown is not None:
                        cards[x] = shown
        if self.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='amendment_answers'").fetchone():
            for pair in self.c.execute(  # workflow repair r3 §3.10: Q and W of every bound binding of a live task
                    "SELECT x.question_card, x.card FROM amendment_answers x" + unheld
                    + "t.terminal=0 AND x.state='bound' ORDER BY x.id").fetchall():
                for x in pair:
                    if x not in cards:
                        shown = self._show(x)
                        if shown is not None:
                            cards[x] = shown
        if enabled:  # r9 §4.4 / r11 §3.5 (NA2): engine._dispose's terminal/superseded case reads Q itself,
            # also once Q is no longer a parent of W: a live unheld task's non-releasing blocker whose W is
            # done/archived (or cached closed) or whose approval is gone (W is then not watched: SPEC-R6-B2); and
            # a post-closure one whose W is unobserved, so that its material input is decided (SPEC-R6-B2).
            # NA3: a delivered blocker of a gone approval also needs W itself (its consumption ACK precedes
            # the superseded disposition): a read-only show of that recorded W only, never watched or cached.
            closed = set(self.meta(WATCHED_CLOSED_KEY, []))
            final = engine._in(engine.BLOCKER_FINAL + ("releasing",))
            for q, w, state, gone, delivered in self.c.execute(
                    "SELECT x.question_card, x.card, x.state, a.id IS NULL OR a.state='stale' OR (a.state='revoked'"
                    " AND a.revoked_reason='superseded'), x.delivered_gen IS NOT NULL FROM blockers x"
                    " LEFT JOIN approvals a ON a.id=x.approval_id"
                    + unheld + "t.terminal=0 AND x.state NOT IN (%s) ORDER BY x.id" % final).fetchall():
                if gone and delivered and w not in cards and w not in closed:
                    shown = self._show(w)
                    if shown is not None:
                        cards[w] = shown
                if q not in cards and (gone or w in closed or (rules.status(cards[w]) in ("done", "archived")
                                                               if w in cards else state in engine.POST_CLOSURE)):
                    shown = self._show(q)
                    if shown is not None:
                        cards[q] = shown

    def _observe_live_watched(self, cards, terminal):
        """Blocker plan r6 §4.2 / r9 §4.2: once the cutover is set, ``show`` the watched cards of non-terminal,
        unheld tasks (never the gate) and each watched card's not-yet-recorded parents. Watched cards observed
        done/archived are cached and no longer shown."""
        closed = set(self.meta(WATCHED_CLOSED_KEY, []))
        add = set()
        for tid, hold in self.c.execute("SELECT clickup_task_id, hold_reason FROM tasks ORDER BY clickup_task_id"):
            if tid in terminal or hold:
                continue
            task = self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone()
            if self._observe_dependencies(tid, cards) is not False:
                continue
            for card, approval in engine._watched(self, tid).items():
                if card in closed:
                    # A current approved owner may reopen its existing card. Historical closure is not
                    # proof that its current native parents are empty; probe through this same bounded loop.
                    if (task['phase'] not in engine.AMENDMENT_PHASES or approval['state'] not in engine.WATCHED_STATES
                            or not rules.current(approval['accounts_gen'], task)):
                        continue
                    shown = cards.get(card) or self._show(card)
                    if shown is None or rules.status(shown) in ('done', 'archived'):
                        continue
                    cards[card] = shown
                    closed.remove(card)
                if card not in cards:
                    shown = self._show(card)
                    if shown is None:
                        continue
                    cards[card] = shown
                if rules.status(cards[card]) in ("done", "archived") and not self.c.execute(
                        "SELECT 1 FROM blockers WHERE card=? AND state NOT IN (%s) UNION SELECT 1 FROM fences"
                        " WHERE card=? AND state NOT IN (%s)" % (engine._in(engine.BLOCKER_FINAL),
                                                                 engine._in(engine.FENCE_FINAL)),
                        (card, card)).fetchone():
                    add.add(card)  # a W with a non-final blocker, fence or park is still evaluated (r11 §3.5)
                for p in cards[card].get("parents") or []:
                    if isinstance(p, str) and p not in cards and not self.c.execute(
                            "SELECT 1 FROM blockers WHERE question_card=? AND state IN (%s) UNION SELECT 1 FROM fences"
                            " WHERE fence_card=? AND state IN (%s)" % (engine._in(engine.BLOCKER_FINAL),
                                                                      engine._in(engine.FENCE_FINAL)),
                            (p, p)).fetchone():
                        shown = self._show(p)
                        if shown is not None:
                            cards[p] = shown
        if set(self.meta(WATCHED_CLOSED_KEY, [])) != closed | add:
            self.set_meta(WATCHED_CLOSED_KEY, sorted(closed | add))

    def _observe_dependencies(self, tid, cards):
        """One current native snapshot shared by question relay and recovery.

        Closed-card caches are hints only: required edges can be added later.
        False selects the legacy port path; None is unavailable native evidence,
        not affirmative detachment. Neither native result permits legacy fallback.
        """
        factory = getattr(self.port, '_preworker_snapshot', None)
        if factory is None:
            return False
        roots, forbidden = engine.dependency_scope(self.c, tid)
        snap = dict(stalled.context(self.c, tid), dependency_scope=[roots, sorted(forbidden)])
        self.dependency_members[tid] = {}
        if roots:
            if not _recovery_dependencies(snap, self.port, self.preworker_reader(tid)):
                return None
            cards.update(snap['shows'])
            self.dependency_members[tid] = snap['members']
        return True

    def _observe_recovery(self, cards, terminal):
        """r3 §4.1 while recovery is enabled: ``show`` each recovery member of a non-terminal, unheld task that is
        not yet in this scan's cards nor cached closed, and W of every live incident. A member observed
        done/archived without a live incident is cached and no longer shown."""
        if self.meta(engine.RECOVERY_KEY) is None:
            return
        closed = set(self.meta(RECOVERY_CLOSED_KEY, []))
        add = set()
        diagnostic_held = progress.held_tasks(self.c)
        for tid, hold in self.c.execute("SELECT clickup_task_id, hold_reason FROM tasks ORDER BY clickup_task_id"
                                        ).fetchall():
            if tid in terminal or (hold and tid not in diagnostic_held):
                continue
            if tid not in self.dependency_members:
                self._observe_dependencies(tid, cards)
            for b in self.c.execute("SELECT question_card FROM blockers WHERE task=?", (tid,)):
                if b[0] not in cards:
                    shown = self._show(b[0])
                    if shown is not None:
                        cards[b[0]] = shown
            live = {r[0] for r in self.c.execute("SELECT card FROM recoveries WHERE task=? AND state IN (%s)"
                                                 % engine._in(engine.RECOVERY_LIVE), (tid,))}
            from . import preworker
            early = [dict(r) for r in self.c.execute(
                "SELECT * FROM recoveries WHERE task=? AND class IN (?,?) AND state IN ('open','handed_off')",
                (tid, preworker.CLASS, 'progress_stall'))]
            for inc in early:
                if inc['class'] == preworker.CLASS or inc['card'].startswith('progress-'):
                    live.discard(inc['card'])  # tagged logical anchors are never native objects
                for ref, in self.c.execute("SELECT result_ref FROM outbox WHERE kind='kb_create' AND state='done'"
                                          " AND idem_key LIKE ?", (engine._rcv_key(tid, inc['card'], inc['run_id'], 0)[:-1] + '%',)):
                    if ref and ref not in cards:
                        shown = self._show(ref)
                        if shown is not None:
                            cards[ref] = shown
            for card in list(engine._recovery_members(self, tid)) + sorted(live):
                if card in closed and card not in live:
                    continue
                if card not in cards:
                    shown = self._show(card)
                    if shown is None:
                        continue
                    cards[card] = shown
                if card not in live and rules.status(cards[card]) in ("done", "archived"):
                    add.add(card)
                for parent in cards[card].get("parents") or []:
                    if isinstance(parent, str) and parent not in cards:
                        shown = self._show(parent)
                        if shown is not None:
                            cards[parent] = shown
        if add - closed:
            self.set_meta(RECOVERY_CLOSED_KEY, sorted(closed | add))

    def diagnostic_input(self, tid, reader=None):
        """Renew an expired diagnostic input lease with a real bounded L, never a timestamp write.
        Reuse this scan's request budget and ordinary observer commits. New activity invalidates the
        barrier/classification normally. Native checks follow and retain their post-I/O expiry.
        """
        t = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()
        now = self.store.clock()
        if t is None or t['terminal'] or (t['hold_reason'] and tid not in (
                stalled.held_notice_tasks(self.c) | progress.held_tasks(self.c))):
            return False
        if tid in self._diagnostic_refresh:
            return self._diagnostic_refresh[tid]
        if now - (t['last_light_ms'] or 0) <= 5 * 60_000:
            return True
        self.confirmed.discard(tid)  # the renewal must establish its own current home-list proof
        transport = self.guard.transport(_Capture(self.inner, self), task=tid)
        if reader is not None:
            transport = _PreworkerTransport(transport, reader)
        res = observe.light_pass(transport, self.store, self.cfg, tid, now)
        self._diagnostic_refresh[tid] = res.complete and tid in self.confirmed
        self.alerts.extend(res.alerts)
        return self._diagnostic_refresh[tid]

    def preworker_check(self, task, card, run, notice=False):
        return _preworker_check(lambda: recovery_snapshot(self.c, task, card, run), self.port,
                                card, run, self.store.clock, self.obs.scan_started_ms, notice=notice,
                                handoff_unblock=self.cfg.handoff_unblock, reader=self.preworker_reader(task))

    def recovery_gate(self, row, p):
        """r3 §5.2 wake send gate: one fresh ``show W`` and the §6.1 admission over the live state, plus no final
        ACK. ``defer`` when W is unreadable or the task held; ``stale`` otherwise unless CURRENT."""
        q = p["params"]
        snap = recovery_snapshot(self.c, q["task"], q["card"], q["run_id"])
        if q.get('class') == 'progress_stall' or (snap.get('incident') or {}).get('class') == 'progress_stall':
            reader = self.preworker_reader(q['task'])
            if not self.diagnostic_input(q['task'], reader=reader):
                return 'defer'
            verdict = _progress_check(lambda: recovery_snapshot(self.c, q['task'], q['card'], q['run_id']),
                                      self.port, q['card'], q['run_id'], self.store.clock,
                                      self.obs.scan_started_ms, reader=reader,
                                      handoff_unblock=self.cfg.handoff_unblock)
            return 'go' if verdict.code == 0 else 'defer' if verdict.code == 2 else 'stale'
        from . import preworker
        if (snap.get('incident') or {}).get('class') == preworker.CLASS:
            if not self.diagnostic_input(q['task'], reader=self.preworker_reader(q['task'])):
                return 'defer'
            verdict = self.preworker_check(q['task'], q['card'], q['run_id'])
            return 'go' if verdict.code == 0 else 'defer' if verdict.code == 2 or verdict.text == 'held' else 'stale'
        snap["members"] = engine.recovery_members(q["task"], snap["approvals"], snap["orch"])
        reader = self.preworker_reader(q['task'])
        if ((snap.get("incident") or {}).get("class") == stalled.CLASS
                and not self.diagnostic_input(q['task'], reader=reader)):
            return 'defer'
        diagnostic = (snap.get("incident") or {}).get("class") == stalled.CLASS
        w = (None if getattr(self.port, '_preworker_snapshot', None) else
             reader.read('show', q['card'], fresh=True)) if diagnostic else self._show(q['card'])
        if (snap.get("incident") or {}).get("class") == stalled.CLASS:
            _diagnostic_reads(snap, self.port, q["card"], w, reader)
            if not getattr(self.port, '_preworker_snapshot', None):
                reader.invalidate([q['card']])
                snap['shows'][q['card']] = reader.read('show', q['card'], fresh=True)
            w = snap['shows'].get(q['card'])
            snap['shows'][q['card']] = w
            latest = recovery_snapshot(self.c, q["task"], q["card"], q["run_id"])
            snap.update(latest)
            snap["members"] = engine.recovery_members(q["task"], snap["approvals"], snap["orch"])
            snap.update(now_ms=self.store.clock(), scan_started_ms=self.obs.scan_started_ms)
            a = next((a for a in snap['approvals'] if a['id'] == (snap.get('incident') or {}).get('approval_id')), None)
            if a is not None and not _diagnostic_complete(snap, q['card'], w, a):
                return 'defer'
        if getattr(self.port, '_preworker_snapshot', None):
            if not _recovery_dependencies(snap, self.port, self.preworker_reader(q['task'])):
                return 'defer'
            w = snap['shows'].get(q['card'])
        verdict = kp.recovery_verdict(snap, w, q["card"], q["run_id"])
        if verdict.verdict == "UNAVAILABLE" or verdict.text == "held":
            return "defer"
        if verdict.code != 0:
            return "stale"
        inc = snap['incident']
        wait = snap.get('diagnostic_wait') or {}
        after = wait.get('ack_s', -1) if wait.get('state') in ('answered', 'superseded') else -1
        ack, comment = stalled.diagnostic_ack(w, inc, after)
        question = stalled.question_projection(comment, inc) or stalled.legacy_question_projection(comment, inc)
        return "stale" if ack["final"] == "owner" and question is not None else "go"

    def attachments(self, card):
        """``attachments --json`` of a card with the SHA-256 of each stored file (cached per scan)."""
        if card not in self._att:
            r = self.port.attachments(card)
            if not _ok(r) or not isinstance(r.json, list):
                self._att_failed.add(card)
            out = []
            for a in r.json if _ok(r) and isinstance(r.json, list) else ():
                if isinstance(a, dict):
                    try:
                        with open(a["stored_path"], "rb") as f:
                            data = f.read()
                    except (KeyError, OSError, TypeError):
                        continue
                    out.append(dict(a, sha256=_sha(data), data=data))
            self._att[card] = out
        return self._att[card]

    def close_cards(self):
        """Request cards of terminal tasks that are done/archived need no further ``show``."""
        closed = set(self.meta(CLOSED_CARDS_KEY, []))
        add = {card for card, task in self.c.execute(
            "SELECT card_id, task FROM cards WHERE role='request' AND task IN"
            " (SELECT clickup_task_id FROM tasks WHERE terminal=1)")
            if rules.status(self.obs.cards.get(card)) in ("done", "archived")}
        if add - closed:
            self.set_meta(CLOSED_CARDS_KEY, sorted(closed | add))

    # ------------------------------------------------------------------------------- §4.1 step 7
    def row(self, key):
        r = self.c.execute("SELECT * FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def state(self, key):
        r = self.c.execute("SELECT state FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return r[0] if r else None

    def update(self, key, **cols):
        with self.store.transaction() as c:
            c.execute("UPDATE outbox SET %s WHERE idem_key=?" % ", ".join(k + "=?" for k in cols),
                      [*cols.values(), key])
        if "state" in cols:
            self.log.info(json.dumps({"event": "outbox", "key": key, "state": cols["state"]}))
        return True

    def hold(self, task, reason, alert):
        self.alerts.append(alert)
        if task:
            self.store.observe_commit(task, [], {"hold_reason": reason})

    def execute(self, alerts_only=False, only=None):
        """Run outbox rows in order (revocation audit stamps first), dependencies and preconditions first."""
        for _ in range(50):
            keys = [r[0] for r in self.c.execute(
                "SELECT idem_key FROM outbox WHERE state NOT IN (%s) AND kind NOT LIKE 'clickup_%%'"
                " ORDER BY (idem_key LIKE '%%:stamp:%%:revoked') DESC, rowid" % ",".join("?" * len(FINAL)), FINAL)]
            progressed = False
            for key in keys:
                if only is not None and key not in only:
                    continue
                row = self.row(key)
                if alerts_only and row["kind"] != "alert":
                    continue
                progressed = self.step(row) or progressed
            if not progressed:
                break

    def step(self, row):
        key = row["idem_key"]
        if row["state"] in FINAL:
            return False
        deps = [self.state(d) for d in json.loads(row["depends_on"] or "[]")]
        if any(s in DEAD for s in deps):
            return self.update(key, state="cancelled_stale", outcome="DEPENDENCY")
        if any(s != "done" for s in deps):
            return False
        kind = row["kind"]
        if kind == "alert":  # a task-less operator alert (the r12 §9 disable sentence) has no task to go stale
            if json.loads(row["preconditions"]).get("task_id") and not self.precheck(row):
                return self.update(key, state="cancelled_stale")
            self.out.append(json.loads(row["body"])["text"])
            return self.update(key, state="done", outcome="OK")
        if kind in CU_KINDS:
            return self.clickup(row)
        if kind in KB_KINDS:
            return self.kanban(row)
        return False

    def precheck(self, row, drop_wait=False):
        """§10.1 recheck against this scan's state (``rules.preconditions_ok``)."""
        pre = load_preconditions(json.loads(row["preconditions"]))
        if drop_wait:
            pre = dataclasses.replace(pre, wait_run_id=None)
        t = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (pre.task_id,)).fetchone()
        apps = {a["id"]: dict(a) for a in self.c.execute("SELECT * FROM approvals WHERE task=?", (pre.task_id,))}
        card = self.obs.cards.get(json.loads(row["body"] or "{}").get("card"))
        confirmed = self.cfg.list_id if pre.task_id in self.obs.confirmed else None
        b = getattr(pre, "blocker_id", None)
        b = t is not None and b is not None and self.c.execute("SELECT * FROM blockers WHERE id=?", (b,)).fetchone()
        return rules.preconditions_ok(dict(t) if t else None, apps, pre, row["kind"], card, confirmed,
                                      blocker=dict(b) if b else None)

    def preworker_stage_current(self, tid, approval):
        from . import preworker
        task = dict(self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone())
        approval = dict(approval)
        state = preworker.context(self.c, tid)
        binding = _preworker_binding(task, approval, state)
        reader = self.preworker_reader(tid)
        if (approval['id'] in self.obs.early_approvals and reader.binding == binding
                and reader.proof is not None and reader.proof.valid()
                and self.store.clock() - (task['last_light_ms'] or 0) <= 300000
                and rules.barrier_ok(task, 0, self.store.clock(), self.obs.scan_started_ms)):
            return True
        if not self.diagnostic_input(tid, reader=reader):
            return False
        cards, atts, complete = _preworker_collect(task, approval, state, reader, recheck=True)
        latest = dict(self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone())
        current = dict(self.c.execute('SELECT * FROM approvals WHERE id=?', (approval['id'],)).fetchone())
        valid = (complete and _preworker_binding(latest, current, preworker.context(self.c, tid)) == binding
                and reader.proof.valid() and self.store.clock() - (latest['last_light_ms'] or 0) <= 300000
                and preworker.inspect(latest, current, state, cards, atts,
                                                               self.store.clock(), self.obs.scan_started_ms) is not None)
        if valid:
            self.obs = dataclasses.replace(self.obs, early_approvals=self.obs.early_approvals | {approval['id']})
        return valid

    def send_gate(self, row, drop_wait=False):
        """Immediately before every send and retry: the §10.1 precondition recheck ('cancel'), the final
        reply-destination check ('foreign': a reply root must be an observed top-level comment of this task,
        §7.1/§8.3/§9), then the §4.4 barrier of a quiet-expired row ('defer': not sent, not an attempt)."""
        if not self.precheck(row, drop_wait):
            return "cancel"
        p = json.loads(row["body"] or "{}")
        tid = json.loads(row["preconditions"])["task_id"]
        blocker_id = json.loads(row['preconditions']).get('blocker_id')
        if blocker_id is not None and ':bq:' in row['idem_key'] and getattr(self.port, '_preworker_snapshot', None):
            b = self.c.execute('SELECT * FROM blockers WHERE id=? AND task=?', (blocker_id, tid)).fetchone()
            observed = {}
            if self._observe_dependencies(tid, observed) is None:
                return 'defer'
            if b is None or self.dependency_members[tid].get(b['card'], {}).get('id') != b['approval_id']:
                return 'cancel'
            w, q = observed.get(b['card']), observed.get(b['question_card'])
            if (not q or not w or kp.latest_run_id(w) != b['run_id']
                    or kp.protocol_parent(b['card'], w, q, self.meta(engine.CUTOVER_KEY))[1] != 'record'):
                return 'cancel'
        aid = json.loads(row['preconditions']).get('approval_id')
        if aid is not None:
            a = self.c.execute('SELECT * FROM approvals WHERE id=?', (aid,)).fetchone()
            if a and a['state'] in engine.preworker.STATES and any(
                    row['idem_key'] == base or row['idem_key'].startswith(base + ':') for base in engine._keys(tid, a)):
                t = self.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone()
                if t['classified_gen'] != t['input_gen'] or engine.preworker.pending_question(self, aid):
                    return 'defer'  # neither pending input nor a human wait authorizes stage writes
                if (not engine.preworker.current_plan(t, a) or t['phase'] not in engine.preworker.PHASES
                        or self.c.execute("SELECT 1 FROM approvals WHERE task=? AND id>? AND state IN (%s)" %
                                          engine._in(engine.ACTIVE), (tid, aid)).fetchone()):
                    return 'cancel'
        if (self.meta(engine.RECOVERY_KEY) is not None
                and aid is not None and a and a['state'] in engine.preworker.STATES
                and any(row['idem_key'] == base or row['idem_key'].startswith(base + ':')
                        for base in engine._keys(tid, a))
                and not self.preworker_stage_current(tid, a)):
            return 'defer'
        if row["kind"] == "cu_reply" and not self.c.execute(
                "SELECT 1 FROM items WHERE task=? AND kind='comment' AND id=?", (tid, str(p.get("root")))).fetchone():
            return "foreign"
        t = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()
        if not rules.send_barrier_ok(dict(t) if t else None, p, self.now, self.obs.scan_started_ms):
            return "defer"
        if p.get("recovery_notice"):
            return self.recovery_notice_gate(p["recovery_notice"], p.get("recovery_delivery_hold"))
        return "go"

    def recovery_notice_gate(self, iid, delivery_hold=None):
        """Immediately before new sends/retries: current authority and freshly observed stopped work."""
        inc = self.c.execute("SELECT * FROM recoveries WHERE id=?", (iid,)).fetchone()
        if not inc or self.meta(engine.RECOVERY_KEY) is None:
            return "cancel"
        if inc['class'] == 'progress_stall':
            reader = self.preworker_reader(inc['task'])
            if not self.diagnostic_input(inc['task'], reader=reader):
                return 'defer'
            def load_notice():
                snap = recovery_snapshot(self.c, inc['task'], inc['card'], inc['run_id'])
                if (delivery_hold and delivery_hold.startswith('outbox:')
                        and delivery_hold[7:].startswith(engine._rcv_key(inc['task'], inc['card'], inc['run_id'], 0)[:-1])
                        and self.state(delivery_hold[7:]) == 'failed'):
                    snap['delivery_hold'] = delivery_hold
                return snap
            verdict = _progress_check(load_notice,
                                      self.port, inc['card'], inc['run_id'], self.store.clock,
                                      self.obs.scan_started_ms, notice=True, reader=reader,
                                      handoff_unblock=self.cfg.handoff_unblock)
            return 'go' if verdict.code == 0 else 'defer' if verdict.code == 2 else 'cancel'
        if inc['class'] == 'approval_staging':
            if not self.diagnostic_input(inc['task'], reader=self.preworker_reader(inc['task'])):
                return 'defer'
            verdict = self.preworker_check(inc['task'], inc['card'], inc['run_id'], notice=True)
            return 'go' if verdict.code == 0 else 'defer' if verdict.code == 2 or verdict.text == 'held' else 'cancel'
        precise = inc['class'] == stalled.CLASS or engine._diagnostic_wait(self, inc['id']) is not None
        reader = self.preworker_reader(inc['task'])
        if precise and not self.diagnostic_input(inc['task'], reader=reader):
            return 'defer'
        w = (None if getattr(self.port, '_preworker_snapshot', None) else
             reader.read('show', inc['card'], fresh=True)) if precise else self._show(inc['card'])
        if w is None and not precise:
            return "defer"
        snap = None
        if precise:
            snap = recovery_snapshot(self.c, inc['task'], inc['card'], inc['run_id'])
            _diagnostic_reads(snap, self.port, inc['card'], w, reader)
            if not getattr(self.port, '_preworker_snapshot', None):
                reader.invalidate([inc['card']])
                snap['shows'][inc['card']] = reader.read('show', inc['card'], fresh=True)
            w = snap['shows'].get(inc['card'])
            snap['shows'][inc['card']] = w
            latest = recovery_snapshot(self.c, inc['task'], inc['card'], inc['run_id'])
            snap.update(latest)  # authority and required inputs may have changed during I/O
            inc = snap.get('incident')
            if not inc or inc['state'] != 'open' or snap.get('abandoned'):
                return 'cancel'
            if w is None:
                return 'defer'
        t = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (inc["task"],)).fetchone()
        if t is None or t["terminal"]:
            return "cancel"
        own_hold = (delivery_hold and t["hold_reason"] == delivery_hold and delivery_hold.startswith("outbox:")
                    and delivery_hold[7:].startswith(engine._rcv_key(inc["task"], inc["card"], inc["run_id"], 0)[:-1])
                    and self.state(delivery_hold[7:]) == "failed")
        if t["hold_reason"] and not own_hold:
            return "defer"
        a = self.c.execute("SELECT * FROM approvals WHERE id=?", (inc["approval_id"],)).fetchone()
        if (not a or (a["state"] not in engine.WATCHED_STATES and not
                      (own_hold and a["state"] == "revoked" and a["revoked_reason"] == "hold"))
                or not rules.current(a["accounts_gen"], t)
                or a["rev"] != inc["approval_rev"] or a["plan_sha256"] != inc["plan_sha256"]
                or (not own_hold and engine._recovery_members(self, inc["task"]).get(inc["card"], {}).get("id") != a["id"])):
            return "cancel"
        if self.c.execute("SELECT 1 FROM approvals WHERE task=? AND id>? AND state IN (%s)" %
                          engine._in(engine.ACTIVE + engine.WATCHED_STATES), (inc["task"], a["id"])).fetchone():
            return "cancel"
        wait = engine._diagnostic_wait(self, inc['id']) or {}
        handoff_run = (stalled.handoff(dict(t), a, inc, snap['shows'], snap, snap['attachments'])
                       if snap is not None and wait.get('takeover') else None)
        handoff_bound = handoff_run is not None and handoff_run == wait.get('question', {}).get('run')
        if (((kp.latest_run_id(w) or 0) != inc["run_id"] or kp.card_status(w) in ("running", "review", "done", "archived")
             or kp.open_run_ids(w)) and not handoff_bound):
            return "cancel"
        if handoff_bound and kp.worker_progress(w, self.store.clock()):
            return 'cancel'  # a live owner needs no takeover; this never authorizes native release
        if snap is not None:
            now = self.store.clock()  # never evaluate a freshness lease with the pre-I/O scan clock
            if (not rules.barrier_ok(t, 0, now, self.obs.scan_started_ms)
                    or now - (t['last_light_ms'] or 0) > 5 * 60_000
                    or not _diagnostic_complete(snap, inc['card'], w, a)):
                return 'defer'
            observed_task = dict(t, hold_reason=None) if own_hold else dict(t)
            wait = engine._diagnostic_wait(self, inc['id']) or {}
            if (wait.get('takeover') and
                    (handoff_bound
                     or (kp.recovery_outcome(w, inc['run_id'], inc['event_created_s'])[0]
                         in ('gated', 'handed_off')
                         and all(kp.card_status(snap['shows'][p]) in ('done', 'archived') for p in w['parents'])
                         and all(b['state'] in engine.BLOCKER_FINAL for b in snap['blockers'])
                         and all(f['state'] in engine.FENCE_FINAL for f in snap['fences'])))):
                if progress.prerequisite_status(inc['card'], snap, snap['shows']):
                    return 'defer'
                return 'go'  # only publish the unavailable-owner question; never release ready/gated work
            found = stalled.candidate(observed_task, a, inc['card'], snap['shows'], snap, snap['attachments'])
            stopped = found if inc['class'] == stalled.CLASS else kp.recovery_incident(w)
            if found is None or stopped is None or stopped['reason_sha256'] != inc['reason_sha256']:
                return 'cancel'
        return "go"

    def refuse(self, row, gate):
        """Apply a non-'go' send gate to an S2 row and its ClickUp mirrors; a deferral changes nothing."""
        if gate == "defer":
            return False
        key = row["idem_key"]
        if gate == "foreign":
            tid = json.loads(row["preconditions"])["task_id"]
            self.alerts.append("ClickUp reply outbox %s targets a thread that is not an observed comment of task %s;"
                               " not sent" % (key, tid))
        with self.store.transaction() as c:
            c.execute("UPDATE outbox SET state='cancelled_stale', outcome=? WHERE idem_key IN (?,?,?) AND state NOT IN"
                      " (%s)" % ",".join("?" * len(FINAL)),
                      ("FOREIGN_ROOT" if gate == "foreign" else "STALE", key, key + "#cu", key + "#cu-att") + FINAL)
        return True

    # -- staging (transient, per outbox key)
    def stage_dir(self, key):
        d = os.path.join(self.staging, _sha(key)[:16])
        os.makedirs(d, exist_ok=True)
        return d

    def stage(self, key, name, data, overwrite=False):
        """Write ``data`` once; retries reuse the same bytes (nonce envelopes stay stable for readback)."""
        path = os.path.join(self.stage_dir(key), name)
        if overwrite or not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            os.replace(tmp, path)
        return path

    def sweep_staging(self):
        """Remove staging dirs whose outbox rows are resolved (§2.1 ``staging/`` is transient). A ``failed`` or
        ``held_ambiguous`` row of a live task can still be resent (§10.2, §11), so it keeps its rendered bytes
        until it is done or abandoned (R8-F1): files already attached must read back unchanged."""
        if not os.path.isdir(self.staging):
            return
        keys = [r[0] for r in self.c.execute(
            "SELECT idem_key FROM outbox WHERE state NOT IN (%s) OR (state IN ('failed', 'held_ambiguous') AND task"
            " IN (SELECT clickup_task_id FROM tasks WHERE terminal=0))" % ",".join("?" * len(FINAL)), FINAL)]
        # a kept orchestrator/gate context row keeps the shared snapshot staging alive (card_context), and a kept
        # relay comment/attach row keeps its relay's context bundle (context_files, staged under the unblock key)
        live = {_sha(k)[:16] for k in keys} | {_sha(shared_context_key(k))[:16] for k in keys} \
            | {_sha(k.rsplit(":", 1)[0] + ":unblock")[:16] for k in keys if ":relay:" in k}
        for name in os.listdir(self.staging):
            if name not in live:
                shutil.rmtree(os.path.join(self.staging, name), ignore_errors=True)

    # ------------------------------------------------------------------------- ClickUp effects (S1)
    def clickup(self, row):
        key, p = row["idem_key"], json.loads(row["body"])
        pre = load_preconditions(json.loads(row["preconditions"]))
        tid = pre.task_id
        mirrors = [k for k in (key + "#cu-att", key + "#cu") if self.state(k) is not None]
        if not mirrors:
            if pre.list_id is not None and tid not in self.obs.confirmed:
                return False  # home list not confirmed this scan: defer, do not cancel
            gate = self.send_gate(row)
            if gate != "go":
                return self.refuse(row, gate)
            built = self.build_clickup(row, p)
            if built is None:
                return False
            if isinstance(built, dict):  # outbound filter rejection (§9): nothing is posted
                fed = self.filter_feedback(row, built["reason"])
                if fed is None:
                    return False  # its request card is not readable in this scan: decided in a later scan
                if fed == "held":
                    self.alerts.append(built["alert"])
                elif not fed:
                    self.update(key, state="failed", outcome="FILTERED")
                    self.hold(tid, "outbound_filter", built["alert"])
                return True
            with self.store.transaction() as c:
                for mkey, kind, target, body in built:
                    inserted = c.execute("INSERT OR IGNORE INTO outbox(idem_key, task, kind, target, payload_sha256, body,"
                              " preconditions, depends_on) VALUES (?,?,?,?,?,?,?,?)",
                              (mkey, tid, kind, target, _sha(json.dumps(body, sort_keys=True)),
                               json.dumps(body, sort_keys=True), row["preconditions"], "[]"))
                    if inserted.rowcount and p.get('recovery_notice') and row['attempts']:
                        # A replacement of a known-unsent stale notice keeps its actual spent send budget/backoff.
                        c.execute("UPDATE outbox SET attempts=?,next_attempt_ms=?,first_send_ms=?,state='retry_wait'"
                                  " WHERE idem_key=?", (row['attempts'], row['next_attempt_ms'], row['first_send_ms'], mkey))
            mirrors = [m[0] for m in built]
        cur = next((m for m in mirrors if self.state(m) != "done"), mirrors[-1])
        mrow = self.row(cur)
        if mrow["state"] in ("pending", "retry_wait") and pre.list_id is not None and tid not in self.obs.confirmed:
            return False
        if mrow["state"] == "pending" or (mrow["state"] == "retry_wait" and (mrow["next_attempt_ms"] or 0) <= self.now):
            gate = self.send_gate(row)  # the next step is a send or retry
            if gate != "go":
                return self.refuse(row, gate)
        before = (mrow["state"], mrow["attempts"], mrow["next_attempt_ms"])
        if mrow["state"] not in FINAL:
            res = clickup_effects.execute(self.guard.transport(self.inner, task=tid), self.store, self.cfg, cur,
                                          self.now, lambda _r: self.precheck(row))
            self.alerts.extend(res.alerts)
        mrow = self.row(cur)
        if mrow["state"] == "done" and cur != mirrors[-1]:
            return True  # next mirror (the comment after its long-text attachment) runs in the next pass
        self.update(key, state=mrow["state"], outcome=mrow["outcome"], attempts=mrow["attempts"],
                    next_attempt_ms=mrow["next_attempt_ms"], first_send_ms=mrow["first_send_ms"],
                    result_ref=mrow["result_ref"])
        return (mrow["state"], mrow["attempts"], mrow["next_attempt_ms"]) != before

    def build_clickup(self, row, p):
        """Mirror rows (key, S1 kind, target, body) for one S2 ClickUp row; None defers; a dict is a filter
        rejection ({"reason", "alert"})."""
        key, kind, tid = row["idem_key"], row["kind"], p["task"]
        mkey = key + "#cu"
        if kind == "cu_status":
            return [(mkey, "clickup_status", tid, {"status": p["status"]})]
        if kind == "cu_attach":
            data = self.card_file(p["card"], p["attachment"], p["sha256"])
            if data is None:
                self.alerts.append("plan %s for task %s is not readable on card %s; publication deferred"
                                   % (p["attachment"], tid, p["card"]))
                return None
            reason = render.check_outbound(data.decode("utf-8", "replace"), self.secrets)
            if reason:
                return {"reason": reason,
                        "alert": "ClickUp outbound filter hold: task %s (plan attachment); nothing was posted" % tid}
            rev = int(re.match(r"plan-r(\d+)-", p["filename"]).group(1))
            name = render.plan_attachment_name(rev, p["sha256"], mkey)
            return [(mkey, "clickup_attachment", tid, {"filename": name, "path": self.stage(mkey, name, data)})]
        text = p["text"]
        if "summary_from" in p:
            src = p["summary_from"]
            data = self.card_file(src["card"], src["attachment"], None)
            if data is None:
                return None
            text = text.replace("{summary}", plan_summary(data.decode("utf-8", "replace")))
        post = render.prepare_clickup_post(text, mkey, task_id=tid, secrets=self.secrets)
        if not post.ok:
            return {"reason": post.reason, "alert": post.alert}
        mk, target = ("clickup_reply", p["root"]) if kind == "cu_reply" else ("clickup_comment", tid)
        if post.attachment_name is None:
            return [(mkey, mk, target, {"text": post.comment})]
        akey = key + "#cu-att"
        aname = clickup_effects.attachment_name(post.attachment_name, akey)
        apath = self.stage(akey, aname, post.attachment_bytes)
        return [(akey, "clickup_attachment", tid, {"filename": aname, "path": apath}),
                (mkey, mk, target, {"text": post.comment.replace(post.attachment_name, aname)})]

    def card_file(self, card, name, sha):
        for a in self.attachments(card):
            if a.get("filename") == name and (sha is None or a["sha256"] == sha):
                return a["data"]
        return None

    def plan_bytes(self, task, sha):
        for card, in self.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC",
                                    (task,)).fetchall():
            for a in self.attachments(card):
                if a["sha256"] == sha:
                    return a["data"]
        return None

    # -------------------------------------------------- outbound filter feedback (OUTBOUND-PROSE-F1)
    def filter_feedback(self, row, reason):
        """A filtered planner output goes back to its own request card instead of a silent hold: in the same
        transaction that fails the output row, queue one Kanban comment naming the triggered rule (never the
        matched text) and asking for a corrected draft, plus a wake of the card (``feedback_wait``). The correction
        is a new output and passes the same filter. At most FILTER_FEEDBACK_MAX corrections per card and input
        generation. The wake records the card's latest run of this scan's show (the producing run, OP-Q2);
        ``feedback_wait`` moves it to the latest run that started before the comment was delivered (SPEC r16).
        "held": a producer that cannot take a correction, or no correction left, failed
        the row and held the task with a durable alert in one transaction (OP-Q1). None: the request card
        is not readable in this scan (nothing changed). False (the caller holds the task with its alert) for any
        other row."""
        key = row["idem_key"]
        m = _OUTPUT_KEY.fullmatch(key)
        if not m:
            return False
        tid, ref = m.group("task"), m.group("ref")
        seen = self.c.execute("SELECT s.key, s.card FROM kanban_seen s JOIN cards c ON c.card_id=s.card AND"
                              " c.task=? AND c.role='request' WHERE s.key LIKE ?", (tid, ref + "%")).fetchone()
        if seen is None:
            return False
        full, card = seen
        show = self.obs.cards.get(card)
        run = rules.latest_run(show)
        if run is None:
            self.alerts.append("ClickUp outbound filter: request card %s of task %s is not readable; its feedback is"
                               " deferred" % (card, tid))
            return None
        gen = self.c.execute("SELECT input_gen FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()[0]
        base = "clickup-dp:%s:filterfb:%s:g%d:" % (tid, card, gen)
        held = "clickup-dp:alert:%s:filterfb:%s:g%d:%s:held" % (tid, card, gen, ref)
        if rules.status(show) in NO_CORRECTION:
            return self.filter_hold(key, tid, held, _no_correction(card, rules.status(show), tid))
        n = self.c.execute("SELECT COUNT(*) FROM outbox WHERE idem_key LIKE ?", (base + "%:comment",)).fetchone()[0]
        if n >= FILTER_FEEDBACK_MAX or self.state(base + ref + ":comment") is not None:
            return self.filter_hold(key, tid, held, "ClickUp outbound filter: request card %s produced %d rejected"
                                    " drafts for task %s (gen %d); no automatic correction left; task on hold"
                                    % (card, n + 1, tid, gen))
        head, when = "comment", ""
        start = "a \"CLICKUP-OUT v1\" header with the same kind, relay and reply_to as the rejected output"
        for cm in (show or {}).get("comments") or []:
            if isinstance(cm, dict) and _sha(json.dumps([card, cm.get("author"), cm.get("created_at"),
                                                         cm.get("body")])) == full:
                hdr = rules.parse_out(cm.get("body"))[0]
                parts = ["kind=" + hdr["kind"]] + ["%s=%s" % (k, hdr[k]) for k in ("relay", "reply_to")
                                                   if _HEADER_VALUE.fullmatch(hdr.get(k) or "")]
                head = "\"CLICKUP-OUT v1 %s\"" % " ".join(parts)
                when = ", created_at %s" % cm.get("created_at")
                start = "exactly %s (the rejected output's header)" % head
        first = "CLICKUP-FILTER v1 output=%s rule=%s attempt=%d/%d" % (ref, reason, n + 1, FILTER_FEEDBACK_MAX)
        text = "\n".join([
            first,
            "Your output %s (output ref %s%s) was NOT posted to ClickUp and will never be sent: the integration's"
            " outbound filter rule \"%s\" matched %s. Nothing from it was relayed."
            % (head, ref, when, reason, FILTER_RULES.get(reason, "material that may not leave the integration")),
            "Post the corrected text as a NEW comment whose first line is %s, then call"
            " kanban_schedule(reason=\"CLICKUP_WAIT\"). The CLICKUP-FILTER line of this comment is not an output"
            " header: a comment that starts with it is never posted. The correction passes the same filter before"
            " anything is posted." % start,
            "Never post a secret value: name the credential or where it is kept (for example \"the BMC password in"
            " the vault\"), not the value. Keep the rest, but drop anything that later CLICKUP-IN relays or the"
            " ledger made obsolete.",
            "This is correction %d of %d for this card and generation; a further rejected draft holds the task for"
            " an operator." % (n + 1, FILTER_FEEDBACK_MAX)])
        live = dataclasses.asdict(Preconditions(task_id=tid, phases=rules.LIVE_PHASES))
        note = ("ClickUp outbound filter: task %s output %s from request card %s was not posted (%s); the planner was"
                " asked for a corrected draft (%d of %d)" % (tid, ref, card, reason, n + 1, FILTER_FEEDBACK_MAX))
        k = base + ref
        with self.store.transaction() as c:
            c.execute("UPDATE outbox SET state='failed', outcome='FILTERED' WHERE idem_key=?", (key,))
            for k_, kind, target, body, pre, deps in (
                    (k + ":comment", "kb_comment", card, {"card": card, "text": text}, live, []),
                    (k + ":unblock", "kb_unblock", card, {"card": card, "relay_line": first,
                                                          "template": "filter_feedback"},
                     dict(live, wait_run_id=run["id"]), [k + ":comment"]),
                    ("clickup-dp:alert:" + k[len("clickup-dp:"):], "alert", "operator", {"text": note},
                     dataclasses.asdict(Preconditions(task_id=tid)), [])):
                body = json.dumps(body, sort_keys=True)
                c.execute("INSERT OR IGNORE INTO outbox(idem_key, task, kind, target, payload_sha256, body,"
                          " preconditions, depends_on) VALUES (?,?,?,?,?,?,?,?)",
                          (k_, tid, kind, target, _sha(body), body, json.dumps(pre), json.dumps(deps)))
        self.log.info(json.dumps({"event": "outbox", "key": key, "state": "failed"}))
        return True

    def feedback_wait(self, row, card):
        """Wake gate of a filter feedback comment: 'go', 'wait' (no attempt), 'done' or 'held'. The wake targets the
        latest run that started before the feedback comment was delivered (its Kanban created_at; at least the
        producing run recorded when the feedback was created), stored as the row's wait_run_id: only a run that
        started at or after delivery surely saw the comment (SPEC r16). A run that started earlier can still read
        it: when its correction (``corrected``) is already there, the wake is done without an unblock.
        It waits while that run is still running or the card is in review. A run that saw the comment makes the
        §10.3 guard report the wake done, and ``uncorrected_feedback`` checks what it produced. A card that can no
        longer take a correction fails the wake and holds the task with a durable alert in one transaction."""
        shown = self.port.show(card)
        if not _ok(shown):
            return "wait"
        pre = json.loads(row["preconditions"])
        at = _feedback_at(shown.json, json.loads(row["body"])["relay_line"])
        if at is not None:
            wait = max([pre["wait_run_id"]] + [r["id"] for r in shown.json.get("runs") or [] if isinstance(r, dict)
                                               and isinstance(r.get("id"), int) and not (
                                                   isinstance(r.get("started_at"), int) and r["started_at"] >= at)])
            if wait != pre["wait_run_id"]:
                pre["wait_run_id"] = wait
                self.update(row["idem_key"], preconditions=json.dumps(pre))
            done = self.corrected(row, shown.json, at)  # a run read the feedback and corrected it already
            if done is None:
                return "wait"
            if done:
                self.update(row["idem_key"], state="done", outcome="CORRECTED")
                return "done"
        status, last = kp.card_status(shown.json), kp.latest_run_id(shown.json) or 0
        if status == "review" or (status == "running" and last == pre["wait_run_id"]):
            return "wait"
        if status in NO_CORRECTION or status is None:
            alert = "clickup-dp:alert:" + row["idem_key"][len("clickup-dp:"):-len(":unblock")] + ":unreachable"
            with self.store.transaction() as c:
                c.execute("UPDATE outbox SET state='failed', outcome='UNREACHABLE' WHERE idem_key=?", (row["idem_key"],))
                self.escalate(c, row["task"], alert, _no_correction(card, status, row["task"]))
            self.log.info(json.dumps({"event": "outbox", "key": row["idem_key"], "state": "failed"}))
            return "held"
        return "go"

    def filter_hold(self, key, tid, alert, text):
        """The filtered output row fails, and its task holds with the durable alert ``alert``, in one transaction."""
        with self.store.transaction() as c:
            c.execute("UPDATE outbox SET state='failed', outcome='FILTERED' WHERE idem_key=?", (key,))
            self.escalate(c, tid, alert, text)
        self.log.info(json.dumps({"event": "outbox", "key": key, "state": "failed"}))
        return "held"

    def escalate(self, c, tid, alert, text):
        """Inside the caller's transaction: hold ``tid`` for outbound_filter (live tickets revoked, as ``hold_task``)
        and queue the operator alert row ``alert`` once. The alert row is printed by the executor exactly once."""
        body = json.dumps({"text": text}, sort_keys=True)
        c.execute("UPDATE tasks SET hold_reason='outbound_filter' WHERE clickup_task_id=?", (tid,))
        revoke_live_tickets(c, tid, "hold", self.now)
        c.execute("INSERT OR IGNORE INTO outbox(idem_key, task, kind, target, payload_sha256, body, preconditions,"
                  " depends_on) VALUES (?,?,?,?,?,?,?,?)",
                  (alert, tid, "alert", "operator", _sha(body), body,
                   json.dumps(dataclasses.asdict(Preconditions(task_id=tid))), "[]"))

    def uncorrected_feedback(self):
        """A request card woken by filter feedback that ends its run (parked past the wake's wait run, or no
        longer able to take a correction) without a corrected output posted -> the task holds with one durable
        operator alert (no draft text, no further wake). Runs after this scan's engine pass consumed outputs."""
        for row in map(dict, self.c.execute("SELECT * FROM outbox WHERE kind='kb_unblock' AND state='done' AND"
                                            " idem_key LIKE 'clickup-dp:%:filterfb:%:unblock' ORDER BY rowid")):
            tid, card = row["task"], row["target"]
            base = row["idem_key"][:-len(":unblock")]
            alert = "clickup-dp:alert:" + base[len("clickup-dp:"):] + ":uncorrected"
            t = self.c.execute("SELECT terminal, hold_reason FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()
            if t is None or t[0] or t[1] or tid not in self.obs.confirmed or self.state(alert) is not None:
                continue
            show, wait = self.obs.cards.get(card), json.loads(row["preconditions"]).get("wait_run_id")
            run, status = rules.latest_run(show), rules.status(show)
            if not isinstance(wait, int) or not run or run["id"] <= wait or not (
                    status in NO_CORRECTION or (status == "scheduled" and run.get("outcome") == "scheduled")):
                continue
            # The correction is a later output (created at or after the feedback was delivered) under the
            # rejected output's kind/relay/reply_to that the engine took up; any other output does not discharge it
            at = _feedback_at(show, json.loads(row["body"])["relay_line"])
            if at is None or self.corrected(row, show, at) is not False:
                continue
            with self.store.transaction() as c:  # hold (as ``hold_task``) and its durable alert commit together
                self.escalate(c, tid, alert, "ClickUp outbound filter: request card %s ended its run after the feedback"
                              " on output %s of task %s without a corrected draft; nothing was posted; task on hold"
                              % (card, base.rsplit(":", 1)[1], tid))

    def corrected(self, row, show, at):
        """Whether the filter feedback wake ``row`` has its correction: a planner output on the card created at or
        after the feedback was delivered (``at``) whose kind, relay and reply_to equal the rejected output's (the
        header the feedback asks for) and that the engine took up as an output row; that row then posts it, or
        (filtered again) gets its own bounded feedback. None: such an output is not consumed by the engine yet
        (decided in a later scan). Without the rejected comment in view nothing matches (False)."""
        tid, card, ref = row["task"], row["target"], row["idem_key"][:-len(":unblock")].rsplit(":", 1)[1]
        mine, outs = None, []
        for cm in (show or {}).get("comments") or []:
            if not isinstance(cm, dict) or cm.get("author") != "planner":
                continue
            parsed = rules.parse_out(cm.get("body"))
            if parsed is None:
                continue
            full = _sha(json.dumps([card, cm.get("author"), cm.get("created_at"), cm.get("body")]))
            ident = tuple(parsed[0].get(k) for k in ("kind", "relay", "reply_to"))
            if full[:12] == ref:
                mine = ident
            elif isinstance(cm.get("created_at"), int) and cm["created_at"] >= at:
                outs.append((full, ident))
        result = False
        for full, ident in outs:
            if mine is None or ident != mine:
                continue
            if not self.c.execute("SELECT 1 FROM kanban_seen WHERE key=?", (full,)).fetchone():
                result = None
            elif any(self.state("clickup-dp:%s:%s:%s" % (tid, k, full[:12])) is not None
                     for k in ("out", "q:business", "q:technical")):
                return True
        return result

    # -------------------------------------------------------------------------- Kanban effects (S3)
    def kanban(self, row):
        key, kind, p = row["idem_key"], row["kind"], json.loads(row["body"])
        if row["state"] in ("retry_wait", "ambiguous") and (row["next_attempt_ms"] or 0) > self.now:
            return False
        q = getattr(load_preconditions(json.loads(row["preconditions"])), "question_card", None)
        fence = kind == "kb_complete" and (":fence:" in key or ":park:" in key) and self.c.execute(
            "SELECT * FROM fences WHERE release_key=?", (key,)).fetchone()
        if (q or fence) and row["state"] in ("sending", "ambiguous"):  # r6 "Reconcile before refusal" (r9 §4.4)
            if row["child_pid"] and kp.child_alive(row["child_pid"], row["child_start_time"]):
                return False
            seen, shown = kp._readback(self.port, self.kb_intent(row, p))
            if seen is None:
                return False
            if seen:
                self.late_children(row, q, shown)
                return self.update(key, state="done", outcome="OK")
        if row["target"] is None and p.get("carrier_from"):  # r3 §5.1: subscribe/wake target the created carrier
            row = dict(row, target=self.row(p["carrier_from"])["result_ref"])
            self.update(key, target=row["target"])
        gate = self.send_gate(row, drop_wait=kind == "kb_unblock")
        if p.get("template") == "recovery_wake":
            gate = self.recovery_gate(row, p) if gate == "go" else gate
            if gate in ("cancel", "stale"):  # never completed: archived silently (r3 §5.2)
                with self.store.transaction():
                    engine._Ctx(self.store, self.now, self.obs, self.cfg).kb(
                        p["params"]["task"], key[:-len(":wake")] + ":archive", "kb_archive", row["target"])
        if gate == "go" and fence:  # r11 §3.3 fence send gate; r12 §3.3: fresh reads at every send, P then W
            gate = kp.fence_gate(self._show(fence["fence_card"]), fence["card"])
            w_show = self._show(fence["card"]) if fence["kind"] == "park" else None
            if fence["kind"] == "park" and (fence["fence_card"] not in rules.parents(w_show) or w_show is None
                                            or kp.open_run_ids(w_show)):
                gate = "stale"
        if gate == "go" and q and kind in ("kb_complete", "kb_comment"):
            b = self.c.execute("SELECT * FROM blockers WHERE question_card=?", (q,)).fetchone()
            w = b["card"]
            if kind == "kb_comment":
                gate = "stale" if b["state"] in engine.BLOCKER_FINAL else \
                    kp.correction_gate(self.obs.cards.get(q), self.obs.cards.get(w), q)
            else:
                gate = kp.answer_gate(self.obs.cards.get(q), self.obs.cards.get(w), q, w, b["run_id"])
            if gate == "children":
                kids = ", ".join(map(str, (self.obs.cards.get(q) or {}).get("children") or []))
                self.alerts.append("question card %s for %s has other children (%s); the answer is not delivered"
                                   " and %s keeps waiting" % (q, w, kids, w))
        if gate != "go":
            return self.refuse(row, gate)
        if p.get("template") == "filter_feedback":
            wait = self.feedback_wait(row, row["target"])
            if wait != "go":
                return wait != "wait"
            row = self.row(key)
        intent = self.kb_intent(row, p)
        if intent is None:
            return False
        possibly = row["state"] in ("sending", "ambiguous")
        prior = (row["child_pid"], row["child_start_time"]) if possibly else None
        attempts = row["attempts"] + 1
        self.update(key, state="sending", attempts=attempts, first_send_ms=row["first_send_ms"] or self.now)
        self.port.on_spawn = lambda pid, st: self.update(key, child_pid=pid, child_start_time=st)
        try:
            if kind == "kb_unblock" and p.get("ticket_lease_retry"):
                res = self.retry_ticket_lease(row, p, prior)
            elif kind == "kb_unblock" and p.get("expect_status") == "blocked":
                res = self.release(intent.target, prior)
            elif kind == "kb_attach" and p.get("template") == "context" and not p.get("card_role"):
                res = self.attach_all(intent, prior)
            elif p.get('template') == 'recovery_wake' and p.get('params', {}).get('class') == 'progress_stall':
                def before_send():
                    current = self.row(key)
                    gate = self.send_gate(current)
                    return self.recovery_gate(current, p) if gate == 'go' else gate
                res = kp.run_kanban_effect(self.port, intent, prior_child=prior,
                                           before_send=before_send if prior is not None else None)
            else:
                res = kp.run_kanban_effect(self.port, intent, prior_child=prior)
        finally:
            self.port.on_spawn = None
        st = res.state
        if st is RowState.DONE:
            self.late_children(row, q, res.json)
            ref = res.json.get("id") if kind == "kb_create" and isinstance(res.json, dict) else None
            return self.update(key, state="done", outcome="OK", result_ref=ref)
        if st is RowState.CANCELLED_STALE:
            return self.update(key, state="cancelled_stale", outcome="STALE")
        if st is RowState.SENDING:  # the earlier child is still alive, or the progress proof is unavailable
            if p.get('template') == 'recovery_wake' and p.get('params', {}).get('class') == 'progress_stall':
                self.update(key, state=row['state'], attempts=row['attempts'], first_send_ms=row['first_send_ms'])
                return False  # no request was sent; preserve ambiguous history without spinning this scan
            return self.update(key, attempts=row["attempts"])
        if attempts >= MAX_ATTEMPTS:
            self.update(key, state="failed", outcome=st.value)
            self.hold(json.loads(row["preconditions"])["task_id"], "outbox:" + key,
                      "Kanban %s outbox %s failed after %d attempts; task on hold" % (kind, key, attempts))
            # Informational delivery only: preserve the hold and ticket revocation, never resume work.
            if ":rcv:" in key:
                for inc in map(dict, self.c.execute("SELECT * FROM recoveries WHERE task=? AND state='open'",
                                                   (row["task"],)).fetchall()):
                    if key.startswith(engine._rcv_key(row["task"], inc["card"], inc["run_id"], 0)[:-1]):
                        with self.store.transaction():
                            engine._escalate(engine._Ctx(self.store, self.now, self.obs, self.cfg), row["task"], inc,
                                             "diagnostic wake delivery failed after bounded retries",
                                             delivery_hold="outbox:" + key)
            return True
        nxt = self.now + KB_BACKOFF_MIN[attempts - 1] * 60_000 if st is RowState.RETRY_WAIT else self.now
        return self.update(key, state=st.value, outcome=st.value, next_attempt_ms=nxt)

    def late_children(self, row, q, shown):
        """r9 L4 post-release children readback: when the show of Q that confirmed this ``complete Q`` row lists a
        child other than W (linked after the send gate), queue one durable deduplicated operator alert, before the
        row is recorded done. The applied delivery stands and is never sent again."""
        if not q or row["kind"] != "kb_complete" or not isinstance(shown, dict):
            return
        b = self.c.execute("SELECT id, task, card FROM blockers WHERE question_card=?", (q,)).fetchone()
        kids = [c for c in shown.get("children") or [] if c != b["card"]] if b else []
        if kids:
            with self.store.transaction():
                engine._Ctx(self.store, self.now, self.obs, self.cfg).alert(
                    "blocker:%d:children" % b["id"], "question card %s for %s was completed with its answer while"
                    " other cards were linked to it (%s): they may have resumed with the answer for %s (protocol"
                    " violation)" % (q, b["card"], ", ".join(map(str, kids)), b["card"]), b["task"])

    def retry_ticket_lease(self, row, p, prior):
        """Resume only this exact expired stop, after fresh ticket and native-parent checks.

        This is a same-card continuation, not promotion authority: the worker still runs ticket-check
        immediately before its effect. Use the ordinary outbox, child journal, backoff and native unblock.
        """
        if prior is not None and kp.child_alive(*prior):
            return kp.EffectResult(RowState.SENDING)
        card, binding, stop = row["target"], p["ticket_lease_retry"], p["stop"]
        show = self._show(card)
        if show is None:
            return kp.EffectResult(RowState.SENDING)
        if (show.get("task") or {}).get("id") != card:
            return kp.EffectResult(RowState.CANCELLED_STALE)
        if prior is not None and kp.recovery_outcome(show, stop["run_id"], stop["event_created_s"])[0]:
            return kp.EffectResult(RowState.DONE)  # lost ACK: readback only, never another unblock
        if kp.ticket_lease_stop(show) != stop:
            return kp.EffectResult(RowState.CANCELLED_STALE)
        parents = show.get("parents")
        if not isinstance(parents, list) or binding["gate_card"] not in parents:
            return kp.EffectResult(RowState.CANCELLED_STALE)
        observed = {}
        for parent in parents:
            if not isinstance(parent, str) or not kp._CARD_ID.fullmatch(parent):
                return kp.EffectResult(RowState.CANCELLED_STALE)
            observed[parent] = self._show(parent)
            if observed[parent] is None:
                return kp.EffectResult(RowState.SENDING)
            if (observed[parent].get("task") or {}).get("id") != parent:
                return kp.EffectResult(RowState.CANCELLED_STALE)
            if kp.card_status(observed[parent]) not in ("done", "archived"):
                return kp.EffectResult(RowState.SENDING)
        # Parent reads may be slow: a replacement run or newly linked parent cannot inherit
        # the earlier observation's permission to retry.
        show = self._show(card)
        if show is None:
            return kp.EffectResult(RowState.SENDING)
        if ((show.get("task") or {}).get("id") != card or kp.ticket_lease_stop(show) != stop
                or show.get("parents") != parents):
            return kp.EffectResult(RowState.CANCELLED_STALE)
        snap = load_ticket_snapshot(self.cfg.state_path, row["task"], self.cfg.ticket_lease_seconds)
        if snap is None:
            return kp.EffectResult(RowState.SENDING)
        a = next((a for a in snap.approvals if a["id"] == binding["id"]), None)
        if a is None or any(a[k] != v for k, v in binding.items()):
            return kp.EffectResult(RowState.CANCELLED_STALE)
        g = engine._gates(a)
        if (not g or g["promotion"] != card or g["plan_revision"] != a["rev"]
                or g["plan_sha256"] != a["plan_sha256"]
                or not kp.promotion_guarded(show, a["gate_card"],
                                           render.ticket_marker(a["gate_card"], row["task"], a["rev"], a["plan_sha256"]))):
            return kp.EffectResult(RowState.CANCELLED_STALE)
        now = self.store.clock()  # reads can consume the lease; do not use the scan's start time
        verdict = ticket_verdict(snap, observed[a["gate_card"]], row["task"], a["rev"], a["plan_sha256"],
                                 a["gate_card"], now)
        if not verdict.valid:
            return kp.EffectResult(RowState.SENDING if verdict.reason == "lease_expired" else RowState.CANCELLED_STALE)
        if (row["task"] not in self.obs.confirmed
                or not rules.send_barrier_ok(snap.task, p, now, self.obs.scan_started_ms)):
            return kp.EffectResult(RowState.SENDING)
        result = self.port.unblock(card)
        if not _ok(result):
            return kp._failed(result)
        back = self._show(card)
        applied = back is not None and kp.recovery_outcome(back, stop["run_id"], stop["event_created_s"])[0]
        return kp.EffectResult(RowState.DONE if applied else RowState.AMBIGUOUS)

    def release(self, card, prior=None):
        """Initial release of a blocked, never-run own card (§8.1 request pickup, §8.4 step 3): ``unblock``
        then a ready/todo/running readback. A card already past ``blocked`` was released earlier. While the
        recorded child of an earlier attempt is alive nothing is sent (§10.2); after it exits, the card is read."""
        if prior is not None and kp.child_alive(*prior):
            return kp.EffectResult(RowState.SENDING)
        shown = self.port.show(card)
        if not _ok(shown):
            return kp.EffectResult(RowState.RETRY_WAIT)
        status, runs = kp.card_status(shown.json), (shown.json.get("runs") or [])
        if status in (None, "archived", "triage"):
            return kp.EffectResult(RowState.CANCELLED_STALE, shown.json)
        if status != "blocked" or runs:
            return kp.EffectResult(RowState.DONE, shown.json)
        res = self.port.unblock(card)
        if not _ok(res):
            return kp.EffectResult(RowState.AMBIGUOUS if res.outcome is Outcome.AMBIGUOUS else RowState.RETRY_WAIT)
        back = self.port.show(card)
        landed = _ok(back) and kp.card_status(back.json) in ("ready", "todo", "running")
        return kp.EffectResult(RowState.DONE if landed else RowState.AMBIGUOUS, back.json)

    def attach_all(self, intent, prior):
        """§8.1/§8.2 context step: context snapshot, ledger, latest passed plan and staged files, each read back."""
        for name, path in intent.payload["files"]:
            one = EffectIntent(intent.idem_key, kp.ATTACH, intent.target, {"path": path, "name": name},
                               intent.preconditions)
            res = kp.run_kanban_effect(self.port, one, prior_child=prior or (None, None))
            if res.state is not RowState.DONE:
                return res
        return kp.EffectResult(RowState.DONE)

    def kb_intent(self, row, p):
        """S3 ``EffectIntent`` for one S2 Kanban row (payload rendering; None defers)."""
        key, kind, card = row["idem_key"], row["kind"], row["target"]
        pre = load_preconditions(json.loads(row["preconditions"]))
        tid = pre.task_id
        if kind == "kb_create":
            body = self.card_body(tid, p)
            if body is None:
                return None
            payload = {"title": p["title"], "body_file": self.stage(key, "body.md", body),
                       "idempotency_key": p["idempotency_key"], "assignee": p.get("assignee"),
                       "skills": p.get("skills") or [], "initial_status": p.get("initial_status", "blocked")}
        elif kind == "kb_comment":
            if p.get("template") == "relay":
                body = self.relay_body(row, p)
                if body is None:
                    return None
            elif p.get("template") == "blocker_relay":
                body = render.blocker_relay_comment(p["relay"], p["blockers"], p["plan_rev"], p["plan_sha256"],
                                                    p["context"])
            else:
                body = p["text"]
            payload = {"body": body}
        elif kind == "kb_attach":
            if p.get("card_role"):  # §8.2 on the orchestrator/gate: exactly one file per row
                path = self.card_context(row, p)
                if path is None:
                    return None
                payload = {"path": path, "name": p["name"]}
            elif p.get("template") == "context":
                files = self.context_files(row, p)
                if files is None:
                    return None
                payload = {"files": files}
            elif p.get("template") == "approval_record":
                a = self.c.execute("SELECT * FROM approvals WHERE id=?", (pre.approval_id,)).fetchone()
                q = p["params"]
                text = render.approval_record(q["task"], q["rev"], q["sha256"], q["accounts_gen"], a["gate_card"], {
                    "author_name": q["author_name"], "author_id": q["author_id"], "comment_id": q["comment_id"],
                    "comment_date_ms": q["comment_date"]}, self.cfg.auto_gate)
                payload = {"path": self.stage(key, p["name"], text), "name": p["name"]}
            else:
                data = self.card_file(p["source_card"], p["attachment"], p["sha256"])
                if data is None:
                    self.alerts.append("plan %s is not readable on card %s; handoff deferred"
                                       % (p["attachment"], p["source_card"]))
                    return None
                payload = {"path": self.stage(key, p["name"], data), "name": p["name"]}
        elif kind == "kb_unblock":
            line = None
            for d in json.loads(row["depends_on"] or "[]"):
                if d.endswith(":comment"):
                    line = json.loads(self.row(d)["body"]).get("text")
            payload = {"relay_line": p.get("relay_line", line)}  # a filter feedback wake names its comment's line
        elif kind == "kb_complete" and p.get("template") == "recovery_wake":
            payload = {"summary": render.recovery_summary(p["params"], card), "metadata": None}
        elif kind == "kb_complete":
            payload = {"summary": p["summary"], "metadata": p.get("metadata")}
        elif kind == "kb_subscribe" and p.get("delivery_mode") == "wake":
            payload = {"mode": "wake"}
        else:
            payload = {}
        return EffectIntent(key, KB_KINDS[kind], card, payload, pre, ())

    # -- rendering
    def approval_view(self, approval_id):
        a = dict(self.c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())
        return a, {"author_name": a["author_name"], "author_id": a["author_id"], "comment_id": a["comment_id"],
                   "comment_date_ms": a["comment_date"]}

    def latest_pass(self, tid):
        for card, in self.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC",
                                    (tid,)).fetchall():
            ev = rules.pass_evidence(self.obs.cards.get(card), self.obs.attachments.get(card), tid)
            if ev:
                return card, ev
        return None

    def card_body(self, tid, p):
        q = p["params"]
        if p["template"] == "recovery_card":
            return render.recovery_card_body(q)
        t = dict(self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone())
        if p["template"] == "request_card":
            last = self.latest_pass(tid)
            rev = max(last[1]["rev"] if last else 0, t["published_rev"] or 0)
            return render.request_card_body(tid, q["seq"], t["input_gen"], rev + 1, q.get("previous_card"),
                                            "clickup-plan-r%d.md" % last[1]["rev"] if last else None)
        a, view = self.approval_view(q["approval_id"])
        if p["template"] == "gate_card":
            return render.gate_card_body(tid, q["rev"], q["sha256"], a["accounts_gen"], view, self.cfg.auto_gate)
        data = self.plan_bytes(tid, q["sha256"])
        if data is None:
            self.alerts.append("plan r%d bytes for task %s are not readable; handoff deferred" % (q["rev"], tid))
            return None
        prev = None
        if q.get("lineage"):
            last = q["lineage"][-1]
            prev = dict(last.get("clickup_gates") or {}, gate=last.get("gate_card"))
        return render.orchestrator_card_body(tid, q["rev"], "clickup-plan-r%d.md" % q["rev"], len(data), q["sha256"],
                                             a["accounts_gen"], q["gate"], view, self.cfg.auto_gate, prev,
                                             protocol=self.meta(engine.CUTOVER_KEY) is not None)  # r9 §4.1, §7

    def read_snapshot(self, tid, d):
        """Complete current task content (GET Task, every comment page, replies of threaded roots); None defers.

        The reads are resumable (QSR F3): progress is kept in ``d/snapshot.json`` and continues in later scans
        within this task's per-scan request share (§4.5), so a supported history larger than one scan's
        budget still completes, untruncated. Partial progress is bound to the task's current quiet window:
        any newly observed activity moves ``quiet_until_ms`` and restarts the reads, so a completed snapshot
        was read entirely after the last activity (§4.4). The progress file is removed once complete."""
        prog = os.path.join(d, "snapshot.json")
        quiet = self.c.execute("SELECT quiet_until_ms FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()[0] or 0
        st = None
        if os.path.exists(prog):
            with open(prog, encoding="utf-8") as f:
                st = json.load(f)
        if not st or st["quiet"] != quiet:
            st = {"quiet": quiet, "task": None, "roots": [], "pages": 0, "next": None, "paged": False, "replies": {}}
        t, complete = self.guard.transport(self.inner, task=tid), False
        try:
            if st["task"] is None:
                r = t.request("GET", "/v2/task/%s" % tid, params={"include_markdown_description": "true"})
                if not _ok(r) or not isinstance(r.json, dict):
                    return None
                st["task"] = r.json
            while not st["paged"]:
                if st["pages"] >= self.cfg.budgets["comment_pages_cap"]:
                    return None
                params = {} if st["next"] is None else {"start": st["next"][0], "start_id": st["next"][1]}
                r = t.request("GET", "/v2/task/%s/comment" % tid, params=params)
                if not _ok(r) or not isinstance(r.json, dict) or not isinstance(r.json.get("comments"), list):
                    return None
                page = [cm for cm in r.json["comments"] if isinstance(cm, dict)]
                st["roots"] += page
                st["pages"] += 1
                if len(r.json["comments"]) < COMMENT_PAGE:
                    st["paged"] = True
                else:
                    st["next"] = [page[-1].get("date"), page[-1].get("id")]
            for cm in st["roots"]:
                if int(cm.get("reply_count") or 0) > 0 and str(cm["id"]) not in st["replies"]:
                    r = t.request("GET", "/v2/comment/%s/reply" % cm["id"])
                    if not _ok(r) or not isinstance(r.json, dict) or not isinstance(r.json.get("comments"), list):
                        return None
                    st["replies"][str(cm["id"])] = [x for x in r.json["comments"] if isinstance(x, dict)]
            complete = True
            return st["task"], st["roots"], st["replies"]
        finally:
            if complete:
                if os.path.exists(prog):
                    os.remove(prog)
            else:
                tmp = prog + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f)
                os.replace(tmp, prog)

    def snapshot(self, tid, gen, snap, staged):
        """(§8.2 context snapshot dict, item views) from a complete ``read_snapshot`` result."""
        task, roots, replies = snap
        comments = []
        for raw in sorted(roots, key=lambda x: (observe._int(x.get("date"), 0), str(x.get("id")))):
            comments.append(self._item_view(tid, "comment", raw, None))
            for rep in replies.get(str(raw["id"]), []):
                comments.append(self._item_view(tid, "reply", rep, str(raw["id"])))
        t = dict(self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone())
        return {"task_id": tid, "gen": gen, "name": task.get("name"),
                "description": task.get("markdown_description") or task.get("description"),
                "requester_id": t["requester_id"], "requester_name": self.cfg.approvers.get(t["requester_id"]),
                "attachments": [{"id": s["id"], "title": s["title"], "size": s["size"], "author_id": s["user_id"],
                                 "date_ms": observe._int(s["date"]), "staged": s["staged"]} for s in staged],
                "comments": comments, "notices": []}, comments

    def card_context(self, row, p):
        """§8.2 file for an orchestrator/gate ``kb_attach`` row (``card_role``): ``clickup-ledger-g<G>.md`` or the
        complete ``clickup-context-g<G>.md`` snapshot (attachment metadata only; one file per row). Both cards
        of one approval share the generation's snapshot, staged under the gate's context row key and kept until
        both rows are final (``sweep_staging``)."""
        tid, gen = p["task"], p["gen"]
        if p["template"] == "ledger":
            return self.stage(row["idem_key"], p["name"], self.ledger(tid))
        skey = shared_context_key(row["idem_key"])
        d = self.stage_dir(skey)
        path = os.path.join(d, p["name"])
        if not os.path.exists(path):
            snap = self.read_snapshot(tid, d)
            if snap is None:
                return None
            meta = [{"id": str(x.get("id")), "title": x.get("title"), "size": x.get("size"),
                     "user_id": (x.get("user") or {}).get("id"), "date": x.get("date"), "staged": None}
                    for x in snap[0].get("attachments") or [] if isinstance(x, dict)]
            self.stage(skey, p["name"], render.context_file(self.snapshot(tid, gen, snap, meta)[0]))
        return path

    def _item_view(self, tid, kind, raw, parent):
        it = self.c.execute("SELECT * FROM items WHERE task=? AND kind=? AND id=?", (tid, kind, str(raw["id"]))).fetchone()
        parsed = parse_comment(raw.get("comment"), raw.get("comment_text"), self.cfg.plain_attr_allowlist)
        cr = (it["command_result"] if it else None) or ""
        uid = (raw.get("user") or {}).get("id")
        return {"kind": kind, "id": str(raw["id"]), "parent_id": parent, "author_id": uid,
                "date_ms": observe._int(raw.get("date")), "addressed": self.cfg.bot_user_id in parsed.mentions,
                "shape": parsed.shape, "closure": cr[len("closure_"):] if cr.startswith("closure_") else None,
                "text": raw.get("comment_text") or ""}

    def relay_tag(self, att, ctx_name):
        """Name suffix for one relay's ledger, plan copy and staged files on its request card (E2E-F2): the
        installed CLI stores a repeated attachment name as ``name (n).ext``, so the exact-name readback (§10.2)
        needs names no other relay on the card uses. The card's first relay keeps the plain names its body
        references; any later relay (an earlier relay or blocker relay attach row exists for the card) uses its
        own context file's stem, ``-g<G>[-b<k>]`` or ``-<blocker relay ref>``. Decided by outbox insertion order,
        so it is stable across retries."""
        earlier = att and self.c.execute(
            "SELECT 1 FROM outbox WHERE kind='kb_attach' AND target=? AND (idem_key LIKE '%:relay:%:attach'"
            " OR idem_key LIKE '%:blocker:%:attach') AND rowid < (SELECT rowid FROM outbox WHERE idem_key=?)",
            (att["target"], att["idem_key"])).fetchone()
        return "-" + os.path.splitext(ctx_name)[0].replace("clickup-context-", "", 1) if earlier else ""

    def context_files(self, row, p):
        """[(name, path)] for the request-card context step, rendered once per relay (stable bytes). The context
        file carries the relay attach row's payload ``name`` (a re-staged first wake gets a new, never overwritten
        name) and the relay comment names that same file. The ledger, plan copy and staged files are attached
        under this relay's own names (``relay_tag``), and the context file names the staged files that way."""
        tid, gen = p["task"], p["gen"]
        base = row["idem_key"].rsplit(":", 1)[0]
        ctx_key = base + ":unblock"
        att = self.row(base + ":attach")
        name = (json.loads(att["body"]).get("name") if att else None) or render.context_name(gen)
        tag = self.relay_tag(att, name)

        def own(n):
            root, ext = os.path.splitext(n)
            return root + tag + ext
        d = self.stage_dir(ctx_key)
        ctx = os.path.join(d, name)
        ledger = os.path.join(d, "clickup-ledger.md")
        files = []
        if not os.path.exists(ctx):
            snap = self.read_snapshot(tid, d)
            if snap is None:
                return None
            atts = [a for a in snap[0].get("attachments") or [] if isinstance(a, dict)]
            staged = clickup_api.stage_attachments(atts, d, self.cfg.bot_user_id, opener=self.opener)
            snapshot, comments = self.snapshot(tid, gen, snap, [dict(s, staged=s["staged"] and own(s["staged"]))
                                                                 for s in staged])
            # the context file is written last: its presence means the whole bundle is complete
            self.stage(ctx_key, "items.json", json.dumps(comments), overwrite=True)
            self.stage(ctx_key, "staged.json", json.dumps([s["staged"] for s in staged]), overwrite=True)
            self.stage(ctx_key, "clickup-ledger.md", self.ledger(tid), overwrite=True)
            self.stage(ctx_key, name, render.context_file(snapshot))
        files.append((name, ctx))
        files.append((own("clickup-ledger.md"), ledger))
        last = self.latest_pass(tid)
        if last:
            data = self.card_file(last[0], last[1]["attachment"], last[1]["sha256"])
            if data is None:
                return None
            name = own("clickup-plan-r%d.md" % last[1]["rev"])
            files.append((name, self.stage(row["idem_key"], name, data)))
        with open(os.path.join(d, "staged.json"), encoding="utf-8") as f:
            for s in json.load(f):
                if s:
                    files.append((own(s), os.path.join(d, s)))
        return files

    def ledger(self, tid):
        t = dict(self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone())
        revs = []
        for card, in self.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq",
                                    (tid,)).fetchall():
            ev = rules.pass_evidence(self.obs.cards.get(card), self.obs.attachments.get(card), tid)
            if ev:
                revs.append({"rev": ev["rev"], "plan_sha256": ev["sha256"], "pass_ref": card,
                             "published": ev["rev"] == t["published_rev"] and ev["sha256"] == t["published_sha256"]})
        apps = [dict(a) for a in self.c.execute("SELECT * FROM approvals WHERE task=? ORDER BY id", (tid,))]
        gates = next((json.loads(a["clickup_gates"]) for a in reversed(apps) if a["clickup_gates"]), None)
        return render.ledger_file({
            "task_id": tid, "phase": t["phase"], "input_gen": t["input_gen"], "classified_gen": t["classified_gen"],
            "scope_gen": t["scope_gen"], "published_rev": t["published_rev"], "revisions": revs,
            "approvals": [{"id": a["id"], "rev": a["rev"], "author_name": a["author_name"],
                           "comment_date_ms": a["comment_date"], "state": a["state"]} for a in apps],
            "clickup_gates": gates})

    def relay_body(self, row, p):
        """``CLICKUP-IN v1`` relay comment (§8.2), rendered once with the relay's context file SHA-256."""
        path = os.path.join(self.stage_dir(row["idem_key"]), "body.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        files = self.context_files(row, p)
        if files is None:
            return None
        d = os.path.dirname(files[0][1])
        with open(files[0][1], "rb") as f:
            ctx_sha = _sha(f.read())
        with open(os.path.join(d, "items.json"), encoding="utf-8") as f:
            known = {(i["kind"], i["id"]): i for i in json.load(f)}
        closures = {tuple(x) for x in p.get("closures") or []}
        items = []
        for kind, iid in p.get("items") or []:
            view = dict(known.get((kind, iid)) or {"kind": kind, "id": iid, "text": "(not present in the snapshot)"})
            if (kind, iid) in closures and not view.get("closure"):
                view["closure"] = "yes"
            items.append(view)
        body = render.relay_comment(p["relay"], p["gen"], items, files[0][0], ctx_sha, p.get("note"))
        self.stage(row["idem_key"], "body.md", body)
        with open(path, encoding="utf-8") as f:
            return f.read()


def _feedback_at(show, line):
    """Kanban ``created_at`` of our delivered filter feedback comment (first line ``line``), else None."""
    for cm in (show or {}).get("comments") or []:
        if isinstance(cm, dict) and cm.get("author") == kp.AUTHOR and isinstance(cm.get("created_at"), int) \
                and str(cm.get("body", "")).splitlines()[:1] == [line]:
            return cm["created_at"]
    return None


def _no_correction(card, status, task):
    return ("ClickUp outbound filter: request card %s is %s, so it cannot be asked for a corrected draft; nothing"
            " was posted; task %s on hold" % (card, status, task))


def shared_context_key(key):
    """Staging key of the snapshot shared by an approval's orchestrator and gate context rows for one gen:
    the gate row's key (``clickup-dp:<task>:gate:r<N>:g<A>:attach:context:g<G>``). Other keys map to themselves."""
    if ":attach:context:" not in key:
        return key
    return key.replace(":orch:", ":gate:", 1)


def plan_summary(text, cap=2000):
    """The ``## ClickUp summary`` section of a plan revision (§8.1), at most ``cap`` characters."""
    m = re.search(r"^## ClickUp summary[ \t]*\r?\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return (m.group(1).strip() if m else "")[:cap]


# ------------------------------------------------------------------------------------------- operator
def _lock(runtime_dir):
    """Exclusive ``msvcrt.locking`` on ``intake.lock``; None when another instance holds it."""
    import msvcrt
    os.makedirs(runtime_dir, exist_ok=True)
    f = open(os.path.join(runtime_dir, "intake.lock"), "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def _logger(runtime_dir):
    os.makedirs(os.path.join(runtime_dir, "logs"), exist_ok=True)
    log = logging.Logger("clickup_intake.run")
    h = logging.handlers.RotatingFileHandler(os.path.join(runtime_dir, "logs", "intake.jsonl"),
                                             maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
    h.setFormatter(logging.Formatter('{"ts": "%(asctime)s", "line": %(message)s}'))
    log.addHandler(h)
    return log


def hold_task(store, task, now):
    """``hold <task>``: operator hold; a live ticket is revoked in the same transaction (§8.5, §11)."""
    with store.transaction() as c:
        if c.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (task,)).rowcount != 1:
            return False
        revoke_live_tickets(c, task, "operator", now)
    return True


def release_task(store, task):
    with store.transaction() as c:
        return c.execute("UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?", (task,)).rowcount == 1


def _resume_filtered_publication(store, key):
    """Explicitly rearm the exact never-sent attachment -> comment -> status chain.

    Keep payloads, preconditions and accounting intact. Normal send-time gates still apply.
    """
    match = re.fullmatch(r"clickup-dp:([^:]+):pub:r([1-9]\d*):([0-9a-f]{12}):i(\d+):attach", key)
    if not match:
        return False
    task, rev, digest, gen = match.groups()
    base = key[:-len(":attach")]
    keys = [base + suffix for suffix in (":attach", ":comment", ":status")]
    with store.transaction() as c:
        rows = [c.execute("SELECT * FROM outbox WHERE idem_key=?", (k,)).fetchone() for k in keys]
        current = c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (task,)).fetchone()
        if any(r is None for r in rows) or current is None:
            return False
        if (not current["eligible"] or current["terminal"] or current["phase"] not in rules.LIVE_PHASES
                or current["hold_reason"] not in (None, "", "outbound_filter")
                or current["published_rev"] is not None or current["published_sha256"] is not None
                or current["input_gen"] != int(gen) or current["classified_gen"] != int(gen)):
            return False
        try:
            pre = json.loads(rows[0]["preconditions"])
            bodies = [json.loads(r["body"]) for r in rows]
            if (pre["task_id"] != task or pre["input_gen"] != int(gen)
                    or pre["scope_gen"] != current["scope_gen"]
                    or not re.fullmatch(digest + r"[0-9a-f]{52}", bodies[0]["sha256"])
                    or bodies[1]["summary_from"] != {"card": bodies[0]["card"],
                                                      "attachment": bodies[0]["attachment"]}
                    or bodies[2]["status"] != "waiting for approval"):
                return False
            for index, row in enumerate(rows):
                if (row["task"] != task or row["target"] != task or bodies[index]["task"] != task
                        or row["kind"] != ("cu_attach", "cu_comment", "cu_status")[index]
                        or (row["state"], row["outcome"]) != (("failed", "FILTERED") if index == 0
                            else ("cancelled_stale", "DEPENDENCY"))
                        or row["attempts"] != 0
                        or any(row[k] is not None for k in ("first_send_ms", "next_attempt_ms", "child_pid",
                                                           "child_start_time", "result_ref"))
                        or json.loads(row["preconditions"]) != pre
                        or json.loads(row["depends_on"]) != ([] if index == 0 else [keys[index - 1]])):
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        mirrors = [k + suffix for k in keys for suffix in ("#cu", "#cu-att")]
        if any(c.execute("SELECT 1 FROM outbox WHERE idem_key=?", (k,)).fetchone() for k in mirrors):
            return False
        c.executemany("UPDATE outbox SET state='pending', outcome=NULL WHERE idem_key=?", [(k,) for k in keys])
        c.execute("UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=? AND hold_reason='outbound_filter'",
                  (task,))
    return True


def _resume_blocker_notification(store, key):
    """Explicit resend of a proven never-sent NULL-root status; never replay native work."""
    match = re.fullmatch(r'clickup-dp:([^:]+):blocker:(\d+):(recorded:g\d+|resumed)', key)
    if not match:
        return False
    tid, bid, notice = match.groups()
    with store.transaction() as c:
        ctx = engine._Ctx(store, store.clock(), engine.Observation(), engine.Config())
        row = c.execute('SELECT * FROM outbox WHERE idem_key=?', (key,)).fetchone()
        b = c.execute('SELECT * FROM blockers WHERE id=? AND task=?', (int(bid), tid)).fetchone()
        t = c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone()
        if not row or not b or not t or not t['eligible'] or t['terminal'] or t['hold_reason']:
            return False
        b, t, row = dict(b), dict(t), dict(row)
        a, bound = engine._gov(ctx, b)
        if (b['closure_item'] is not None or not a or not engine._incorporated(b, bound)
                or bound['task'] != tid or bound['question_card'] != b['question_card'] or bound['card'] != b['card']
                or bound['rev'] != a['rev'] or bound['plan_sha256'] != a['plan_sha256']
                or a['state'] not in engine.AMENDED_STATES or t['phase'] not in rules.LIVE_PHASES
                or not rules.current(a['accounts_gen'], t)
                or t['classified_gen'] != t['input_gen'] or t['scope_gen'] != t['input_gen']
                or a['rev'] != t['published_rev'] or a['plan_sha256'] != t['published_sha256']
                or bound['gen'] != b['delivered_gen'] or b['resolution_gen'] != b['delivered_gen']
                or t['input_gen'] != b['delivered_gen']
                or c.execute("SELECT 1 FROM approvals WHERE task=? AND id>? AND state IN (%s)" %
                             engine._in(engine.ACTIVE + engine.WATCHED_STATES), (tid, a['id'])).fetchone()):
            return False
        if notice.startswith('recorded:'):
            if notice != 'recorded:g%d' % b['delivered_gen'] or b['state'] not in ('delivered', 'resumed', 'resumed_unconfirmed'):
                return False
            text = 'Answer recorded.' + (' The security/containment boundary is unchanged; any new authority will come back'
                                         ' as a plan revision for approval.' if b['boundary'] else '')
        else:
            if b['state'] not in ('resumed', 'resumed_unconfirmed'):
                return False
            text = 'The blocked step has resumed' + (' with your answer.' if b['state'] == 'resumed' else '.')
        original = dict(task=tid, ref='cu-' + _sha(key)[:12], root=None, text=text)
        try:
            valid = (json.loads(row['body']) == original and _sha(row['body']) == row['payload_sha256']
                     and json.loads(row['depends_on']) == []
                     and json.loads(row['preconditions']) == json.loads(json.dumps(dataclasses.asdict(Preconditions(task_id=tid,
                         list_id=json.loads(row['preconditions'])['list_id'])))))
        except (KeyError, TypeError, ValueError):
            return False
        if (not valid or row['kind'] != 'cu_reply' or row['task'] != tid or row['target'] != tid
                or (row['state'], row['outcome']) != ('cancelled_stale', 'FOREIGN_ROOT') or row['attempts'] != 0
                or any(row[k] is not None for k in ('first_send_ms', 'next_attempt_ms', 'child_pid',
                                                   'child_start_time', 'result_ref'))
                or c.execute('SELECT 1 FROM outbox WHERE idem_key IN (?,?)',
                             (key + '#cu', key + '#cu-att')).fetchone()):
            return False
        root = engine._closure_root(ctx, tid, b)
        if root is None:
            return False
        historical = ('Historical update for earlier question blocker %d:\n%s\n\n'
                      'This records that earlier transition only, not a statement of current work status.'
                      % (b['id'], text))
        body = json.dumps(dict(original, root=root, text=historical), sort_keys=True, separators=(',', ':'))
        pre = dataclasses.asdict(Preconditions(task_id=tid, list_id=json.loads(row['preconditions'])['list_id'],
            input_gen=t['input_gen'], scope_gen=t['scope_gen'], published_rev=a['rev'],
            approval_id=a['id'], approval_states=(a['state'],)))
        c.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                  ('delivery_repair:' + key, json.dumps(row, sort_keys=True)))
        c.execute("UPDATE outbox SET body=?,payload_sha256=?,preconditions=?,state='pending',outcome=NULL WHERE idem_key=?",
                  (body, _sha(body), json.dumps(pre), key))
    return True


def outbox_command(store, action, key):
    """``outbox resend|abandon <id>``; resend also repairs bound never-sent NULL-root statuses."""
    base = key.split("#", 1)[0]
    root = store.conn.execute("SELECT kind, outcome FROM outbox WHERE idem_key=?", (base,)).fetchone()
    if action == 'resend' and root and root['kind'] == 'cu_reply' and root['outcome'] == 'FOREIGN_ROOT':
        return key == base and _resume_blocker_notification(store, base)
    if (action == "resend" and root and root["kind"] == "cu_attach" and root["outcome"] == "FILTERED"
            and re.fullmatch(r"clickup-dp:[^:]+:pub:r[1-9]\d*:[0-9a-f]{12}:i\d+:attach", base)):
        return key == base and _resume_filtered_publication(store, base)
    keys = [k for k in (base, base + "#cu-att", base + "#cu")]
    rows = {k: store.conn.execute("SELECT task, state FROM outbox WHERE idem_key=?", (k,)).fetchone() for k in keys}
    rows = {k: r for k, r in rows.items() if r}
    if not rows or not any(r["state"] in ("held_ambiguous", "failed") for r in rows.values()):
        return False
    task = next(iter(rows.values()))["task"]
    with store.transaction() as c:
        for k, r in rows.items():
            if r["state"] == "done":
                continue
            if action == "resend":
                c.execute("UPDATE outbox SET state='pending', outcome=NULL, attempts=0, next_attempt_ms=NULL,"
                          " child_pid=NULL, child_start_time=NULL WHERE idem_key=?", (k,))
            else:
                c.execute("UPDATE outbox SET state='cancelled_stale', outcome='ABANDONED' WHERE idem_key=?", (k,))
        c.execute("UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=? AND hold_reason IN (%s)"
                  % ",".join("?" * len(keys)), [task] + ["outbox:" + k for k in keys])
    return True


def status_lines(state_path):
    conn = open_readonly(state_path)
    try:
        phases = conn.execute("SELECT COALESCE(phase, '-'), COUNT(*) FROM tasks GROUP BY 1 ORDER BY 1").fetchall()
        holds = conn.execute("SELECT COUNT(*) FROM tasks WHERE hold_reason IS NOT NULL AND hold_reason<>''").fetchone()
        rows = conn.execute("SELECT state, COUNT(*) FROM outbox WHERE kind NOT LIKE 'clickup_%' GROUP BY 1 ORDER BY 1"
                            ).fetchall()
        tickets = conn.execute("SELECT COUNT(*) FROM approvals WHERE state='ticketed'").fetchone()
        incident = conn.execute("SELECT 1 FROM meta WHERE key=?", (PREFLIGHT_KEY,)).fetchone()
    finally:
        conn.close()
    return ["tasks: " + (", ".join("%s=%d" % (p, n) for p, n in phases) or "none") + "; holds=%d" % holds[0],
            "outbox: " + (", ".join("%s=%d" % (s, n) for s, n in rows) or "empty") + "; live tickets=%d" % tickets[0],
            "preflight incident: " + ("open" if incident else "none")]


def rollback_preflight(store):
    """r6 §7 step 3, r9 §7, r11 §9, r12 §9 full code rollback preflight (read-only): the reasons it refuses."""
    one = lambda sql: store.conn.execute(sql).fetchone()[0]  # noqa: E731
    reasons = ["blockers enabled"] if one("SELECT COUNT(*) FROM meta WHERE key='%s'" % engine.CUTOVER_KEY) else []
    for label, n in (
            ("blocker rows not done or cancelled_stale", one(
                "SELECT COUNT(*) FROM outbox WHERE state NOT IN ('done', 'cancelled_stale') AND"
                " (idem_key LIKE 'clickup-dp:%:blocker:%' OR idem_key LIKE 'clickup-dp:%:bq:%')")),
            ("blockers releasing", one("SELECT COUNT(*) FROM blockers WHERE state='releasing'")),
            ("fences open or releasing", one(
                "SELECT COUNT(*) FROM fences WHERE kind='fence' AND state IN ('open', 'releasing')")),
            ("parks open or releasing", one(
                "SELECT COUNT(*) FROM fences WHERE kind='park' AND state IN ('open', 'releasing')"))):
        if n:
            reasons.append("%s: %d" % (label, n))
    return reasons


def ticket_check(cfg, port, gate, task, rev, sha, now):
    """§8.5 read-only verdict: local state snapshot (read-only URI) + ``show --json`` of the gate."""
    snapshot = load_ticket_snapshot(cfg.state_path, task, cfg.ticket_lease_seconds)
    shown = port.show(gate)
    return ticket_verdict(snapshot, shown.json if _ok(shown) else "", task, rev, sha, gate, now)


def recovery_check(cfg, port, card, run, clock=None):
    """r3 §6.1 read-only admission: one read-only state snapshot and one ``show W --json``; never locks or writes."""
    snap = load_recovery_snapshot(cfg.state_path, None, card, run)
    if snap is not None and (snap.get('incident') or {}).get('class') == 'progress_stall':
        return _progress_check(lambda: load_recovery_snapshot(cfg.state_path, None, card, run), port,
                               card, run, clock or (lambda: int(time.time() * 1000)),
                               handoff_unblock=cfg.handoff_unblock)
    if card.startswith('progress-'):
        # Logical anchors never fall through to native lookup, even without a matching row.
        return kp.RecoveryVerdict('UNAVAILABLE' if snap is None else 'STALE', 'logical_anchor_unresolved')
    if snap is not None and (snap.get('incident') or {}).get('class') == 'approval_staging':
        return _preworker_check(lambda: load_recovery_snapshot(cfg.state_path, None, card, run), port,
                                card, run, clock or (lambda: int(time.time() * 1000)),
                                handoff_unblock=cfg.handoff_unblock)
    reader = _PreworkerReads(port, clock or (lambda: int(time.time() * 1000)))
    if snap is not None:
        snap["members"] = engine.recovery_members((snap["incident"] or {}).get("task"), snap["approvals"],
                                                  snap["orch"])
    w = None if getattr(port, '_preworker_snapshot', None) else reader.read('show', card, fresh=True)
    if snap is not None and (snap.get("incident") or {}).get("class") == stalled.CLASS:
        _diagnostic_reads(snap, port, card, w, reader)
        if not getattr(port, '_preworker_snapshot', None):
            reader.invalidate([card])
            w = reader.read('show', card, fresh=True)
            snap['shows'][card] = w
        latest = load_recovery_snapshot(cfg.state_path, None, card, run)
        if latest is None:
            return kp.RecoveryVerdict("UNAVAILABLE", "state_unavailable")
        snap.update(latest)
        snap["members"] = engine.recovery_members((snap.get("incident") or {}).get("task"), snap["approvals"], snap["orch"])
        snap.update(now_ms=clock() if clock is not None else int(time.time() * 1000),
                    scan_started_ms=(snap.get("task") or {}).get("last_light_ms") or 0)
    if snap is not None and getattr(port, '_preworker_snapshot', None):
        if not _recovery_dependencies(snap, port, reader):
            return kp.RecoveryVerdict('UNAVAILABLE', 'dependency_graph_unavailable')
        return kp.recovery_verdict(snap, snap['shows'].get(card), card, run)
    return kp.recovery_verdict(snap if snap is not None else False, w, card, run)


def _progress_collect(snap, reader):
    """Use the existing 64-read native closure allowance, without requiring a valid PASS."""
    from .preworker_snapshot import MAX_DEPENDENCY_NODES
    roots = set(snap['requests'][:1]) | {b['question_card'] for b in snap['blockers']}
    roots |= {a['orchestrator_card'] for a in snap['approvals'] if a['orchestrator_card']}
    roots |= {f['fence_card'] for f in snap['fences'] if f['state'] in ('open', 'releasing', 'abandoned', 'refused')}
    if snap.get('failure') and ':bq:' in snap['failure']['idem_key'] and snap.get('dependency_scope'):
        roots.update(snap['dependency_scope'][0])
    inc = snap.get('incident') or {}
    whole_workflow = (progress.outer_incident(inc, snap) if inc else bool(snap.get('completed_incidents')))
    if whole_workflow:
        roots = progress.outer_roots(snap)
    elif not inc:
        roots.update(progress.outer_roots(snap))
    factory = getattr(reader.port, '_preworker_snapshot', None)
    if factory is None:
        return {}, {}, False
    started = reader.clock()
    if reader.proof is not None:
        # Discovery hints only: every caller still acquires a fresh snapshot.
        # Reusing known ids avoids spending the same closure reads at send time.
        roots.update(reader.proof.shows)
        reader.proof.close()
    elif getattr(reader, 'dependencies', None):
        # The earlier dependency read already discovered this frontier. Expand
        # its ids with one charged link read, not two duplicate full snapshots.
        # Only ids are reused; statuses, files and authority are read afresh below.
        import sqlite3
        hints = reader.dependencies[1]
        roots.update(hints.shows)
        roots.update(child for shown in hints.shows.values() for child in shown.get('children', []))
        if len(roots) > MAX_DEPENDENCY_NODES:
            return {}, {}, False
        try:
            roots.update(row[0] for row in hints.query(
                'SELECT DISTINCT child_id FROM task_links WHERE parent_id IN '
                '(SELECT value FROM json_each(?)) LIMIT 513', (json.dumps(sorted(roots)),)))
        except (ValueError, sqlite3.Error):
            return {}, {}, False
        if len(roots) > MAX_DEPENDENCY_NODES:
            return {}, {}, False
    reader.proof = proof = factory(roots, reader.take, reader.clock)
    if proof is None:
        return {}, {}, False
    while True:
        children = {child for shown in proof.shows.values() for child in shown.get('children', [])}
        missing = children - proof.shows.keys()
        if not missing:
            break
        proof.close()
        expanded = roots | proof.shows.keys() | children
        if (missing <= roots or len(expanded) > MAX_DEPENDENCY_NODES
                or reader.clock() - started > 5 * 60_000):
            return {}, {}, False
        roots = expanded
        reader.proof = proof = factory(roots, reader.take, reader.clock)
        if proof is None:
            return {}, {}, False
    if reader.clock() - started > 5 * 60_000:
        proof.close()
        return {}, {}, False
    atts = {}
    for source in snap['requests'][:1]:
        if source not in proof.shows:
            continue
        atts[source] = []
        for a in proof.attachments.get(source, []):
            if not PLAN_NAME.fullmatch(str(a.get('filename', ''))):
                continue
            try:
                with open(a['stored_path'], 'rb') as stream:
                    data = stream.read(MARKER_SCAN_MAX + 1)
            except (KeyError, OSError, TypeError):
                return {}, {}, False
            if len(data) > MARKER_SCAN_MAX:
                return {}, {}, False
            sha = _sha(data)
            proof.files[a['stored_path']] = sha
            atts[source].append(_observed_attachment(dict(a, data=data, sha256=sha)))
    return proof.shows, atts, proof.valid()


def _progress_check(load, port, card, run, clock, scan=None, notice=False, reader=None, handoff_unblock=True):
    from . import progress
    snap = load()
    if snap is None or not snap.get('task'):
        return kp.RecoveryVerdict('UNAVAILABLE', 'state_unavailable')
    reader = reader or _PreworkerReads(port, clock)
    before = progress.binding(snap['task'], snap)
    shows, atts, complete = _progress_collect(snap, reader)
    latest = load()
    if latest is None or not latest.get('task'):
        return kp.RecoveryVerdict('UNAVAILABLE', 'state_unavailable')
    if before != progress.binding(latest['task'], latest):
        return kp.RecoveryVerdict('STALE', 'context_changed')
    unavailable = not complete or reader.proof is None or not reader.proof.valid()
    whole_workflow = bool(latest.get('incident')) and progress.outer_incident(latest['incident'], latest)
    if unavailable and not whole_workflow:
        return kp.RecoveryVerdict('UNAVAILABLE', 'progress_observation')
    if unavailable:
        shows, atts = {}, {}
    latest.update(shows=shows, attachments=atts, now_ms=clock(), handoff_unblock=handoff_unblock,
                  native_unavailable=unavailable,
                  scan_started_ms=scan if scan is not None else (latest['task']['last_light_ms'] or 0))
    return progress.verdict(latest, card, run, notice=notice)


def _preworker_check(load, port, card, run, clock, scan=None, notice=False, handoff_unblock=True, reader=None):
    from . import preworker
    snap = load()
    if snap is None:
        return kp.RecoveryVerdict('UNAVAILABLE', 'state_unavailable')
    snap['handoff_unblock'] = handoff_unblock
    a, why = preworker.authority(snap, card, run)
    if why:
        return kp.RecoveryVerdict('STALE', why)
    binding = _preworker_binding(snap['task'], a, snap)
    reader = reader or _PreworkerReads(port, clock)
    cards, atts, complete = _preworker_collect(snap['task'], a, snap, reader, recheck=True)
    latest = load()  # Revalidate authority and required target set after all I/O.
    if latest is None:
        return kp.RecoveryVerdict('UNAVAILABLE', 'state_unavailable')
    latest['handoff_unblock'] = handoff_unblock
    current, why = preworker.authority(latest, card, run)
    if why:
        return kp.RecoveryVerdict('STALE', why)
    if (not complete or reader.proof is None or not reader.proof.valid()
            or _preworker_binding(latest['task'], current, latest) != binding):
        return kp.RecoveryVerdict('UNAVAILABLE', 'staging_observation')
    snap.update(latest)
    snap.update(shows=cards, attachments=atts)
    snap.update(now_ms=clock(), scan_started_ms=scan if scan is not None else (snap['task']['last_light_ms'] or 0))
    return preworker.verdict(snap, card, run, notice=notice)


def _diagnostic_complete(snap, card, w, approval):
    """Separate an unavailable observation (defer) from a completely observed changed obligation (cancel)."""
    if not isinstance(w, dict) or not isinstance(w.get('parents'), list):
        return False
    required = set(w['parents']) | set(snap['requests']) | {b['question_card'] for b in snap['blockers']}
    required |= {f['fence_card'] for f in snap['fences'] if f['state'] in ('open', 'releasing')}
    required |= {a['orchestrator_card'] for a in snap['approvals'] if a['orchestrator_card']}
    for other in required | {card}:
        show = snap['shows'].get(other)
        if not isinstance(show, dict) or (show.get('task') or {}).get('id') != other or kp.card_status(show) is None:
            return False
    if any(r not in snap['attachments'] for r in snap['requests']):
        return False
    for r in snap['requests']:
        ev = rules.pass_evidence(snap['shows'].get(r), snap['attachments'][r], approval['task'])
        if ev and ev['rev'] == approval['rev'] and ev['sha256'] == approval['plan_sha256']:
            return True
    return False


def _recovery_dependencies(snap, port, reader):
    """Current approval-bound graph for public admission and every recovery effect."""
    if not snap.get('dependency_scope') or reader.remaining <= 0:
        return False
    roots, forbidden = snap['dependency_scope']
    extra = set(snap.get('requests', [])) | {b['question_card'] for b in snap.get('blockers', [])}
    extra |= {f['fence_card'] for f in snap.get('fences', []) if f['state'] in ('open', 'releasing')}
    binding = json.dumps([snap['dependency_scope'], sorted(extra)], sort_keys=True)
    cached = getattr(reader, 'dependencies', None)
    if cached and cached[0] == binding and cached[1].valid():
        proof = cached[1]
    else:
        if cached:
            cached[1].close()
        proof = port._preworker_snapshot(set(roots) | extra, reader.take, reader.clock)
        if proof is None:
            return False
        reader.dependencies = (binding, proof)
    if (not (set(roots) | extra).issubset(proof.shows)
            or any(parent not in proof.shows for shown in proof.shows.values()
                   for parent in shown['parents'])):
        return False  # a missing required node is unknown, not affirmative lost ownership
    snap['members'] = engine.dependency_members(roots, proof.shows, forbidden)
    snap['shows'] = dict(proof.shows)
    snap['attachments'] = {}
    for source in snap.get('requests', []):
        if source not in proof.shows:
            return False
        snap['attachments'][source] = []
        for att in proof.attachments.get(source, []):
            if not PLAN_NAME.fullmatch(str(att.get('filename', ''))):
                continue
            try:
                with open(att['stored_path'], 'rb') as stream:
                    data = stream.read(MARKER_SCAN_MAX + 1)
            except (KeyError, OSError, TypeError):
                return False
            if len(data) > MARKER_SCAN_MAX:
                return False
            digest = _sha(data)
            proof.files[att['stored_path']] = digest
            snap['attachments'][source].append(_observed_attachment(dict(att, data=data, sha256=digest)))
    return proof.valid()


def _diagnostic_reads(snap, port, card, w, reader=None):
    """Fresh read-only diagnostic inputs; failed reads remain absent, never empty proof."""
    reader = reader or _PreworkerReads(port, lambda: int(time.time() * 1000))
    if getattr(port, '_preworker_snapshot', None):
        if not _recovery_dependencies(snap, port, reader):
            snap['shows'], snap['attachments'] = {}, {}
        return
    snap["shows"], snap["attachments"] = {card: w}, {}
    want = set((w or {}).get("parents") or []) | set(snap["requests"]) | {b["question_card"] for b in snap["blockers"]}
    want |= {a["orchestrator_card"] for a in snap["approvals"] if a["orchestrator_card"]}
    want |= {f['fence_card'] for f in snap['fences'] if f['state'] in ('open', 'releasing')}
    reader.invalidate(want)  # each legacy gate re-observes obligations; accounting is shared, not proof
    for other in sorted(want - {card}):
        shown = reader.read('show', other, fresh=True)
        if shown is not None:
            snap["shows"][other] = shown
    for source in snap["requests"]:
        atts = reader.read('attachments', source, fresh=True)
        if atts is not None:
            snap["attachments"][source] = atts


def recovery_adopt(store, port, cfg, card, run, now):
    """r3 §11 step 7: record an exact current pre-cutover incident (W, R) as adopted; (ok, line)."""
    c = store.conn
    if c.execute("SELECT 1 FROM meta WHERE key=?", (engine.RECOVERY_KEY,)).fetchone() is None:
        return False, "recovery not enabled"
    old = c.execute("SELECT id FROM recoveries WHERE card=? AND run_id=?", (card, run)).fetchone()
    if old:
        return True, "adopted incident %d card=%s run=%d (existing)" % (old[0], card, run)
    shown = port.show(card)
    if not _ok(shown) or not isinstance(shown.json, dict) or kp.card_status(shown.json) is None:
        return False, "card %s is unreadable" % card
    found = kp.recovery_incident(shown.json)
    if found is None:
        return False, "no actionable typed worker stop on %s" % card
    if found["run_id"] != run:
        return False, "run %d is not the latest blocked run of %s (latest %d)" % (run, card, found["run_id"])
    ctx = engine._Ctx(store, now, engine.Observation(), cfg)
    for (tid,) in c.execute("SELECT clickup_task_id FROM tasks ORDER BY clickup_task_id").fetchall():
        a = engine._recovery_members(ctx, tid).get(card)
        if a is None:
            continue
        t = ctx.task(tid)
        if t["terminal"]:
            return False, "task %s is terminal" % tid
        if t["hold_reason"]:
            return False, "task %s is held" % tid
        if t["phase"] not in engine.AMENDMENT_PHASES:
            return False, "task %s is in phase %s" % (tid, t["phase"])
        if not rules.current(a["accounts_gen"], t):
            return False, "approval %d is not current" % a["id"]
        with store.transaction():
            inc = engine.open_incident(ctx, tid, card, a, found, adopted=1)
        return True, "adopted incident %d card=%s run=%d" % (inc["id"], card, run)
    return False, "%s is not a recovery member of a live approval" % card


def recovery_status(store):
    c = store.conn
    cut = c.execute("SELECT value FROM meta WHERE key=?", (engine.RECOVERY_KEY,)).fetchone()
    states = c.execute("SELECT state, COUNT(*) FROM recoveries GROUP BY 1 ORDER BY 1").fetchall()
    return ["recovery: " + ("enabled; cutover %s" % json.loads(cut[0]) if cut else "disabled"),
            "incidents: " + (", ".join("%s=%d" % (st, n) for st, n in states) or "none")]


def recovery_rollback_check(store):
    """r3 §8 (read-only): the recovery-only reasons a rollback to 2d53206 is refused."""
    return _recovery_reasons(store.conn)


def _recovery_reasons(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    reasons = ["recovery enabled"] if one("SELECT COUNT(*) FROM meta WHERE key='%s'" % engine.RECOVERY_KEY) else []
    for label, n in (
            (":rcv: rows not done or cancelled_stale", one(
                "SELECT COUNT(*) FROM outbox WHERE state NOT IN ('done', 'cancelled_stale') AND"
                " idem_key LIKE 'clickup-dp:%:rcv:%'")),
            ("originating recovery notice rows not done or cancelled_stale", one(
                "SELECT COUNT(*) FROM outbox WHERE state NOT IN ('done', 'cancelled_stale') AND"
                " idem_key LIKE 'clickup-dp:%:recovery-notice:%'")),
            ("open incidents", one("SELECT COUNT(*) FROM recoveries WHERE state IN (%s)"
                                   % engine._in(engine.RECOVERY_LIVE)) if _has_table(conn, "recoveries") else 0)):
        if n:
            reasons.append("%s: %d" % (label, n))
    return reasons


# ---- workflow repair r3 §3.8/§3.9: amendment continuation, admission flag and rollback readiness ----------------
_SHA64 = re.compile(r"[0-9a-f]{64}")
ADMISSION_EXEMPT = (("blockers", "rollback-preflight"), ("blockers", "admission-open"), ("recovery", "status"),
                    ("recovery", "rollback-check"))  # r3 §3.8: admission-open and the read-only lock-taking commands


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _meta_json(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def rollback_readiness(conn, target):
    """r3 §3.9 readiness of a rollback to ``target`` (read-only): the reasons it is refused; [] when ready."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    reasons = []
    if _has_table(conn, "amendment_answers"):
        n = one("SELECT COUNT(*) FROM amendment_answers WHERE state='bound'")
        if n:
            reasons.append("amendment bindings bound: %d" % n)
        n = one("SELECT COUNT(*) FROM amendment_answers k JOIN blockers b ON b.id=k.blocker_id"
                " WHERE k.state='applied' AND b.state NOT IN (%s)" % engine._in(engine.BLOCKER_FINAL))
        if n:
            reasons.append("applied amendment blockers not final: %d" % n)
    n = one("SELECT COUNT(*) FROM outbox WHERE state IN ('sending', 'ambiguous')")
    if n:
        reasons.append("outbox rows sending or ambiguous: %d" % n)
    if target == "aa3cbe67c998":  # r3 §3.9 item 4: that target predates recovery
        reasons += _recovery_reasons(conn)
    return reasons


def amendment_status(conn, target):
    """``blockers amendment-status`` lines (read-only)."""
    lines = []
    if _has_table(conn, "amendment_answers"):
        for k in conn.execute("SELECT k.*, b.state AS bstate FROM amendment_answers k LEFT JOIN blockers b"
                              " ON b.id=k.blocker_id ORDER BY k.id").fetchall():
            lines.append("binding %d blocker %d approval %d source %s: %s%s; blocker %s"
                         % (k["id"], k["blocker_id"], k["approval_id"], k["source"], k["state"],
                            " (%s)" % k["reason"] if k["reason"] else "", k["bstate"] or "missing"))
    cut, adm = _meta_json(conn, engine.AMEND_KEY), _meta_json(conn, engine.ADMISSION_KEY)
    lines.append("amendment continuation: " + ("enabled %d drain %s" % (cut["ms"], cut["drain_sha256"][:12])
                                               if cut else "disabled"))
    lines.append("admission: " + ("closed (rollback to %s)" % adm["target"] if adm else "open"))
    reasons = rollback_readiness(conn, target)
    lines.append("rollback-target %s: %s" % (target, "refused (%s)" % "; ".join(reasons) if reasons else "ready"))
    return lines


def amendment_enable(store, drain_sha, now):
    """r3 §3.8 ``blockers amendment-enable``: write the enablement key once; a repeat keeps the first value."""
    if not drain_sha or not _SHA64.fullmatch(drain_sha):
        return False, "--drain-sha must be 64 lowercase hex characters"
    with store.transaction() as c:
        if _meta_json(c, engine.ADMISSION_KEY) is not None:
            return False, "admission closed"
        if _meta_json(c, engine.CUTOVER_KEY) is None:
            return False, "blockers not enabled"
        cut = _meta_json(c, engine.AMEND_KEY)
        if cut is not None:
            return True, "amendment continuation enabled %d drain %s (existing)" % (cut["ms"],
                                                                                 cut["drain_sha256"][:12])
        c.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                  (engine.AMEND_KEY, json.dumps({"ms": now, "drain_sha256": drain_sha})))
    return True, "amendment continuation enabled %d drain %s" % (now, drain_sha[:12])


def rollback_prepare(store, target, now):
    """r3 §3.9: readiness and the admission flag in ONE transaction; no override."""
    if target != ROLLBACK_TARGET:
        return False, "target must be %s" % ROLLBACK_TARGET
    with store.transaction() as c:
        reasons = rollback_readiness(c, target)
        if reasons:
            return False, "rollback refused: " + "; ".join(reasons)
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                  (engine.ADMISSION_KEY, json.dumps({"target": target, "ms": now})))
    return True, "rollback ready; admission closed"


def admission_open(store):
    with store.transaction() as c:
        c.execute("DELETE FROM meta WHERE key=?", (engine.ADMISSION_KEY,))
    return True, "admission open"


def adopt_amendment(store, port, blocker, approval, receipt, receipt_sha, now):
    """r3 §3.9 ``blockers adopt-amendment``: one ``bound`` operator row, or the first failed requirement."""
    c = store.conn
    if _meta_json(c, engine.ADMISSION_KEY) is not None:
        return False, "admission closed"
    if _meta_json(c, engine.CUTOVER_KEY) is None:
        return False, "blockers not enabled"
    if _meta_json(c, engine.AMEND_KEY) is None:
        return False, "amendment continuation not enabled"
    if c.execute("SELECT 1 FROM amendment_answers WHERE blocker_id=? AND approval_id=?",
                 (blocker, approval)).fetchone():
        return True, "binding blocker %d approval %d (existing)" % (blocker, approval)
    b = c.execute("SELECT * FROM blockers WHERE id=?", (blocker,)).fetchone()
    if b is None:
        return False, "unknown blocker %d" % blocker
    b = dict(b)
    if b["state"] != "scope_change" or b["delivered_gen"] is not None or b["release_key"] is not None \
            or b["resolution_authority"] != "unchanged" or b["resolution_body"] is None:
        return False, "blocker %d is not an undelivered scope_change with an unchanged-authority resolution" % blocker
    try:
        with open(receipt, "rb") as f:
            data = f.read()
    except (OSError, TypeError):
        return False, "receipt unreadable"
    if not receipt_sha or _sha(data) != receipt_sha:
        return False, "receipt SHA-256 mismatch"
    a = c.execute("SELECT * FROM approvals WHERE id=?", (approval,)).fetchone()
    if a is None or a["task"] != b["task"]:
        return False, "approval %d is not an approval of the blocker's task" % approval
    a = dict(a)
    line = rules.adoption_line(b["id"], b["question_card"], b["card"], b["resolution_gen"], b["resolution_relay"],
                               _sha(b["resolution_body"]), b["task"], a["id"], a["rev"], a["plan_sha256"])
    if line is None or not rules.has_line(data.decode("utf-8", "replace"), line):
        return False, "the receipt has no exact line for the live blocker and approval"
    if b["pending_relay"] != b["resolution_relay"]:
        return False, "blocker %d has a newer relay" % blocker
    if a["state"] not in engine.AMENDED_STATES:
        return False, "approval %d is %s" % (a["id"], a["state"])
    if a["id"] <= (b["approval_id"] or 0) or b["resolution_gen"] > a["accounts_gen"]:
        return False, "approval %d does not account the blocker's resolution" % a["id"]
    t = dict(c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (b["task"],)).fetchone())
    if t["input_gen"] > a["accounts_gen"] or not rules.current(a["accounts_gen"], t):
        return False, "approval %d is not current" % a["id"]
    if t["terminal"]:
        return False, "task %s is terminal" % t["clickup_task_id"]
    if t["hold_reason"]:
        return False, "task %s is held" % t["clickup_task_id"]
    if t["phase"] not in engine.AMENDMENT_PHASES:
        return False, "task %s is in phase %s" % (t["clickup_task_id"], t["phase"])
    q = port.show(b["question_card"])
    q = q.json if _ok(q) and isinstance(q.json, dict) else None
    if rules.status(q) != "blocked":
        return False, "question card %s is not blocked" % b["question_card"]
    w = port.show(b["card"])
    w = w.json if _ok(w) and isinstance(w.json, dict) else None
    if (rules.status(w) != "todo" or b["question_card"] not in rules.parents(w) or kp.open_run_ids(w)
            or any(r > (b["run_id"] or 0) for r in kp.claimed_runs(w)) or b["card"] not in engine.approval_cards(a)):
        return False, "work card %s is not eligible" % b["card"]
    with store.transaction() as tc:
        engine.insert_binding(tc, b["task"], b, a, "operator", receipt_sha, now)
        kid = tc.execute("SELECT id FROM amendment_answers WHERE blocker_id=? AND approval_id=?",
                         (blocker, approval)).fetchone()[0]
    return True, "binding %d blocker %d approval %d bound" % (kid, blocker, approval)


def _parser():
    p = argparse.ArgumentParser(prog="clickup_dedicated_panel_intake")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=("shadow",))
    sub.add_parser("status")
    for name in ("admit", "hold", "release"):
        sub.add_parser(name).add_argument("task")
    bl = sub.add_parser("blockers")
    bl.add_argument("action", choices=("enable", "disable", "protocol-comment", "rollback-preflight",
                                       "adopt-amendment", "amendment-status", "amendment-enable", "rollback-prepare",
                                       "admission-open"))  # workflow repair r3 §3.9
    bl.add_argument("--blocker", type=int)
    bl.add_argument("--approval", type=int)
    bl.add_argument("--receipt")
    bl.add_argument("--receipt-sha")
    bl.add_argument("--drain-sha")
    bl.add_argument("--target")
    ob = sub.add_parser("outbox")
    ob.add_argument("action", choices=("resend", "abandon"))
    ob.add_argument("id")
    tc = sub.add_parser("ticket-check")
    tc.add_argument("--gate", required=True)
    tc.add_argument("--task", required=True)
    tc.add_argument("--rev", required=True, type=int)
    tc.add_argument("--sha", required=True)
    rc = sub.add_parser("recovery-check")
    rc.add_argument("--card", required=True)
    rc.add_argument("--run", required=True, type=int)
    rv = sub.add_parser("recovery")
    rv.add_argument("action", choices=("enable", "disable", "adopt", "status", "rollback-check"))
    rv.add_argument("--card")
    rv.add_argument("--run", type=int)
    return p


def default_root():
    """The Hermes root of this installation: ``<root>/scripts/clickup_intake/main.py``."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None, root=None, *, transport=None, port=None, clock=None, opener=None, out=None):
    args = _parser().parse_args(argv)
    out = out or sys.stdout
    clock = clock or (lambda: int(time.time() * 1000))
    config_path = os.path.join(root or default_root(), *CONFIG_REL)

    def say(*lines):
        for line in lines:
            out.write(line + "\n")
        out.flush()

    try:
        cfg = load_config(config_path)
    except (OSError, ValueError):
        if args.cmd == "ticket-check":
            say("INVALID config_unavailable")
            return 3
        if args.cmd == "recovery-check":
            say("UNAVAILABLE config_unavailable")
            return 2
        say("ClickUp intake: config unavailable or invalid")
        return 2
    port = port or kp.KanbanCli(HERMES_EXE, cfg.board, discord_chat=cfg.discord_chat)

    if args.cmd == "ticket-check":
        verdict = ticket_check(cfg, port, args.gate, args.task, args.rev, args.sha, clock())
        say(str(verdict))
        return 0 if verdict.valid else 3
    if args.cmd == "recovery-check":  # r3 §6.1: read-only, before the lock
        verdict = recovery_check(cfg, port, args.card, args.run, clock)
        say(str(verdict))
        return verdict.code
    if args.cmd == "recovery" and args.action == "adopt" and (args.card is None or args.run is None):
        say("recovery adopt requires --card and --run")
        return 2
    if args.cmd == "blockers" and args.action == "adopt-amendment" and None in (
            args.blocker, args.approval, args.receipt, args.receipt_sha):
        say("blockers adopt-amendment requires --blocker, --approval, --receipt and --receipt-sha")
        return 2
    if args.cmd == "blockers" and args.action == "amendment-status":  # r3 §3.9: read-only, before the lock
        try:
            conn = open_readonly(cfg.state_path)
            try:
                lines = amendment_status(conn, ROLLBACK_TARGET)
            finally:
                conn.close()
        except Exception:
            say("state unavailable")
            return 2
        say(*lines)
        return 0
    if args.cmd == "status":
        try:
            say(*status_lines(cfg.state_path))
        except Exception:
            say("state unavailable")
            return 2
        return 0
    if args.cmd == "run" and not cfg.enabled:
        return 0

    lock = _lock(cfg.runtime_dir)
    if lock is None:
        if args.cmd != "run":
            say("busy: another clickup-intake instance holds the lock; retry")
            return 1
        return 0
    try:
        store = Store(cfg.state_path, clock)
        try:
            closed = _meta_json(store.conn, engine.ADMISSION_KEY)  # r3 §3.8: before any other write
            if closed is not None and (args.cmd, getattr(args, "action", None)) not in ADMISSION_EXEMPT:
                if args.cmd == "run":
                    log = _logger(cfg.runtime_dir)
                    log.info(json.dumps({"event": "admission_closed", "target": closed.get("target")}))
                    for h in list(log.handlers):
                        h.close()
                        log.removeHandler(h)
                    return 0
                say("admission closed (rollback to %s)" % closed.get("target"))
                return 1
            store.ensure_relay_tables()
            now = clock()
            if args.cmd == "hold":
                ok = hold_task(store, args.task, now)
                say("held" if ok else "unknown task")
                return 0 if ok else 1
            if args.cmd == "release":
                ok = release_task(store, args.task)
                say("released" if ok else "unknown task")
                return 0 if ok else 1
            if args.cmd == "blockers" and args.action in ("adopt-amendment", "amendment-enable", "rollback-prepare",
                                                          "admission-open"):  # workflow repair r3 §3.8/§3.9
                if args.action == "adopt-amendment":
                    ok, line = adopt_amendment(store, port, args.blocker, args.approval, args.receipt,
                                               args.receipt_sha, now)
                    line = line if ok else "adopt refused: " + line
                elif args.action == "amendment-enable":
                    ok, line = amendment_enable(store, args.drain_sha, now)
                    line = line if ok else "amendment-enable refused: " + line
                elif args.action == "rollback-prepare":
                    ok, line = rollback_prepare(store, args.target, now)
                else:
                    ok, line = admission_open(store)
                say(line)
                return 0 if ok else 1
            if args.cmd == "blockers":  # r6 §4.1/§7, r9 §7, r11 §9, r12 §9
                if args.action == "rollback-preflight":
                    reasons = rollback_preflight(store)
                    say("rollback preflight " + ("refused: " + "; ".join(reasons) if reasons else "OK"))
                    return 1 if reasons else 0
                if args.action == "protocol-comment":  # D1-A
                    g = Glue(cfg, store, None, port, now)
                    if g.meta(engine.CUTOVER_KEY) is None:
                        say("blockers not enabled")
                        return 1
                    targets = g.protocol_targets()
                    say("protocol comments queued: %d" % len(engine.protocol_comments(store, now, targets, cfg)))
                elif args.action == "enable":
                    with store.transaction() as c:
                        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                  (engine.CUTOVER_KEY, json.dumps(now)))
                    say("blockers enabled; cutover %d" % now)
                else:
                    engine.disable_blockers(store, now, cfg)
                    say("blockers disabled")
                return 0
            if args.cmd == "recovery":  # r3 §8, §11
                if args.action == "enable":
                    with store.transaction() as c:
                        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                  (engine.RECOVERY_KEY, json.dumps(now)))
                    say("recovery enabled; cutover %d" % now)
                elif args.action == "disable":
                    engine.disable_recovery(store, now, cfg)
                    say("recovery disabled")
                elif args.action == "status":
                    say(*recovery_status(store))
                elif args.action == "rollback-check":
                    reasons = recovery_rollback_check(store)
                    say("recovery rollback-check " + ("refused: " + "; ".join(reasons) if reasons else "OK"))
                    return 1 if reasons else 0
                else:
                    ok, line = recovery_adopt(store, port, cfg, args.card, args.run, now)
                    say(line if ok else "adopt refused: " + line)
                    return 0 if ok else 1
                return 0
            if args.cmd == "outbox":
                ok = outbox_command(store, args.action, args.id)
                say(args.action + (" done" if ok else ": no held_ambiguous/failed row with that id"))
                return 0 if ok else 1
            secrets = ()
            if transport is None:
                token = clickup_api.load_token(CREDENTIAL)
                transport, secrets = clickup_api.HttpTransport(token), (token,)
            log = _logger(cfg.runtime_dir)
            try:
                glue = Glue(cfg, store, transport, port, now, secrets=secrets, opener=opener, log=log)
                if args.cmd == "admit":
                    if not glue.preflight():
                        glue.finish()
                        say(*glue.out)
                        return 1
                    ok, why = observe.admit_task(glue.guard.transport(transport), store, cfg, args.task, now)
                    say(*glue.out, ("admitted" if ok else "refused: " + why))
                    return 0 if ok else 1
                try:
                    glue.scan(shadow=args.mode == "shadow" or cfg.mode == "shadow")
                except Exception as e:  # never echo content; class name only
                    log.info(json.dumps({"event": "error", "class": e.__class__.__name__}))
                    say(*glue.out, "ClickUp intake run failed (%s); see logs" % e.__class__.__name__)
                    return 1
                say(*glue.out)
                return 0
            finally:
                for h in list(log.handlers):
                    h.close()
                    log.removeHandler(h)
        finally:
            store.close()
    finally:
        lock.close()
