"""Exit receipts trust only explicit integer fields, never displayed tool text."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PACKAGE))
from akf.engine import Engine,WorkflowError


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        from akf.install import InstallError
        native = patch('akf.execution.NativeClient', side_effect=InstallError('synthetic native endpoint unavailable'))
        native.start()
        self.addCleanup(native.stop)
        self.temp=tempfile.TemporaryDirectory(prefix='receipt-',dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);project=root/'project';knowledge=root/'knowledge'
        project.mkdir();knowledge.mkdir();(knowledge/'topics').mkdir()
        config=root/'config.json'
        config.write_text(json.dumps({'knowledge_id':'fixture','project_root':str(project),
            'knowledge_root':str(knowledge),'state_dir':str(root/'state'),'enabled':True,
            'environment':'synthetic','project_snapshot':False}))
        self.engine=Engine(config);self.addCleanup(self.engine.db.close)
        self.engine.start_turn('session','turn','a'*64)
        self.number=0

    def record(self,response):
        self.number+=1
        result=self.engine.record_receipt('session','turn',str(self.number),'Bash',{'cmd':'python3 -m unittest'},response)
        return self.engine._get('receipt',result['receipt_id'])

    def test_explicit_integer_fields_keep_success_and_failure(self):
        for response,expected in [({'exit_code':0},0),({'exitCode':7},7),
            ({'result':{'exitCode':1}},1),({'structuredContent':{'exit_code':3}},3),
            ({'exit_code':None,'result':{'exitCode':0}},0)]:
            with self.subTest(response=response):
                receipt=self.record(response)
                self.assertEqual(receipt['exit_code'],expected)
                self.assertEqual(receipt['exit_code_state'],'captured')

    def test_text_arrays_and_multiple_results_remain_missing(self):
        responses=['exit code 0','{"exit_code":0}',
            [{'type':'text','text':'{"exit_code":0}'}],
            {'output':'{"exit_code":0}'},
            {'content':[{'type':'text','text':'{"exitCode":0}'}]},
            {'result':[{'exit_code':0},{'exit_code':1}]},{}]
        for response in responses:
            with self.subTest(response=response):
                receipt=self.record(response)
                self.assertIsNone(receipt['exit_code'])
                self.assertEqual(receipt['exit_code_state'],'missing_structured_exit_code')

    def test_invalid_structured_types_remain_unknown(self):
        for value in [True,False,'0',0.0,{},[]]:
            with self.subTest(value=value):
                receipt=self.record({'exit_code':value})
                self.assertIsNone(receipt['exit_code'])
                self.assertEqual(receipt['exit_code_state'],'invalid_structured_exit_code')

    def test_conflicting_explicit_fields_do_not_select_a_success(self):
        for response in [{'exit_code':0,'result':{'exitCode':1}},
                         {'exit_code':0,'exitCode':2}]:
            receipt=self.record(response)
            self.assertIsNone(receipt['exit_code'])
            self.assertEqual(receipt['exit_code_state'],'conflicting_structured_exit_codes')
        receipt=self.record({'exit_code':0,'result':{'exitCode':0}})
        self.assertEqual(receipt['exit_code'],0)
        self.assertEqual(receipt['exit_code_state'],'captured')

    def test_help_exit_zero_stays_execution_and_test_claim_stays_pending(self):
        for number, command in enumerate(('python3 -m unittest --help', 'pnpm exec vitest run --help')):
            with self.subTest(command=command):
                result = self.engine.record_receipt('session', 'turn', 'help-' + str(number), 'Bash',
                    {'command': command}, {'exit_code': 0})
                receipt = self.engine._get('receipt', result['receipt_id'])
                self.assertEqual(receipt['classification'], 'execution')
                with self.assertRaisesRegex(WorkflowError, 'Test claims need'):
                    self.engine._evidence({'session_id': 'session', 'turn_id': 'turn'},
                        {'content': 'Test command passed.', 'claim_type': 'test_result', 'evidence': [
                            {'kind': 'receipt', 'receipt_id': receipt['id'], 'expected_exit': 0}]})

    def test_token_value_is_omitted_from_persisted_test_scope(self):
        result = self.engine.record_receipt('session', 'turn', 'token', 'Bash',
            {'command': 'pnpm test -- --token synthetic-secret-for-review'}, {'exit_code': 0})
        receipt = self.engine._get('receipt', result['receipt_id'])
        self.assertNotIn('synthetic-secret-for-review', json.dumps(receipt))
        self.assertEqual(receipt['test_evidence']['scope'], {'state': 'sensitive_selection_omitted'})

    def test_vitest_exec_is_a_test_receipt(self):
        result = self.engine.record_receipt('session', 'turn', 'vitest', 'Bash',
            {'command': 'pnpm exec vitest run src/views/user/tests/PaymentView.spec.ts'}, {'exit_code': 0})
        self.assertEqual(self.engine._get('receipt', result['receipt_id'])['classification'], 'test')

    def test_missing_exit_receipt_stays_pending_with_precise_reason(self):
        receipt=self.record([{'type':'text','text':'exit_code:0'}])
        job={'session_id':'session','turn_id':'turn'}
        update={'content':'Command finished.','claim_type':'execution_result','evidence':[
            {'kind':'receipt','receipt_id':receipt['id'],'expected_exit':0}]}
        with self.assertRaisesRegex(WorkflowError,'missing_structured_exit_code'):
            self.engine._evidence(job,update)
        self.assertEqual(list((self.engine.knowledge_root/'topics').iterdir()),[])


if __name__=='__main__':unittest.main(verbosity=2)
