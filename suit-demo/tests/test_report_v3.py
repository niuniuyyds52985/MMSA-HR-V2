import os,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from interview_core import InterviewCore,BASE
from interview_research import ResearchStore
class ReportV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{'INTERVIEW_CREDENTIALS_FILE':str(Path(self.tmp.name)/'absent'),'DASHSCOPE_API_KEY':''});env.start();self.addCleanup(env.stop)
        self.core=InterviewCore(self.tmp.name,BASE/'config/interview.v3.json')
        self.sid=self.core.create_interview(uuid.uuid4().hex,'synthetic-report-v3')['session_id']
    def media_answer(self):
        r=self.core.get_state(self.sid)['current_round']
        return self.core.submit_answer(self.sid,r['round_id'],uuid.uuid4().hex,uuid.uuid4().hex,path=str(Path(self.tmp.name)/'media'/'synthetic.mp4'),media_hash='f'*64,media_meta={'duration_ms':1000,'mime_type':'video/mp4'})
    def test_saved_media_is_pending_before_worker_not_unavailable_or_insufficient(self):
        self.media_answer()
        r=self.core.get_report(self.sid)
        self.assertEqual(r['standard_analysis']['pending_count'],2)
        self.assertTrue(all(r['analysis_completeness_by_modality'][m]=='pending' for m in ('au','gaze','expression','audio','gaze360','emotic')))
        self.assertEqual(r['potential_profile']['factors'][0]['state'],'processing')
    def test_replaced_queued_recording_never_blocks_current_asr(self):
        first=self.media_answer()
        second=self.core.submit_answer(self.sid,first['round_id'],uuid.uuid4().hex,uuid.uuid4().hex,path=str(Path(self.tmp.name)/'media'/'new.mp4'),media_hash='e'*64,media_meta={'duration_ms':1000,'mime_type':'video/mp4'},replace_answer_id=first['answer_id'])
        job=self.core.claim_job(job_type='media')
        self.assertEqual(job['answer_id'],second['answer_id'])
        self.assertEqual(job['asr_engine'],'faster-whisper-base-int8.v1')
        with self.core._db() as db:
            old=db.execute("SELECT status FROM jobs WHERE answer_id=?",(first['answer_id'],)).fetchone()[0]
        self.assertEqual(old,'superseded')
    def test_pause_visible_and_history_unchanged(self):
        self.media_answer(); store=ResearchStore(self.core)
        old=self.core.get_report(self.sid)
        with self.core._db(True) as db:
            db.execute('INSERT INTO standard_analysis_controls VALUES(?,?,?,?)',(self.sid,1,'synthetic pause','2026-10-26'))
            self.core._bump(db,self.sid)
        new=self.core.get_report(self.sid)
        self.assertEqual(new['analysis_completeness_by_modality']['audio'],'paused')
        self.assertEqual(self.core.get_report(self.sid,old['version']),old)
if __name__=='__main__': unittest.main()
