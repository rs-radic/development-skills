"""Task-local native read snapshot; never opens an initializing/writable connection.

Uses the existing tasks/links/runs/comments/events/attachments schema. Per-card
observations are discovery hints only. One SQLite read transaction supplies the
whole ancestor closure, including a completed orchestrator's promotion target.
The same open connection's data_version invalidates the proof after any native
commit; it is never persisted or treated as a cross-connection revision token.
"""
import hashlib
import json
from pathlib import Path
import sqlite3

from . import kanban_port as kp


class Snapshot:
    def __init__(self, conn, path, take, clock):
        self.conn, self.path, self.take, self.clock = conn, Path(path), take, clock
        self.started = clock()
        self.identity = self._identity()
        self.shows, self.attachments, self.files = {}, {}, {}
        self.native_stable = lambda: True
        self.version = self.query('PRAGMA data_version')[0][0]

    def _identity(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino

    def query(self, sql, args=()):
        if not self.take():
            raise ValueError('native read allowance exhausted')
        return self.conn.execute(sql, args).fetchall()

    def valid(self):
        try:
            if self.conn is None or not self.native_stable() or self._identity() != self.identity:
                return False
            for path, digest in self.files.items():
                with open(path, 'rb') as stream:
                    if hashlib.sha256(stream.read(1048577)).hexdigest() != digest:
                        return False
            return (self.query('PRAGMA data_version')[0][0] == self.version
                    and 0 <= self.clock() - self.started <= 600000)
        except (OSError, ValueError, sqlite3.Error):
            return False

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __del__(self):
        self.close()


def acquire(path, roots, take, clock):
    """Nine fixed SELECT/PRAGMA reads plus final proof checks, independent of N.

    Failure or a >5-minute acquisition yields no snapshot. Attachment *bytes*
    are verified by the existing source reader after acquisition and remain
    outside SQLite's transaction; they are never considered a revision token.
    """
    proof = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2,
                               check_same_thread=False)  # finalization may run on a different caller thread
        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda b: b.decode('utf-8', 'replace')
        proof = Snapshot(conn, path, take, clock)
        conn.execute('BEGIN')
        roots = set(roots)
        initial = proof.query('SELECT r.*,t.status AS task_status FROM task_runs r JOIN tasks t ON t.id=r.task_id '
                              'WHERE r.task_id IN (SELECT value FROM json_each(?)) ORDER BY r.id',
                              (json.dumps(sorted(roots)),))
        for task in roots.copy():
            runs = [_run(r) for r in initial if r['task_id'] == task]
            rows = [r for r in initial if r['task_id'] == task]
            shown = dict(task=dict(status=rows[0]['task_status'] if rows else None), runs=runs)
            gates = kp.clickup_gates(shown)
            if gates:
                roots.add(gates['promotion'])
        prefix = ('WITH RECURSIVE needed(id) AS (SELECT value FROM json_each(?) UNION '
                  'SELECT l.parent_id FROM task_links l JOIN needed n ON l.child_id=n.id) ')
        args = (json.dumps(sorted(roots)),)
        tasks = proof.query(prefix + 'SELECT * FROM tasks WHERE id IN needed', args)
        for r in tasks:
            task = dict(r)
            # Native Task.from_row decodes skills; completion_contract is opaque text.
            for key in ('skills',):
                if isinstance(task.get(key), str):
                    task[key] = json.loads(task[key])
            proof.shows[r['id']] = dict(task=task, parents=[], children=[], runs=[], comments=[], events=[],
                                         latest_summary=None)
            proof.attachments[r['id']] = []
        links = proof.query(prefix + 'SELECT * FROM task_links WHERE child_id IN needed OR parent_id IN needed', args)
        for r in links:
            if r['child_id'] in proof.shows: proof.shows[r['child_id']]['parents'].append(r['parent_id'])
            if r['parent_id'] in proof.shows: proof.shows[r['parent_id']]['children'].append(r['child_id'])
        for table, key in (('task_runs', 'runs'), ('task_comments', 'comments'),
                           ('task_events', 'events'), ('task_attachments', 'attachments')):
            rows = proof.query(prefix + 'SELECT * FROM ' + table + ' WHERE task_id IN needed ORDER BY id', args)
            for row in rows:
                r = dict(row)
                task = r.pop('task_id')
                if task not in proof.shows:
                    continue
                if key == 'runs':
                    r = _run(r)
                    if r.get('summary'): proof.shows[task]['latest_summary'] = r['summary']
                if key == 'events' and isinstance(r.get('payload'), str):
                    r['payload'] = json.loads(r['payload'])
                (proof.attachments[task] if key == 'attachments' else proof.shows[task][key]).append(r)
        conn.execute('COMMIT')
        if not proof.valid() or clock() - proof.started > 300000:
            proof.close()
            return None
        return proof
    except (OSError, ValueError, TypeError, sqlite3.Error):
        if proof is not None: proof.close()
        return None


def _run(row):
    out = dict(row)
    out.pop('task_id', None)
    out.pop('task_status', None)
    if isinstance(out.get('metadata'), str): out['metadata'] = json.loads(out['metadata'])
    return out
