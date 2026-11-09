"""Session-scoped durable caches for completed analysis nodes, without credentials."""
import json
import time


class Checkpoints:
    def __init__(self, store):
        self.store = store
        with store.connection(True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS analysis_checkpoints (
                session_id TEXT NOT NULL, node TEXT NOT NULL, input_key TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, error TEXT, updated REAL NOT NULL,
                PRIMARY KEY(session_id,node,input_key))""")

    def get(self, sid, node, key):
        with self.store.connection() as db:
            row = db.execute("SELECT status,result FROM analysis_checkpoints WHERE session_id=? AND node=? AND input_key=?",
                             (sid,node,key)).fetchone()
        return json.loads(row["result"]) if row and row["status"] == "succeeded" else None

    def save(self, sid, node, key, result):
        self._set(sid,node,key,"succeeded",json.dumps(result,ensure_ascii=False,allow_nan=False),None)

    def failed(self, sid, node, key, error, provenance=None):
        # Never replace a completed result with a failed attempt.
        with self.store.connection(True) as db:
            db.execute("""INSERT INTO analysis_checkpoints VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(session_id,node,input_key) DO UPDATE SET status=excluded.status,
                result=excluded.result,error=excluded.error,updated=excluded.updated WHERE analysis_checkpoints.status!='succeeded'""",
                (sid,node,key,"failed",json.dumps(provenance or {},ensure_ascii=False,allow_nan=False),error,time.time()))

    def _set(self,sid,node,key,status,result,error):
        with self.store.connection(True) as db:
            db.execute("""INSERT INTO analysis_checkpoints VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(session_id,node,input_key) DO UPDATE SET status=excluded.status,
                result=excluded.result,error=excluded.error,updated=excluded.updated""",
                (sid,node,key,status,result,error,time.time()))
