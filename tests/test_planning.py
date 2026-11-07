import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from nori.planning import PlanningService, _validate_plan
from nori.store import NoriError


PLAN = {'goal':'Build a fixture report','requirements':{'R1':'Report is useful'},
        'tasks':{'T1':{'why':'Produce report','requirements':['R1'],'depends_on':[],
                       'write_scope':['report.txt'],'read_scope':[],'executor':'codex',
                       'max_attempts':2,'checks':{'R1':{'kind':'manual','prompt':'Review report'}}}},
        'final_checks':{'R1':{'kind':'manual','prompt':'Review report'}}}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.executable = self.root/'fixture-codex'
        self.fixture(PLAN)
        self.service = PlanningService(self.root/'app',executables={'codex':str(self.executable)})

    def fixture(self, plan=None, *, delay=0, exit_code=0, valid_help=True):
        event = json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(plan)}})
        self.executable.write_text('#!'+sys.executable+'\nimport sys,time\n'
            "if '--version' in sys.argv: print('fixture 1'); sys.exit(0)\n"
            "if '--help' in sys.argv: print("+repr('--sandbox read-only --json --skip-git-repo-check' if valid_help else '--unsafe')+"); sys.exit(0)\n"
            "if sys.argv[1:] == ['login','status']: sys.exit(0)\n"
            "assert sys.argv[1:] == ['exec','--json','--sandbox','read-only','--skip-git-repo-check','-']\n"
            "prompt=sys.stdin.read(); assert 'NOT an executor' in prompt\n"
            f'time.sleep({delay})\nprint({event!r},flush=True)\nsys.exit({exit_code})\n')
        self.executable.chmod(0o700)

    def finish(self,service,sid):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            result = service.observe(sid)
            if result['status'] in {'PROPOSED','FAILED','STOPPED','UNAVAILABLE'}:
                return result
            time.sleep(.02)
        self.fail('Planning did not terminate: '+str(result))

    def test_persistent_plan_only_fixture(self):
        sid = self.service.create('Build me a useful report',previous_conversation=[{'role':'user','content':'Use plain text'}])
        result = self.finish(self.service,sid)
        self.assertEqual(result['status'],'PROPOSED',result)
        self.assertEqual(result['proposed_plan'],PLAN)
        self.assertFalse(result['approved'])
        self.assertFalse(result['executed'])
        self.assertTrue(result['raw_stdout'])
        reopened = PlanningService(self.root/'app')
        self.assertEqual(reopened.read(sid)['proposed_plan'],PLAN)
        self.assertEqual(reopened.list()[0]['id'],sid)
        self.assertEqual(list((self.root/'app'/'planning'/sid/'work').iterdir()),[])

    def test_no_commands_or_mock_allowed(self):
        for key,value in [('operations',{}),('mock_outputs',{}),('argv',['echo','bad'])]:
            plan=copy.deepcopy(PLAN); plan[key]=value
            with self.assertRaises(NoriError): _validate_plan(plan,'codex')
        plan=copy.deepcopy(PLAN); plan['tasks']['T1']['executor']='mock'
        with self.assertRaises(NoriError): _validate_plan(plan,'codex')

    def test_invalid_proposal_preserves_raw(self):
        self.fixture({'goal':'not a complete spec'})
        sid=self.service.create('Test invalid output')
        result=self.finish(self.service,sid)
        self.assertEqual(result['status'],'FAILED')
        self.assertIsNone(result['proposed_plan'])
        self.assertIn('not a complete spec',result['raw_stdout'])

    def test_failed_exit_cannot_propose(self):
        self.fixture(PLAN,exit_code=7)
        result=self.finish(self.service,self.service.create('Test failure'))
        self.assertEqual(result['status'],'FAILED')
        self.assertIsNone(result['proposed_plan'])

    def test_stop_and_budget(self):
        self.fixture(PLAN,delay=2)
        sid=self.service.create('Slow fixture')
        self.assertEqual(self.service.stop(sid)['status'],'STOP_REQUESTED')
        self.assertEqual(self.finish(self.service,sid)['status'],'STOPPED')
        service=PlanningService(self.root/'bounded',executables={'codex':str(self.executable)},max_seconds=.1)
        result=self.finish(service,service.create('Bounded fixture'))
        self.assertEqual(result['status'],'STOPPED')
        self.assertEqual(result['error'],'max_seconds')

    def test_probe_truthful_and_no_unsafe_fallback(self):
        self.fixture(PLAN,valid_help=False)
        result=self.service.read(self.service.create('Need safe planning'))
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertNotIn('argv',result)
        service=PlanningService(self.root/'missing',executables={'codex':'nori-nonexistent-fixture-cli','claude':str(self.executable)})
        self.assertEqual(service.probe('codex')['status'],'not_installed')
        self.assertEqual(service.probe('claude')['status'],'unsupported')

    def test_invalid_inputs_and_session_traversal(self):
        for request in ['',None,'x'*32001]:
            with self.assertRaises(NoriError): self.service.create(request)
        with self.assertRaises(NoriError): self.service.create('request','/tmp/arbitrary')
        with self.assertRaises(NoriError): self.service.read('../anything')
        with self.assertRaises(NoriError): self.service.create('request',previous_conversation=[{'role':'system','content':'unsafe'}])

    def test_missing_receipt_never_relaunches(self):
        sid=self.service.create('Fixture plan')
        self.finish(self.service,sid)
        p=self.root/'app'/'planning'/sid/'session.json'
        session=json.loads(p.read_text()); session['status']='STARTING'; p.write_text(json.dumps(session))
        receipt=self.root/'app'/'planning'/sid/'control'/'process-receipts'/sid/'receipt.json'
        receipt.unlink()
        self.assertEqual(self.service.observe(sid)['status'],'UNKNOWN')
        self.assertFalse(receipt.exists())
