"""Engine uses selected native evidence without changing original task ownership."""
from unittest.mock import Mock, patch

from test_engine import Fixture


class ExecutionIntegrationTests(Fixture):
    def native(self, rows, *, session='session', cwd=None):
        client = Mock()
        def response(method, params):
            if method == 'thread/read':
                return {'thread': {'id': session, 'cwd': str(cwd or self.project)}}
            self.assertEqual(method, 'thread/items/list')
            return {'data': rows, 'nextCursor': None}
        client.request.side_effect = response
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        return client, context

    def receipt(self, tool='test-call', command='pnpm exec vitest run src/views/user/tests/PaymentView.spec.ts'):
        self.engine.start_turn('session', 'native-turn', 'a' * 64)
        saved = self.engine.record_receipt('session', 'native-turn', tool, 'Bash', {'cmd': command}, {})
        return self.engine._get('receipt', saved['receipt_id'])

    def row(self, tool, code=0, command='pnpm exec vitest run src/views/user/tests/PaymentView.spec.ts'):
        return {'turnId':'native-turn', 'startedAtMs':1793843999000, 'completedAtMs':1793844000000,
                'item':{'id':tool, 'type':'commandExecution', 'command':command,
                        'cwd':str(self.project), 'status':'completed', 'exitCode':code}}

    def candidate_for(self, job, receipt, code=0, claim='native-tests'):
        candidate = self.candidate(job, 'The selected Vitest command completed with exit ' + str(code) + '.', 'test_result')
        candidate['updates'][0].update(claim_id=claim, evidence=[{'kind':'receipt','receipt_id':receipt['id'],'expected_exit':code}])
        return candidate

    def test_prepare_supplements_two_tests_with_one_cached_turn_query(self):
        first = self.receipt('first')
        second = self.receipt('second')
        client, context = self.native([self.row('first',0), self.row('second',1)])
        with patch('akf.execution.NativeClient', return_value=context):
            job = self.engine.prepare_maintenance('session','native-turn')
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual([r['exit_code'] for r in job['receipts']], [0,1])
        self.assertEqual([r['execution_state'] for r in job['receipts']], ['success','failure'])
        original = job['receipts'][0]
        self.assertEqual(original['original_exit_state']['exit_code'], None)
        self.assertEqual(original['observed_at'], first['observed_at'])
        self.assertEqual((original['session_id'],original['turn_id']), ('session','native-turn'))
        self.assertEqual(original['native_evidence']['execution_time_source'], 'native_completedAtMs')
        self.assertEqual(self.engine.submit(job['id'], self.candidate_for(job, original))['state'], 'local_saved')
        saved = self.engine._get('fact','topics/limits.md:native-tests')
        self.assertEqual(saved['evidence'][0]['native_evidence']['executed_at'], '2026-11-05T02:00:00+00:00')

    def test_revalidation_recovers_only_referenced_receipt_and_retains_siblings(self):
        receipt = self.receipt()
        job = self.engine.prepare_maintenance('session','native-turn')
        candidate = self.candidate_for(job, receipt)
        candidate['updates'].append(self.candidate(job)['updates'][0])
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['pending_count'], 1)
        source = self.engine._get('fact','topics/limits.md:request-limit')
        client, context = self.native([self.row('test-call')])
        with patch('akf.execution.NativeClient', return_value=context):
            result = self.engine.resume(job['id'])
        self.assertEqual(result['pending_count'], 0)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(self.engine._get('fact','topics/limits.md:request-limit'), source)
        before = (self.knowledge/'topics/limits.md').read_bytes()
        self.engine.resume(job['id'])
        self.assertEqual((self.knowledge/'topics/limits.md').read_bytes(), before)

    def test_identity_failure_has_specific_reason_and_no_knowledge_write(self):
        receipt = self.receipt()
        client, context = self.native([self.row('test-call')], session='other-session')
        with patch('akf.execution.NativeClient', return_value=context):
            job = self.engine.prepare_maintenance('session','native-turn')
            result = self.engine.submit(job['id'], self.candidate_for(job,receipt))
        self.assertEqual(result['pending_count'], 1)
        self.assertIn('session_identity_mismatch', result['pending_updates'][0]['error'])
        self.assertEqual(list((self.knowledge/'topics').iterdir()), [])
        saved = self.engine._get('receipt',receipt['id'])
        self.assertIsNone(saved['exit_code'])
        self.assertEqual(saved['native_evidence']['reason'], 'session_identity_mismatch')

    def test_another_task_receipt_is_rejected_before_any_native_query(self):
        receipt = self.receipt()
        job = self.job()
        with patch('akf.execution.NativeClient') as client:
            result = self.engine.submit(job['id'], self.candidate_for(job,receipt))
        client.assert_not_called()
        self.assertIn('does not belong to this task', result['pending_updates'][0]['error'])

    def test_hook_subdirectory_is_default_receipt_command_cwd(self):
        from akf.hooks import handle_event
        frontend = self.project / 'frontend'
        frontend.mkdir()
        command = 'pnpm exec vitest run foo.spec.ts'
        handle_event({'hook_event_name':'PostToolUse','cwd':str(frontend),'session_id':'session',
                      'turn_id':'native-turn','tool_use_id':'frontend-test','tool_name':'Bash',
                      'tool_input':{'cmd':command},'tool_response':{}}, self.engine)
        row = self.row('frontend-test', command=command)
        row['item']['cwd'] = str(frontend)
        client, context = self.native([row], cwd=frontend)
        with patch('akf.execution.NativeClient', return_value=context):
            job = self.engine.prepare_maintenance('session','native-turn')
        self.assertEqual(job['receipts'][0]['exit_code'], 0)
        self.assertEqual(job['receipts'][0]['command_cwd'], str(frontend))
