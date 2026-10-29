"""Delivered regressions for native scalar, full-scan refusal, and target ancestry."""
import ast,io,json,pathlib,sqlite3,sys,unittest
from unittest import mock
import test_preworker_snapshot as original
from clickup_intake import main,kanban_port as kp,preworker
RECORDS=[]

class PreworkerReviewRegressionTest(unittest.TestCase):
 def case(self,incident=False):
  helper=original.PreworkerSnapshotTest('test_public_large_closure_current_and_changed_parent_refuses')
  self.addCleanup(helper.doCleanups)
  return helper,helper.case(incident)

 def native_fixture(self,helper,c):
  _,old=helper.direct_port(c)
  root=pathlib.Path(main.HERMES_EXE).parents[2]
  module=ast.parse((root/'hermes_cli/kanban_db.py').read_text(encoding='utf-8'))
  schema=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SCHEMA_SQL' for t in n.targets))
  path=pathlib.Path(c.w.dir)/'review-native-schema.db'
  migrations=ast.parse((root/'hermes_cli/kanban_db_connect.py').read_text(encoding='utf-8'))
  later=next(ast.literal_eval(n.value) for n in migrations.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_LATER_TASK_COLUMNS' for t in n.targets))
  with sqlite3.connect(path) as out,sqlite3.connect(old) as src:
   out.executescript(schema)
   existing={r[1] for r in out.execute('PRAGMA table_info(tasks)')}
   for name,ddl in later:
    if name not in existing:out.execute('ALTER TABLE tasks ADD COLUMN '+ddl)
   src.row_factory=sqlite3.Row
   for table in ('tasks','task_links','task_runs','task_comments','task_events','task_attachments'):
    for row in src.execute('SELECT * FROM '+table):
     v=dict(row)
     if table=='tasks':v['created_at']=c.w.clock()//1000
     if table in ('task_comments','task_events','task_attachments'):v.pop('id')
     if table=='task_attachments' and v['created_at'] is None:v['created_at']=c.w.clock()//1000
     keys=list(v);out.execute('INSERT INTO '+table+' ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[v[k] for k in keys])
  sys.path.insert(0,str(root));self.addCleanup(sys.path.remove,str(root))
  import hermes_cli.kanban_db as native
  import hermes_cli.update_lock as locks
  import hermes_cli.venv_sync as sync
  port=kp.KanbanCli(main.HERMES_EXE,'fixture',discord_chat='fixture',runner=lambda *a:self.fail('no native command allowed'))
  self.enterContext(mock.patch.object(native,'kanban_db_path',return_value=path))
  self.enterContext(mock.patch.object(locks,'update_in_progress',return_value=False))
  self.enterContext(mock.patch.object(sync,'completion_pending_path',return_value=path.parent/'absent-marker'))
  return native,path,port

 def check(self,c,port):
  inc=c.incidents()[0];out=io.StringIO()
  rc=main.main(['recovery-check','--card',inc['card'],'--run','0'],root=c.w.root,port=port,clock=c.w.clock,out=out)
  return rc,out.getvalue().strip()

 def test_native_scalar_completion_contract_public_admission(self):
  helper,c=self.case(True);native,path,port=self.native_fixture(helper,c)
  source=c.life.req_cards()[0]
  clean=self.check(c,port);self.assertEqual(clean[0],0,clean)
  with sqlite3.connect(path) as w:w.execute("UPDATE tasks SET completion_contract='local-only' WHERE id=?",(source,))
  with sqlite3.connect(path) as r:
   r.row_factory=sqlite3.Row
   task=native.Task.from_row(r.execute('SELECT * FROM tasks WHERE id=?',(source,)).fetchone())
  from hermes_cli.kanban_output import _task_to_dict
  native_value=_task_to_dict(task)['completion_contract']
  self.assertEqual(native_value,'local-only')
  before=path.read_bytes();actual=self.check(c,port)
  RECORDS.append(dict(test=self._testMethodName,positive_control=clean,native_show_completion_contract=native_value,actual=actual,readonly_unchanged=path.read_bytes()==before))
  self.assertEqual(path.read_bytes(),before)
  self.assertEqual(actual[0],0,'a valid native scalar must not make the complete proof unavailable')

 def test_full_scan_cannot_bypass_refused_early_source(self):
  results=[]
  for mode in ('blocked_parent','changes_requested'):
   helper,c=self.case()
   source=c.life.req_cards()[0]
   if mode=='blocked_parent':
    parent=c.b._new('Still blocked required parent',status='blocked');c.b.cards[source]['parents'].append(parent)
   else:c.b.cards[source]['runs'][-1]['outcome']='changes_requested'
   calls=[];original_execute=main.Glue.execute
   def record(glue,*args,**kwargs):
    calls.append(dict(early=sorted(glue.obs.early_approvals),restricted=kwargs.get('only') is not None))
    return original_execute(glue,*args,**kwargs)
   with mock.patch.object(main.Glue,'execute',new=record):c.scans(1)
   results.append(dict(mode=mode,gate=c.life.gate(),approval=c.applications()[0]['state'],execute=calls))
  RECORDS.append(dict(test=self._testMethodName,results=results))
  self.assertTrue(all(r['gate'] is None for r in results),'normal executor sent after early refusal: '+repr(results))

 def test_native_target_transitive_parent_public_admission(self):
  helper,c=self.case(True)
  # Normal supported staging creates the gate and orchestrator after the admitted incident.
  c.scans(1)
  gate=c.life.gate();self.assertIsNotNone(gate)
  ancestor=c.b._new('Blocked target grandparent',status='blocked')
  parent=c.b._new('Done target parent',status='done')
  c.b.cards[parent]['parents']=[ancestor]
  c.b.cards[gate]['parents'].append(parent)
  native,path,port=self.native_fixture(helper,c)
  bad=self.check(c,port)
  # Direct-parent negative control on the exact same fixture.
  with sqlite3.connect(path) as w:w.execute("UPDATE tasks SET status='blocked' WHERE id=?",(parent,))
  direct=self.check(c,port)
  with sqlite3.connect(path) as w:
   w.execute("UPDATE tasks SET status='done' WHERE id IN (?,?)",(parent,ancestor))
  good=self.check(c,port)
  # Missing required native root remains unavailable, not silently ignored.
  with sqlite3.connect(path) as w:w.execute('DELETE FROM tasks WHERE id=?',(ancestor,))
  missing=self.check(c,port)
  RECORDS.append(dict(test=self._testMethodName,gate=gate,blocked_grandparent=bad,blocked_parent=direct,all_done=good,missing_parent=missing))
  self.assertEqual(direct[0],2);self.assertEqual(good[0],0);self.assertEqual(missing[0],2)
  self.assertEqual(bad[0],2,'collected blocked target ancestor must refuse CURRENT')

 def test_notice_renewal_respects_exhausted_target_read_allowance(self):
  from clickup_intake.store import Store
  helper,c=self.case(True)
  native,path,port=self.native_fixture(helper,c)
  inc=c.incidents()[0]
  c.w.clock.advance(301)
  store=Store(c.w.state_path,c.w.clock)
  self.addCleanup(store.close)
  cfg=main.load_config(pathlib.Path(c.w.runtime)/'config.json')
  glue=main.Glue(cfg,store,c.w.transport,c.b,c.w.clock(),opener=c.w.opener)
  reader=glue.preworker_reader(c.tid)
  for _ in range(main.PREWORKER_READ_LIMIT): self.assertTrue(reader.take())
  wake=next(r for r in c.w.q("SELECT * FROM outbox WHERE kind='kb_complete'")
            if json.loads(r['body']).get('template')=='recovery_wake')
  before=c.w.cu.count()
  self.assertEqual(glue.recovery_gate(wake,json.loads(wake['body'])),'defer')
  self.assertEqual(c.w.cu.calls[before:],[])
  glue=main.Glue(cfg,store,c.w.transport,c.b,c.w.clock(),opener=c.w.opener)
  reader=glue.preworker_reader(c.tid)
  for _ in range(main.PREWORKER_READ_LIMIT): self.assertTrue(reader.take())
  before=c.w.cu.count()
  self.assertEqual(glue.recovery_notice_gate(inc['id']),'defer')
  self.assertEqual(c.w.cu.calls[before:],[], 'exhausted target allowance must prevent notice renewal GETs')
  self.assertEqual(reader.remaining,0)
