import copy
from unittest.mock import patch

from akf.engine import WorkflowError, digest
import test_multifact
from test_engine import Fixture


class ReviewTests(Fixture):
    batch = test_multifact.MultiFactTests.batch

    def test_explain_pending_and_manual_confirmation_preserve_evidence_gap(self):
        job, candidate, complete = self.batch(pending=True)
        self.engine.submit(job['id'], candidate)
        result = self.engine.explain(job['id'])
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['item_id'], 'topics/limits.md:other-value')
        self.assertEqual(item['proposed_change'], 'add')
        self.assertIn('objective evidence', item['evidence_gap'])
        result = self.engine.review(job['id'], 'confirm', item['item_id'])
        self.assertIsNotNone(result['items'][0]['manual_confirmation'])
        self.assertEqual(result['pending_count'], 1)
        self.assertEqual(result['items'][0]['evidence_status'], 'unverified')
        self.assertEqual(result['review_history'][-1]['verification'], 'manual_confirmation')

    def test_revalidate_one_item_preserves_saved_sibling_and_records_action(self):
        job, candidate, complete = self.batch(pending=True)
        self.engine.submit(job['id'], candidate)
        first = self.engine._get('fact', 'topics/limits.md:request-limit')
        result = self.engine.review(job['id'], 'revalidate', 'topics/limits.md:other-value', complete['updates'][1])
        self.assertEqual(result['pending_count'], 0)
        self.assertTrue(result['complete'])
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit'), first)
        self.assertEqual(result['review_history'][-1]['verification'], 'tool_validation')

    def test_reject_and_withdraw_one_item_leave_saved_fact_unchanged(self):
        for action in ('reject', 'withdraw'):
            job, candidate, complete = self.batch(pending=True)
            self.engine.submit(job['id'], candidate)
            note = self.knowledge / 'topics/limits.md'
            before = note.read_bytes()
            result = self.engine.review(job['id'], action, 'topics/limits.md:other-value')
            self.assertEqual(result['pending_count'], 0)
            self.assertTrue(result['complete'])
            self.assertEqual(result['items'][0]['resolution'], 'rejected' if action == 'reject' else 'withdrawn')
            self.assertEqual(note.read_bytes(), before)
            self.engine.resume(job['id'])
            self.assertEqual(note.read_bytes(), before)

    def test_review_retains_original_cas_and_manual_block_protection(self):
        job, candidate, complete = self.batch(pending=True)
        self.engine.submit(job['id'], candidate)
        note = self.knowledge / 'topics/limits.md'
        note.write_text(note.read_text() + '\nHuman paragraph.\n')
        before = note.read_bytes()
        update = complete['updates'][1]
        result = self.engine.review(job['id'], 'revalidate', 'topics/limits.md:other-value', update)
        self.assertEqual(result['state'], 'conflict')
        self.assertEqual(result['items'][0]['proposed_change'], 'add')
        self.assertTrue(result['items'][0]['conflict_reason'])
        update['expected_sha256'] = digest(before)
        with self.assertRaises(WorkflowError):
            self.engine.review(job['id'], 'revalidate', 'topics/limits.md:other-value', update)
        self.assertEqual(note.read_bytes(), before)

    def test_dismiss_only_one_of_multiple_pending_entries(self):
        job, candidate, complete = self.batch(pending=True)
        third = {**copy.deepcopy(candidate['updates'][1]), 'claim_id': 'third-value'}
        candidate['updates'].append(third)
        self.engine.submit(job['id'], candidate)
        result = self.engine.review(job['id'], 'withdraw', 'topics/limits.md:other-value')
        self.assertEqual(result['pending_count'], 1)
        self.assertFalse(result['complete'])
        self.engine.review(job['id'], 'revalidate', 'topics/limits.md:third-value')
        self.assertEqual(self.engine.status(job['id'])['pending_count'], 1)
        self.assertEqual(len(self.engine.explain(job['id'])['items']), 2)

    def test_conflict_candidate_is_explainable_and_can_be_withdrawn(self):
        job = self.job()
        candidate = self.candidate(job)
        note = self.knowledge / 'topics/limits.md'
        note.write_text('Human note.\n')
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'conflict')
        result = self.engine.explain(job['id'])
        self.assertTrue(result['items'][0]['conflict_reason'])
        result = self.engine.review(job['id'], 'withdraw', 'topics/limits.md:request-limit')
        self.assertEqual(result['state'], 'cancelled')
        self.engine.resume(job['id'])
        self.assertEqual(note.read_text(), 'Human note.\n')

    def test_new_test_receipt_can_complete_pending_without_new_maintenance_hint(self):
        job = self.job()
        candidate = self.candidate(job, 'Focused test passed.', 'test_result')
        candidate['updates'][0]['evidence'] = []
        self.engine.submit(job['id'], candidate)
        receipt = self.engine.record_receipt('session', job['turn_id'], 'supplemental-test', 'Bash',
            {'cmd': 'python3 -m unittest'}, {'exit_code': 0}, prefinal_hint=True)
        self.assertFalse(receipt['first_receipt'])
        update = copy.deepcopy(candidate['updates'][0])
        update['evidence'] = [{'kind': 'receipt', 'receipt_id': receipt['receipt_id'], 'expected_exit': 0}]
        result = self.engine.review(job['id'], 'revalidate', 'topics/limits.md:request-limit', update)
        self.assertEqual(result['state'], 'local_saved')
        self.assertEqual(result['pending_count'], 0)

    def test_pause_prevents_review_mutations(self):
        job, candidate, _ = self.batch(pending=True)
        self.engine.submit(job['id'], candidate)
        self.engine.control('pause')
        result = self.engine.review(job['id'], 'withdraw', 'topics/limits.md:other-value')
        self.assertEqual(result['state'], 'paused')
        self.assertEqual(self.engine.status(job['id'])['pending_count'], 1)

    def test_revalidate_only_selected_when_other_pending_becomes_valid(self):
        job, candidate, complete = self.batch(pending=True)
        third = {**copy.deepcopy(complete['updates'][1]), 'claim_id': 'third-value'}
        source = self.project / 'other.py'
        source.write_text('VALUE = 10\n')
        candidate['updates'] = [copy.deepcopy(complete['updates'][1]), third]
        self.engine.submit(job['id'], candidate)
        self.assertEqual(self.engine.status(job['id'])['pending_count'], 2)
        source.write_text('VALUE = 9\n')
        result = self.engine.review(job['id'], 'revalidate', 'topics/limits.md:other-value')
        self.assertEqual(result['pending_count'], 1)
        self.assertIsNone(self.engine._get('fact', 'topics/limits.md:third-value'))
        self.assertIsNotNone(self.engine._get('fact', 'topics/limits.md:other-value'))

    def test_dismissing_pending_preserves_interrupted_saved_batch_recovery(self):
        job, candidate, _ = self.batch(pending=True)
        with patch('akf.engine.atomic_write', side_effect=OSError('synthetic pre-write interruption')):
            with self.assertRaises(OSError):
                self.engine.submit(job['id'], candidate)
        result = self.engine.review(job['id'], 'withdraw', 'topics/limits.md:other-value')
        self.assertEqual(result['state'], 'applying')
        self.assertEqual(result['pending_count'], 0)
        self.assertFalse(result['complete'])
        result = self.engine.resume(job['id'])
        self.assertEqual(result['state'], 'local_saved')
        self.assertTrue(result['complete'])
        self.assertIsNotNone(self.engine._get('fact', 'topics/limits.md:request-limit'))
        self.assertIsNone(self.engine._get('fact', 'topics/limits.md:other-value'))
