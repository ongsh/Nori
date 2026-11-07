import base64
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from nori.quality_gate import QualityGate, validate_check, check_paths
from nori.store import NoriError


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = {'requirements': {'r':'hello'}, 'operations':{}}

    def evaluate(self,descriptor,decisions=None):
        return QualityGate(self.root,self.spec,decisions).evaluate({'r':descriptor})

    def operation(self,code,**kwargs):
        self.spec['operations']['test'] = {'argv':[sys.executable,'-c',code],**kwargs}
        return {'kind':'command','operation':'test'}

    def test_legacy_exact_snapshots(self):
        (self.root/'a').write_text('hello')
        verdict,checks = self.evaluate('a')
        self.assertEqual(verdict,'PASS')
        self.assertEqual(base64.b64decode(checks[0]['raw_output']['content_base64']),b'hello')
        self.assertEqual(checks[0]['input_snapshot'],checks[0]['output_snapshot'])
        (self.root/'a').write_text('wrong')
        self.assertEqual(self.evaluate('a')[0],'FAIL')

    def test_missing_invalid_encoding_and_large_file(self):
        self.assertEqual(self.evaluate('a')[0],'FAIL')
        (self.root/'a').write_bytes(b'\xff')
        self.assertEqual(self.evaluate('a')[0],'INCONCLUSIVE')
        (self.root/'a').write_bytes(b'x'*1048577)
        self.assertEqual(self.evaluate('a')[0],'INCONCLUSIVE')

    def test_json_pointer_and_type(self):
        (self.root/'a').write_text('{"a/b":{"~key":[true,4]}}')
        d = {'kind':'json_value','path':'a','pointer':'/a~1b/~0key/1','expected':4}
        self.assertEqual(self.evaluate(d)[0],'PASS')
        d.update(pointer='/a~1b/~0key/0',expected=1)
        self.assertEqual(self.evaluate(d)[0],'FAIL')
        d['pointer'] = '/a~1b/~0key/00'
        self.assertEqual(self.evaluate(d)[0],'FAIL')
        d['pointer'] = '/a~2b'
        with self.assertRaises(NoriError): self.evaluate(d)

    def test_manual_current_binding_required(self):
        d = {'kind':'manual','prompt':'Review the rendered fixture'}
        self.assertEqual(self.evaluate(d)[0],'INCONCLUSIVE')
        binding = QualityGate(self.root,self.spec).binding('r',d)
        decision = {'r':{'binding':binding,'verdict':'PASS','reason':'Reviewed fixture'}}
        self.assertEqual(self.evaluate(d,decision)[0],'PASS')
        self.spec['requirements']['r'] = 'changed'
        self.assertEqual(self.evaluate(d,decision)[0],'INCONCLUSIVE')
        self.spec['requirements']['r'] = 'hello'
        (self.root/'a').write_text('changed')
        self.assertEqual(self.evaluate(d,decision)[0],'INCONCLUSIVE')

    def test_command_real_exit_and_output(self):
        d = self.operation("import sys; print('PASS metadata'); print('err',file=sys.stderr); sys.exit(7)")
        verdict,checks = self.evaluate(d)
        self.assertEqual(verdict,'FAIL')
        self.assertEqual(checks[0]['raw_output']['exit_code'],7)
        self.assertEqual(checks[0]['raw_output']['stderr'],'err\n')
        d = self.operation("print('ok')")
        self.assertEqual(self.evaluate(d)[0],'PASS')

    def test_command_timeout_and_output_limits(self):
        d = self.operation('import time; time.sleep(2)',timeout_seconds=.05)
        verdict,checks = self.evaluate(d)
        self.assertEqual(verdict,'INCONCLUSIVE')
        self.assertTrue(checks[0]['raw_output']['timed_out'])
        d = self.operation("print('x'*10000)",max_output_bytes=20)
        verdict,checks = self.evaluate(d)
        self.assertEqual(verdict,'INCONCLUSIVE')
        self.assertTrue(checks[0]['raw_output']['output_limit_exceeded'])
        self.assertEqual(len(checks[0]['raw_output']['stdout']),20)

    def test_command_side_effects_cannot_pass(self):
        d = self.operation("from pathlib import Path; Path('changed').write_text('x')")
        verdict,checks = self.evaluate(d)
        self.assertEqual(verdict,'INCONCLUSIVE')
        self.assertEqual(checks[0]['raw_output']['out_of_scope_changes'],['changed'])

    def test_reject_unsafe_configuration(self):
        for d in ['../a','/tmp/a',{'kind':'command','operation':'missing'}]:
            with self.assertRaises(NoriError): self.evaluate(d)
        for op in [{'argv':['sh','-c','true']},{'argv':[sys.executable],'shell':True},{'argv':[sys.executable],'timeout_seconds':float('inf')},{'argv':[sys.executable],'max_output_bytes':0}]:
            self.spec['operations']['bad'] = op
            with self.assertRaises(NoriError): self.evaluate({'kind':'command','operation':'bad'})
        (self.root/'link').symlink_to('/tmp')
        with self.assertRaises(NoriError): self.evaluate('link/file')

    def test_helpers_and_empty_checks(self):
        self.assertEqual(check_paths('a'),['a'])
        self.assertEqual(check_paths({'kind':'manual','prompt':'p'}),[])
        self.assertEqual(validate_check('a',self.spec,'r')['expected'],'hello')
        self.assertEqual(QualityGate(self.root,self.spec).evaluate({})[0],'INCONCLUSIVE')
