"""Durable local state ``state.sqlite3`` (plan r5 §2.2, §3, §8.5).

WAL journal mode; one write transaction per decision step. ``observe_commit`` and
``renew_ticket`` are the only ticket-mutating store functions owned by S0; the
read-only opener serves ``ticket-check``.
"""

import contextlib
import pathlib
import sqlite3
from typing import NamedTuple

from .contracts import ApprovalState, Phase, RowState, TicketSnapshot

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000


def _in(values):
    return ", ".join("'%s'" % v for v in values)


_V1 = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    f"""CREATE TABLE tasks (
        clickup_task_id TEXT PRIMARY KEY,
        eligible INTEGER NOT NULL DEFAULT 0,
        requester_id INTEGER,
        phase TEXT CHECK (phase IS NULL OR phase IN ({_in(p.value for p in Phase)})),
        last_status_seen TEXT,
        list_fp TEXT,
        content_hash TEXT,
        attach_ids_hash TEXT,
        quiet_until_ms INTEGER,
        input_gen INTEGER NOT NULL DEFAULT 0,
        classified_gen INTEGER NOT NULL DEFAULT 0,
        scope_gen INTEGER NOT NULL DEFAULT 0,
        published_rev INTEGER,
        published_sha256 TEXT,
        published_accounts_gen INTEGER,
        next_rev INTEGER,
        last_status_written TEXT,
        last_light_ms INTEGER,
        last_full_ms INTEGER,
        last_task_get_ms INTEGER,
        full_cursor TEXT,
        full_started_ms INTEGER,
        barrier_ok_ms INTEGER,
        hold_reason TEXT,
        terminal INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE items (
        task TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('comment', 'reply', 'attachment')),
        id TEXT NOT NULL,
        parent_id TEXT,
        author_id INTEGER,
        date_ms INTEGER,
        first_hash TEXT,
        cur_hash TEXT,
        first_mentions_bot INTEGER NOT NULL DEFAULT 0,
        cur_mentions_bot INTEGER NOT NULL DEFAULT 0,
        shape TEXT,
        shape_sig TEXT,
        reply_count INTEGER NOT NULL DEFAULT 0,
        first_seen_ms INTEGER,
        deleted INTEGER NOT NULL DEFAULT 0,
        gen INTEGER,
        command_result TEXT,
        delivered_relay TEXT,
        PRIMARY KEY (task, kind, id))""",
    """CREATE TABLE cards (
        card_id TEXT PRIMARY KEY,
        task TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('request', 'orchestrator', 'gate')),
        seq INTEGER,
        rev INTEGER,
        approval_id INTEGER,
        last_run_id INTEGER,
        wait_run_id INTEGER,
        created_key TEXT)""",
    """CREATE TABLE relays (
        relay_ref TEXT PRIMARY KEY,
        task TEXT NOT NULL,
        card TEXT,
        gen INTEGER,
        item_ids TEXT,
        state TEXT NOT NULL CHECK (state IN ('staged', 'delivered', 'classified')),
        classification TEXT)""",
    "CREATE TABLE kanban_seen (key TEXT PRIMARY KEY, card TEXT, consumed_ms INTEGER)",
    f"""CREATE TABLE approvals (
        id INTEGER PRIMARY KEY,
        task TEXT NOT NULL,
        rev INTEGER NOT NULL,
        accounts_gen INTEGER NOT NULL,
        comment_id TEXT,
        author_id INTEGER,
        author_name TEXT,
        comment_date INTEGER,
        text_hash TEXT,
        plan_sha256 TEXT,
        state TEXT NOT NULL CHECK (state IN ({_in(s.value for s in ApprovalState)})),
        orchestrator_card TEXT,
        gate_card TEXT,
        clickup_gates TEXT,
        ticket_epoch INTEGER NOT NULL DEFAULT 0,
        ticket_input_gen INTEGER,
        ticket_renewed_ms INTEGER,
        revoked_reason TEXT,
        revoked_ms INTEGER,
        audit_stamp_state TEXT NOT NULL DEFAULT 'none' CHECK (audit_stamp_state IN ('none', 'valid', 'revoked')),
        promoted_sha TEXT,
        UNIQUE (task, rev, accounts_gen))""",
    f"""CREATE TABLE outbox (
        idem_key TEXT PRIMARY KEY,
        task TEXT,
        kind TEXT NOT NULL,
        target TEXT,
        payload_sha256 TEXT,
        body TEXT,
        preconditions TEXT,
        depends_on TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ({_in(s.value for s in RowState)})),
        outcome TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_ms INTEGER,
        first_send_ms INTEGER,
        child_pid INTEGER,
        child_start_time TEXT,
        result_ref TEXT)""",
]

MIGRATIONS = {1: _V1}

_TASK_COLUMNS = (
    "eligible", "requester_id", "phase", "last_status_seen", "list_fp", "content_hash", "attach_ids_hash",
    "quiet_until_ms", "published_rev", "published_sha256", "published_accounts_gen", "next_rev",
    "last_status_written", "last_light_ms", "last_full_ms", "last_task_get_ms", "full_cursor", "full_started_ms",
    "barrier_ok_ms", "hold_reason", "terminal",
)  # settable by observe_commit; input/classified/scope gens are not


class ObserveResult(NamedTuple):
    input_gen: int
    revoked: list


