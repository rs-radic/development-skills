"""Native-schema projection of the existing offline board, not invented run history."""
import json
import sqlite3
from pathlib import Path


def install(board, directory):
    def snapshot(roots, take, clock):
        from clickup_intake.preworker_snapshot import acquire
        path = Path(directory) / 'native-read-fixture.db'
        conn = sqlite3.connect(path)
        with conn:
            for table in ('tasks', 'task_links', 'task_runs', 'task_comments', 'task_events', 'task_attachments'):
                conn.execute('DROP TABLE IF EXISTS ' + table)
            conn.executescript('''
            CREATE TABLE tasks(id TEXT PRIMARY KEY, body TEXT, title TEXT, status TEXT, assignee TEXT,
                current_run_id INTEGER, worker_pid INTEGER, last_heartbeat_at INTEGER);
            CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
            CREATE TABLE task_runs(id INTEGER, task_id TEXT, profile TEXT, status TEXT, outcome TEXT,
                metadata TEXT, summary TEXT, worker_pid INTEGER, started_at INTEGER, ended_at INTEGER);
            CREATE TABLE task_comments(id INTEGER, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
            CREATE TABLE task_events(id INTEGER, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
            CREATE TABLE task_attachments(id INTEGER, task_id TEXT, filename TEXT, stored_path TEXT,
                size INTEGER, uploaded_by TEXT, created_at INTEGER);
            ''')
            for card, shown in board.cards.items():
                task = shown['task']
                conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)', tuple(task.get(k) for k in
                    ('id','body','title','status','assignee','current_run_id','worker_pid','last_heartbeat_at')))
                conn.executemany('INSERT INTO task_links VALUES (?,?)', [(p,card) for p in shown.get('parents',[])])
                for r in shown.get('runs',[]):
                    conn.execute('INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (r.get('id'),card,r.get('profile'),r.get('status'),r.get('outcome'),
                         json.dumps(r.get('metadata')),r.get('summary'),r.get('worker_pid'),r.get('started_at'),r.get('ended_at')))
                for i,r in enumerate(shown.get('comments',[])):
                    conn.execute('INSERT INTO task_comments VALUES (?,?,?,?,?)',
                        (i,card,r.get('author'),r.get('body'),r.get('created_at')))
                for i,r in enumerate(shown.get('events',[])):
                    conn.execute('INSERT INTO task_events VALUES (?,?,?,?,?,?)',
                        (i,card,r.get('run_id'),r.get('kind'),json.dumps(r.get('payload')),r.get('created_at')))
                for i,r in enumerate(shown.get('atts',[])):
                    conn.execute('INSERT INTO task_attachments VALUES (?,?,?,?,?,?,?)',
                        (i,card,r.get('filename'),r.get('stored_path'),r.get('size'),r.get('uploaded_by'),r.get('created_at')))
        conn.close()
        return acquire(path, roots, take, clock)
    board._preworker_snapshot = snapshot
