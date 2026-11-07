"""Review links retained candidates to verified history without rewriting either task."""
import copy

from akf.engine import WorkflowError
from akf.review import review
from test_engine import Fixture


class HistoryReviewTests(Fixture):
    def saved_and_pending(self):
        first = self.job()
        candidate = self.candidate(first)
        self.engine.submit(first['id'], candidate)
        fact = self.engine._get('fact', 'topics/limits.md:request-limit')
        second = self.job()
        pending = self.candidate(second, 'Unverified: ' + fact['content'])
        pending['updates'][0]['evidence'] = []
        self.engine.submit(second['id'], pending)
        return first, second, fact

    def test_link_history_keeps_original_task_and_saved_fact(self):
        first, pending, fact = self.saved_and_pending()
        note = self.knowledge / 'topics/limits.md'
        before = note.read_bytes()
        result = review(self.engine, pending['id'], 'link-history', 'topics/limits.md:request-limit',
                        linked_fact='topics/limits.md:request-limit', reason='Matching verified saved observation.')
        self.assertEqual(result['pending_count'], 0)
        self.assertEqual(result['items'][0]['resolution'], 'linked_history')
        self.assertEqual(result['items'][0]['linked_fact']['version_id'], fact['version_id'])
        self.assertEqual(result['review_history'][-1]['reason'], 'Matching verified saved observation.')
        self.assertEqual(self.engine._get('job', pending['id'])['turn_id'], pending['turn_id'])
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit'), fact)
        self.assertEqual(note.read_bytes(), before)
        self.engine.resume(pending['id'])
        self.assertEqual(note.read_bytes(), before)

    def test_mismatched_or_unverified_history_keeps_pending(self):
        first, pending, fact = self.saved_and_pending()
        altered = copy.deepcopy(fact)
        altered['content'] = 'Different fact.'
        self.engine._put('fact', 'topics/limits.md:request-limit', altered)
        with self.assertRaisesRegex(WorkflowError, 'content'):
            review(self.engine, pending['id'], 'link-history', 'topics/limits.md:request-limit',
                   linked_fact='topics/limits.md:request-limit', reason='Review selected history.')
        altered.update(content=fact['content'], evidence_status='unknown')
        self.engine._put('fact', 'topics/limits.md:request-limit', altered)
        with self.assertRaisesRegex(WorkflowError, 'verified'):
            review(self.engine, pending['id'], 'link-history', 'topics/limits.md:request-limit',
                   linked_fact='topics/limits.md:request-limit', reason='Review selected history.')
        self.assertEqual(self.engine.status(pending['id'])['pending_count'], 1)

    def test_review_action_keeps_specific_gap_reason(self):
        first, pending, fact = self.saved_and_pending()
        result = review(self.engine, pending['id'], 'withdraw', 'topics/limits.md:request-limit',
                        reason='Superseded historical task; no deployment rerun.')
        self.assertEqual(result['review_history'][-1]['reason'], 'Superseded historical task; no deployment rerun.')
        self.assertEqual(result['items'][0]['reason'], 'Superseded historical task; no deployment rerun.')

    def test_explicit_reanchor_preserves_human_edits_and_original_digest(self):
        first, pending, fact = self.saved_and_pending()
        from akf.engine import digest
        note = self.knowledge / 'topics/limits.md'
        note.write_text(note.read_text() + '\nNew human paragraph.\n')
        original = self.engine.explain(pending['id'])['items'][0]['candidate']['expected_sha256']
        result = review(self.engine, pending['id'], 'reanchor', 'topics/limits.md:request-limit',
                        expected_sha256=digest(note.read_bytes()), reason='Reviewed latest note and preserved its human paragraph.')
        self.assertEqual(result['items'][0]['candidate']['expected_sha256'], original)
        update = copy.deepcopy(result['items'][0]['candidate'])
        update['content'] = fact['content']
        update['evidence'] = self.candidate(pending)['updates'][0]['evidence']
        result = review(self.engine, pending['id'], 'revalidate', 'topics/limits.md:request-limit', update)
        self.assertEqual(result['pending_count'], 0)
        self.assertIn('New human paragraph.', note.read_text())
        history = self.engine._get('job', pending['id'])['review_history']
        self.assertEqual(history[-2]['action'], 'reanchor')
        self.assertEqual(history[-2]['old_anchor_sha256'], original)

    def test_reanchor_rejects_unread_digest_and_edited_owned_block(self):
        first, pending, fact = self.saved_and_pending()
        from akf.engine import digest
        note = self.knowledge / 'topics/limits.md'
        with self.assertRaisesRegex(WorkflowError, 'digest'):
            review(self.engine, pending['id'], 'reanchor', 'topics/limits.md:request-limit',
                   expected_sha256='0' * 64, reason='Stale read.')
        note.write_text(note.read_text().replace(fact['content'], 'Human changed owned content.'))
        with self.assertRaisesRegex(WorkflowError, 'Owned block'):
            review(self.engine, pending['id'], 'reanchor', 'topics/limits.md:request-limit',
                   expected_sha256=digest(note.read_bytes()), reason='Reviewed target.')
        self.assertEqual(self.engine.status(pending['id'])['pending_count'], 1)

    def test_split_saves_verified_subset_and_keeps_unsupported_original_pending(self):
        first, pending, fact = self.saved_and_pending()
        residual = copy.deepcopy(self.engine.explain(pending['id'])['items'][0]['candidate'])
        residual['content'] = 'Unverified: Browser and build results still lack their own receipts.'
        verified = self.candidate(pending)['updates'][0]
        verified.update(claim_id='verified-subset', content='The checked source limit is eight.')
        original = self.engine._get('job', pending['id'])['pending_updates'][0]['update']
        result = review(self.engine, pending['id'], 'split', 'topics/limits.md:request-limit',
                        {'status': 'partial', 'updates': [verified, residual]}, reason='Only the source subset has matching evidence.')
        self.assertEqual(result['pending_count'], 1)
        self.assertEqual(result['items'][0]['candidate']['content'], residual['content'])
        self.assertIsNotNone(self.engine._get('fact', 'topics/limits.md:verified-subset'))
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit'), fact)
        job = self.engine._get('job', pending['id'])
        self.assertEqual(job['review_history'][-1]['candidate_before'], original)
        self.assertEqual(job['session_id'], pending['session_id'])
        self.assertEqual(job['turn_id'], pending['turn_id'])
        before = (self.knowledge / 'topics/limits.md').read_bytes()
        again = review(self.engine, pending['id'], 'split', 'topics/limits.md:request-limit',
                       {'status': 'partial', 'updates': [verified, residual]}, reason='Only the source subset has matching evidence.')
        self.assertEqual(again['pending_count'], 1)
        self.assertEqual((self.knowledge / 'topics/limits.md').read_bytes(), before)

    def test_split_rejects_unsafe_subset_before_persisting_it(self):
        first, pending, fact = self.saved_and_pending()
        residual = copy.deepcopy(self.engine.explain(pending['id'])['items'][0]['candidate'])
        child = self.candidate(pending)['updates'][0]
        child.update(claim_id='unsafe-subset', content='api_key = synthetic-private-split-value')
        result = review(self.engine, pending['id'], 'split', 'topics/limits.md:request-limit',
                        {'status': 'partial', 'updates': [child, residual]}, reason='Check boundary.')
        self.assertEqual(result['pending_count'], 1)
        rows = self.engine.db.execute('SELECT value FROM state').fetchall()
        self.assertFalse(any('synthetic-private-split-value' in row[0] for row in rows))

    def test_interrupted_split_keeps_original_candidate_and_can_resume(self):
        from unittest.mock import patch
        first, pending, fact = self.saved_and_pending()
        residual = copy.deepcopy(self.engine.explain(pending['id'])['items'][0]['candidate'])
        original = copy.deepcopy(residual)
        residual['content'] = 'Unverified: Other test scopes still need receipts.'
        child = self.candidate(pending)['updates'][0]
        child.update(claim_id='interrupted-subset', content='Verified source subset.')
        payload = {'status': 'partial', 'updates': [child, residual]}
        with patch('akf.engine.atomic_write', side_effect=OSError('synthetic interrupted write')):
            with self.assertRaises(OSError):
                review(self.engine, pending['id'], 'split', 'topics/limits.md:request-limit', payload, reason='Split exact scope.')
        job = self.engine._get('job', pending['id'])
        self.assertEqual(job['review_history'][-1]['candidate_before'], original)
        self.engine.resume(pending['id'])
        result = review(self.engine, pending['id'], 'split', 'topics/limits.md:request-limit', payload, reason='Split exact scope.')
        self.assertEqual(result['pending_count'], 1)
        history = self.engine._get('job', pending['id'])['review_history']
        self.assertEqual(history[-1]['child_item'], 'topics/limits.md:interrupted-subset')

    def test_split_does_not_revalidate_unselected_pending_sibling(self):
        import test_multifact
        job, candidate, complete = test_multifact.MultiFactTests.batch(self, pending=True)
        candidate['updates'][0]['content'] = 'Unverified: Aggregate awaiting source evidence.'
        candidate['updates'][0]['evidence'] = []
        self.engine.submit(job['id'], candidate)
        residual = copy.deepcopy(candidate['updates'][0])
        child = copy.deepcopy(complete['updates'][0])
        child.update(claim_id='selected-subset', content='Verified selected source.')
        (self.project / 'other.py').write_text('VALUE = 9\n')
        result = review(self.engine, job['id'], 'split', 'topics/limits.md:request-limit',
                        {'status': 'partial', 'updates': [child, residual]}, reason='Selected aggregate only.')
        self.assertEqual(result['pending_count'], 2)
        self.assertIsNone(self.engine._get('fact', 'topics/limits.md:other-value'))

    def test_interrupted_split_retry_keeps_unselected_sibling_pending(self):
        import test_multifact
        from unittest.mock import patch
        job, candidate, complete = test_multifact.MultiFactTests.batch(self, pending=True)
        candidate['updates'][0].update(content='Unverified: Aggregate waiting.', evidence=[])
        self.engine.submit(job['id'], candidate)
        residual = copy.deepcopy(candidate['updates'][0])
        child = copy.deepcopy(complete['updates'][0])
        child.update(claim_id='retry-subset', content='Verified selected source.')
        (self.project / 'other.py').write_text('VALUE = 9\n')
        payload = {'status': 'partial', 'updates': [child, residual]}
        with patch('akf.engine.atomic_write', side_effect=OSError('synthetic interrupted write')):
            with self.assertRaises(OSError):
                review(self.engine, job['id'], 'split', 'topics/limits.md:request-limit', payload, reason='Only selected aggregate.')
        result = review(self.engine, job['id'], 'split', 'topics/limits.md:request-limit', payload, reason='Only selected aggregate.')
        self.assertEqual(result['pending_count'], 2)
        self.assertIsNone(self.engine._get('fact', 'topics/limits.md:other-value'))