def _schema_version(conn):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def revoke_live_tickets(conn, task_id, reason, now_ms):
    """Revoke every ``ticketed`` approval of ``task_id``; call inside the caller's write transaction."""
    ids = [r[0] for r in conn.execute("SELECT id FROM approvals WHERE task=? AND state='ticketed' ORDER BY id",
                                      (task_id,))]
    if ids:
        conn.execute("UPDATE approvals SET state='revoked', revoked_reason=?, revoked_ms=? "
                     "WHERE task=? AND state='ticketed'", (reason, now_ms, task_id))
    return ids


class Store:
    """Writer connection to ``state.sqlite3``. ``clock`` returns epoch milliseconds."""

    def __init__(self, path, clock):
        self.path = str(path)
        self.clock = clock
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise RuntimeError("state.sqlite3 could not enter WAL mode")
            self._migrate()
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    @contextlib.contextmanager
    def transaction(self):
        """One write transaction (BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any error)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _migrate(self):
        with self.transaction() as c:
            version = _schema_version(c)
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"state.sqlite3 schema {version} is newer than supported {SCHEMA_VERSION}")
            for v in range(version + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS[v]:
                    c.execute(stmt)
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(v),))

    def observe_commit(self, task, items, task_fields):
        """Store one task pass in a single transaction (§4.1 step 4, §8.5).

        Stores items and task fields, stamps ``gen = ++input_gen`` per material item,
        raises ``input_gen`` on a changed ``content_hash``, and revokes a live ticket when
        the task is terminal/held or ``input_gen`` moved past ``ticket_input_gen``.
        """
        bad = set(task_fields) - set(_TASK_COLUMNS)
        if bad:
            raise ValueError(f"observe_commit cannot set {sorted(bad)}")
        now = self.clock()
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO tasks(clickup_task_id) VALUES (?)", (task,))
            prev = c.execute("SELECT content_hash, input_gen FROM tasks WHERE clickup_task_id=?", (task,)).fetchone()
            gen = prev["input_gen"]
            if "content_hash" in task_fields and prev["content_hash"] is not None \
                    and task_fields["content_hash"] != prev["content_hash"]:
                gen += 1
            for it in items:
                item_gen = None
                if it.material:
                    gen += 1
                    item_gen = gen
                c.execute(
                    "INSERT INTO items(task, kind, id, parent_id, author_id, date_ms, first_hash, cur_hash,"
                    " first_mentions_bot, cur_mentions_bot, shape, shape_sig, reply_count, first_seen_ms, deleted,"
                    " gen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(task, kind, id) DO UPDATE SET parent_id=excluded.parent_id,"
                    " author_id=excluded.author_id, date_ms=excluded.date_ms, cur_hash=excluded.cur_hash,"
                    " cur_mentions_bot=excluded.cur_mentions_bot, shape=excluded.shape,"
                    " shape_sig=excluded.shape_sig, reply_count=excluded.reply_count, deleted=excluded.deleted,"
                    " gen=COALESCE(excluded.gen, items.gen)",
                    (task, it.kind, it.id, it.parent_id, it.author_id, it.date_ms, it.hash, it.hash,
                     int(it.mentions_bot), int(it.mentions_bot), it.shape, it.shape_sig, it.reply_count, now,
                     int(it.deleted), item_gen))
            cols = list(task_fields)
            c.execute(f"UPDATE tasks SET {''.join(k + '=?, ' for k in cols)}input_gen=? WHERE clickup_task_id=?",
                      [task_fields[k] for k in cols] + [gen, task])

            row = c.execute("SELECT terminal, hold_reason, input_gen FROM tasks WHERE clickup_task_id=?",
                            (task,)).fetchone()
            if row["terminal"]:
                reason = "terminal"
            elif row["hold_reason"]:
                reason = "hold"
            elif c.execute("SELECT 1 FROM approvals WHERE task=? AND state='ticketed'"
                           " AND (ticket_input_gen IS NULL OR ticket_input_gen <> ?)", (task, gen)).fetchone():
                reason = "new_input"
            else:
                reason = None
            revoked = []
            if reason and c.execute("SELECT 1 FROM approvals WHERE task=? AND state='ticketed'", (task,)).fetchone():
                revoked = revoke_live_tickets(c, task, reason, now)
        return ObserveResult(gen, revoked)

    def renew_ticket(self, approval_id, epoch, now):
        """Compare-and-set lease renewal (§8.5); True only if the ticket is still current."""
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE approvals SET ticket_renewed_ms=? WHERE id=? AND state='ticketed' AND ticket_epoch=?"
                " AND EXISTS (SELECT 1 FROM tasks t WHERE t.clickup_task_id=approvals.task"
                " AND t.input_gen=approvals.ticket_input_gen AND t.classified_gen=t.input_gen"
                " AND t.terminal=0 AND (t.hold_reason IS NULL OR t.hold_reason=''))",
                (now, approval_id, epoch))
        return cur.rowcount == 1


def open_readonly(path):
    """Read-only connection (``file:...?mode=ro`` URI, busy_timeout 5 s); never creates or writes the file."""
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def load_ticket_snapshot(path, task_id, lease_seconds):
    """Read the task row and its approval rows in one read transaction; None if unavailable/wrong schema."""
    try:
        conn = open_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        conn.execute("BEGIN")
        if _schema_version(conn) != SCHEMA_VERSION:
            return None
        task = conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (task_id,)).fetchone()
        approvals = conn.execute("SELECT * FROM approvals WHERE task=? ORDER BY id", (task_id,)).fetchall()
        conn.execute("COMMIT")
        return TicketSnapshot(task=dict(task) if task else None, approvals=[dict(a) for a in approvals],
                              lease_seconds=lease_seconds)
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        conn.close()
