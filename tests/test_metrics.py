import math
import unittest
from unittest.mock import patch
from collector.data_collector import Metrics

class MetricsTests(unittest.TestCase):
    def make(self):
        with patch("prometheus_client.start_http_server", return_value=(None,None)):
            return Metrics(dict(SITE="site01",DEVICES=["fz01","fz02"],METRICS_PORT=9201,METRICS_HOST="127.0.0.1"))

    def test_states_labels_stale_and_registry(self):
        m = self.make()
        state = dict(status="normal",temp=-30,ts=1,eta=None,door=True,cooling=False)
        m.update(state,"site01","fz01")
        def value(name, device="fz01"):
            return m.registry.get_sample_value("coldguard_freezer_"+name,dict(site="site01",device=device))
        self.assertEqual(value("door_open"),1)
        self.assertEqual(value("cooling"),0)
        self.assertTrue(math.isnan(value("door_open","fz02")))
        m.update({**state,"door":False,"cooling":True},"site01","fz01")
        self.assertEqual(value("door_open"),0)
        self.assertEqual(value("cooling"),1)
        m.update({**state,"status":"stale"},"site01","fz01")
        self.assertTrue(math.isnan(value("door_open")))
        self.assertTrue(math.isnan(value("cooling")))
        self.make()  # Registries must not collide.

class QueueTests(unittest.TestCase):
    def test_persisted_cursor_failure_and_restart(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from datetime import datetime
        from common.coldguard_runtime import EventSender
        class DB:
            rows=[]
            def query(self, sql, args):
                rows=[r for r in self.rows if r['site_id']==args[0] and r['id']>args[1] and r['event_type'] in args[2:]]
                return [{'total':len(rows)}] if 'COUNT(*)' in sql else rows[:100]
        db=DB()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db'
            conn=sqlite3.connect(path)
            sender=EventSender(db,conn,'site01',('high_temp',))
            self.assertEqual(sender.pending_count(),0)
            db.rows=[dict(id=1,site_id='site01',device_id='fz01',event_type='high_temp',severity='critical',temp_c=-24,predicted_sec=None,detail='hot',occurred_at=datetime(2026,1,1))]
            self.assertEqual(sender.pending_count(),1)
            def fail(*args): raise ConnectionError('test')
            with self.assertRaises(ConnectionError): sender.flush(fail)
            self.assertEqual(sender.pending_count(),1)
            conn.close()
            conn=sqlite3.connect(path)
            sender=EventSender(db,conn,'site01',('high_temp',))
            self.assertEqual(sender.pending_count(),1)
            sender.flush(lambda *args: None)
            self.assertEqual(sender.pending_count(),0)
            conn.close()
