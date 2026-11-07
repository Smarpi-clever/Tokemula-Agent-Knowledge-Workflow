"""Native recovery and test classification use bounded synthetic protocol data."""
import copy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from akf.execution import recover_receipt, test_command, test_details


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class TestCommands(unittest.TestCase):
    def test_vitest_and_existing_commands(self):
        for command in ('pnpm exec vitest run src/views/user/tests/PaymentView.spec.ts',
                        'pnpm vitest run foo.spec.ts', 'npx vitest run foo.spec.ts',
                        'vitest run foo.spec.ts', 'pnpm test', 'npm run test',
                        'python3 -B -m unittest -v tests/test_limits.py', 'pytest -q', 'go test ./...'):
            with self.subTest(command=command):
                self.assertTrue(test_command(command))

    def test_shell_environment_and_exec_wrappers(self):
        for command in ("/bin/zsh -lc 'pnpm exec vitest run foo.spec.ts'",
                        "bash -c 'env CI=1 pnpm exec vitest run foo.spec.ts'",
                        'CI=true pnpm exec vitest run foo.spec.ts',
                        'exec python3 -B -m unittest', 'env -u NODE_OPTIONS CI=1 pnpm test'):
            with self.subTest(command=command):
                self.assertTrue(test_command(command))

    def test_compound_exit_status_is_not_test_status(self):
        for command in ('pnpm exec vitest run foo.spec.ts; echo success',
                        'pnpm test && echo done', 'pnpm test || true',
                        'cd /fixture && pnpm test', 'pnpm test | cat',
                        'pnpm test\necho done', 'echo $(pnpm test)',
                        'echo `pnpm test`', 'pnpm exec vitest --help',
                        "sh -c 'pnpm test; true'", 'echo pnpm test',
                        "pnpm exec vitest run '$(touch foo)'", 'pnpm exec vitest run >out.txt'):
            with self.subTest(command=command):
                self.assertFalse(test_command(command))

    def test_help_and_version_requests_have_no_test_execution(self):
        for command in ('python3 -m unittest --help', 'python3 -m unittest -h',
                        'pytest --version', 'pytest -V', 'go test -h',
                        'pnpm exec vitest run --help', 'pnpm exec vitest run --version',
                        'pnpm exec vitest run -v', 'pnpm test -- --help'):
            with self.subTest(command=command):
                self.assertFalse(test_command(command))
                self.assertIsNone(test_details(command))

    def test_scope_omits_sensitive_parameters(self):
        for command in ('pnpm exec vitest run --password private-password',
                        'pnpm test -- --token synthetic-secret-for-review',
                        'pnpm test -- --token=synthetic-secret-for-review'):
            with self.subTest(command=command):
                details = test_details(command)
                self.assertEqual(details['scope'], {'state': 'sensitive_selection_omitted'})
                self.assertNotIn('private-password', str(details))
                self.assertNotIn('synthetic-secret-for-review', str(details))

    def test_test_scope_and_statistics_come_from_its_own_command(self):
        command = 'pnpm exec vitest run src/views/user/tests/PaymentView.spec.ts'
        details = test_details(command, ' Test Files  1 passed (1)\n Tests  2 passed | 1 skipped (3)\n')
        self.assertEqual(details['scope']['arguments'], ['src/views/user/tests/PaymentView.spec.ts'])
        self.assertEqual(details['statistics']['tests'], {'total': 3, 'passed': 2, 'skipped': 1})
        self.assertEqual(details['statistics']['test_files'], {'total': 1, 'passed': 1})
        self.assertEqual(test_details('python3 -m unittest', 'Ran 4 tests in 0.002s\nOK')['statistics']['tests']['total'], 4)
        self.assertEqual(test_details(command, 'all tests passed')['count_state'], 'unknown')
        self.assertEqual(test_details(command, 'Tests 2 failed (1)')['count_state'], 'unknown')
        self.assertEqual(test_details(command, 'Tests 1 passed (1)\nTests 2 passed (2)')['count_state'], 'unknown')


class NativeRecovery(unittest.TestCase):
    def setUp(self):
        self.project = Path('/fixture/project')
        self.receipt = {'id': 'receipt', 'session_id': 'session', 'turn_id': 'turn',
                        'tool_use_id': 'call', 'tool_name': 'Bash',
                        'command_sha256': sha('pnpm exec vitest run foo.spec.ts'),
                        'observed_at': '2026-11-05T02:00:00+00:00',
                        'exit_code': None, 'exit_code_state': 'missing_structured_exit_code',
                        'classification': 'execution'}
        self.thread = {'id': 'session', 'cwd': str(self.project)}
        self.item = {'type': 'commandExecution', 'id': 'call',
                     'command': "/bin/zsh -lc 'pnpm exec vitest run foo.spec.ts'",
                     'cwd': str(self.project), 'status': 'completed', 'exitCode': 0}
        self.row = {'turnId': 'turn', 'item': self.item}
        self.client = Mock()
        self.client.request.side_effect = self.response

    def response(self, method, params):
        if method == 'thread/read':
            return {'thread': self.thread}
        self.assertEqual(method, 'thread/items/list')
        self.assertEqual(params['threadId'], 'session')
        self.assertEqual(params['turnId'], 'turn')
        return {'data': [self.row], 'nextCursor': None}

    def recover(self, **kwargs):
        return recover_receipt(self.receipt, self.project, client=self.client, **kwargs)

    def test_success_and_failure_preserve_original_observation(self):
        before = copy.deepcopy(self.receipt)
        for code, state in ((0, 'success'), (4, 'failure')):
            self.item['exitCode'] = code
            recovered = self.recover()
            self.assertEqual(recovered['exit_code'], code)
            self.assertEqual(recovered['execution_state'], state)
            self.assertEqual(recovered['classification'], 'test')
            self.assertEqual(recovered['observed_at'], before['observed_at'])
            self.assertEqual(recovered['original_exit_state']['exit_code'], None)
            self.assertEqual(recovered['native_evidence']['executed_at'], before['observed_at'])
            self.assertEqual(recovered['native_evidence']['execution_time_source'], 'hook_observed_at')
            self.assertIn('supplemented_at', recovered['native_evidence'])
            self.assertEqual(self.receipt, before)

    def test_native_execution_and_supplementation_times_are_separate(self):
        self.row.update(startedAtMs=1793843999000, completedAtMs=1793844000000)
        result = self.recover()
        proof = result['native_evidence']
        self.assertEqual(proof['execution_time_source'], 'native_completedAtMs')
        self.assertEqual(proof['executed_at'], '2026-11-05T02:00:00+00:00')
        self.assertNotEqual(proof['supplemented_at'], proof['executed_at'])
        self.assertEqual(result['observed_at'], self.receipt['observed_at'])

    def test_correlated_native_test_summary_does_not_replace_exit_status(self):
        self.item.update(aggregatedOutput=' Test Files  1 failed (1)\n Tests  1 failed | 2 passed (3)\n', exitCode=1, status='failed')
        result = self.recover()
        self.assertEqual(result['execution_state'], 'failure')
        self.assertEqual(result['test_evidence']['statistics']['tests']['failed'], 1)
        self.assertNotIn('aggregatedOutput', result)
        self.item.update(aggregatedOutput=' Tests  3 passed (3)\n', exitCode=None)
        self.assertIsNone(self.recover()['exit_code'])

    def test_running_interrupted_and_unknown_stay_distinct(self):
        for status, code, expected in (('inProgress', None, 'running'),
                                       ('interrupted', None, 'interrupted'),
                                       ('completed', None, 'unknown'),
                                       ('completed', True, 'unknown'),
                                       ('failed', None, 'failure')):
            with self.subTest(status=status, code=code):
                self.item.update(status=status, exitCode=code)
                result = self.recover()
                self.assertIsNone(result['exit_code'])
                self.assertEqual(result['execution_state'], expected)
                self.assertTrue(result['native_evidence']['reason'])

    def test_identity_mismatches_keep_missing_exit(self):
        changes = [('thread', 'id', 'other-session'), ('thread', 'cwd', '/other/project'),
                   ('row', 'turnId', 'other-turn'), ('item', 'id', 'other-call'),
                   ('item', 'cwd', '/other/project'), ('item', 'command', 'echo success'),
                   ('item', 'type', 'agentMessage')]
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                target = getattr(self, section)
                old = target[key]
                target[key] = value
                result = self.recover()
                self.assertIsNone(result['exit_code'])
                self.assertTrue(result['native_evidence']['reason'])
                target[key] = old

    def test_exact_receipt_cwd_and_validated_identity_are_required(self):
        self.receipt['command_cwd'] = '/fixture/project/subdir'
        self.assertEqual(self.recover()['native_evidence']['reason'], 'command_cwd_mismatch')
        self.item['cwd'] = '/fixture/project/subdir'
        self.assertEqual(self.recover()['exit_code'], 0)

    def test_cached_queries_are_reused_and_recovery_is_idempotent(self):
        cache = {}
        first = self.recover(cache=cache)
        second = self.recover(cache=cache)
        self.assertEqual(self.client.request.call_count, 2)
        self.assertEqual(second['native_evidence']['query_count'], 0)
        self.assertEqual(recover_receipt(first, self.project, client=self.client, cache=cache), first)
        self.assertEqual(self.client.request.call_count, 2)

    def test_same_turn_different_receipts_reuse_pagination(self):
        original = self.client.request.side_effect
        pages = iter([{'data': [], 'nextCursor': 'next'}, {'data': [self.row], 'nextCursor': None}])
        self.client.request.side_effect = lambda method, params: original(method, params) if method == 'thread/read' else next(pages)
        cache = {}
        self.assertEqual(self.recover(cache=cache)['exit_code'], 0)
        self.assertEqual(self.recover(cache=cache)['exit_code'], 0)
        self.assertEqual(self.client.request.call_count, 3)

    def test_query_failure_keeps_concrete_gap(self):
        from akf.install import InstallError
        self.client.request.side_effect = InstallError('Unsupported method')
        result = self.recover()
        self.assertIsNone(result['exit_code'])
        self.assertIn('Unsupported method', result['native_evidence']['reason'])
        cache = {}
        self.recover(cache=cache)
        calls = self.client.request.call_count
        self.recover(cache=cache)
        self.assertEqual(self.client.request.call_count, calls)

    def test_compound_native_command_cannot_change_receipt_identity(self):
        self.item['command'] += '; echo success'
        self.assertEqual(self.recover()['native_evidence']['reason'], 'command_identity_mismatch')

    def test_limited_missing_turn_and_duplicate_tool_ids_stay_unknown(self):
        self.client.request.side_effect = lambda method, params: {'thread': self.thread} if method == 'thread/read' else {'data': [], 'nextCursor': 'more'}
        result = self.recover()
        self.assertEqual(result['native_evidence']['reason'], 'native_item_query_limit')
        self.assertLessEqual(self.client.request.call_count, 11)
        self.client.request.reset_mock()
        self.client.request.side_effect = lambda method, params: {'thread': self.thread} if method == 'thread/read' else {'data': [self.row, self.row], 'nextCursor': None}
        self.assertEqual(self.recover()['native_evidence']['reason'], 'ambiguous_native_tool_identity')

    def test_captured_hook_exit_skips_native_query(self):
        self.receipt.update(exit_code=0, exit_code_state='captured')
        self.assertEqual(self.recover(), self.receipt)
        self.client.request.assert_not_called()

    def test_plain_text_success_never_provides_exit(self):
        self.item.update(exitCode=None, aggregatedOutput='all tests passed; exit code 0')
        self.assertIsNone(self.recover()['exit_code'])


if __name__ == '__main__':
    unittest.main()
