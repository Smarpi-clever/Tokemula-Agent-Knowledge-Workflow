import copy
import json
from unittest.mock import patch

from akf.engine import Engine, WorkflowError, atomic_write, digest
from test_engine import Fixture


class MultiFactTests(Fixture):
    def batch(self, pending=False):
        job = self.job()
        candidate = self.candidate(job)
        source = self.project / 'other.py'
        source.write_text('VALUE = 9\n')
        second = {**copy.deepcopy(candidate['updates'][0]), 'claim_id': 'other-value',
                  'content': 'The other value is nine.', 'evidence': [{
                      'kind': 'file', 'path': 'other.py', 'sha256': digest(source.read_bytes()),
                      'excerpt': 'VALUE = 9'}]}
        candidate['updates'].append(second)
        complete = copy.deepcopy(candidate)
        if pending:
            second['evidence'] = []
        return job, candidate, complete

    def test_grouped_facts_use_one_write_and_register_both(self):
        note = self.knowledge / 'topics/limits.md'
        note.write_text('# Limits\n\nHuman introduction.\n')
        job, candidate, _ = self.batch()
        with patch('akf.engine.atomic_write', wraps=atomic_write) as write:
            result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'local_saved')
        self.assertEqual(write.call_count, 1)
        self.assertIn('Human introduction.', note.read_text())
        self.assertEqual(note.read_text().count('<!-- akf:'), 2)
        self.assertEqual(len(self.engine._all('fact')), 2)
        plans = self.engine._get('job', job['id'])['plans']
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0]['changes']), 2)
        with patch('akf.engine.atomic_write', wraps=atomic_write) as write:
            self.engine.submit(job['id'], candidate)
            self.engine.resume(job['id'])
        self.assertEqual(write.call_count, 0)

    def test_missing_test_evidence_does_not_block_source_fact(self):
        job, candidate, _ = self.batch()
        test = candidate['updates'][1]
        test.update(claim_type='test_result', content='Unverified: The focused test passed.', evidence=[])
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'local_saved')
        self.assertEqual(result['pending_count'], 1)
        note = self.knowledge / 'topics/limits.md'
        before = note.read_bytes()
        self.assertIn(b'The request limit is eight.', before)
        self.assertNotIn(b'The focused test passed.', before)
        saved = self.engine.status(job['id'])
        self.assertEqual(saved['pending_updates'][0]['update'], test)
        self.assertEqual(self.engine.context()['pending_jobs'][0]['pending_count'], 1)
        self.assertEqual(self.engine.status()['jobs'][0]['pending_count'], 1)
        self.engine.submit(job['id'], candidate)
        self.engine.resume(job['id'])
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(self.engine._get('job', job['id']).get('write_history', []), [])

    def test_security_and_group_cas_errors_never_partially_write(self):
        for mutation in ('secret', 'cas'):
            with self.subTest(mutation=mutation):
                job, candidate, _ = self.batch()
                second = candidate['updates'][1]
                if mutation == 'secret':
                    second['content'] = 'api_key = synthetic-forbidden-value'
                else:
                    second['expected_sha256'] = 'a' * 64
                result = self.engine.submit(job['id'], candidate)
                self.assertIn(result['state'], ('rejected', 'candidate_pending', 'conflict'))
                self.assertEqual(list((self.knowledge / 'topics').iterdir()), [])
        self.assertNotIn(b'synthetic-forbidden-value', (self.root / 'state/state.sqlite3').read_bytes())

    def test_pending_fact_owned_block_conflict_blocks_whole_write(self):
        first, candidate, _ = self.batch()
        self.engine.submit(first['id'], candidate)
        note = self.knowledge / 'topics/limits.md'
        note.write_text(note.read_text().replace('The other value is nine.', 'Human changed this fact.'))
        second = self.job(12)
        candidate = self.candidate(second, 'The request limit is twelve.')
        candidate['updates'].append({**copy.deepcopy(candidate['updates'][0]),
                                     'claim_id': 'other-value', 'content': 'The other value is nine.', 'evidence': []})
        before = note.read_bytes()
        self.assertEqual(self.engine.submit(second['id'], candidate)['state'], 'conflict')
        self.assertEqual(note.read_bytes(), before)

    def test_recovery_after_replace_restores_all_fact_rows_once(self):
        job, candidate, _ = self.batch()
        def interrupt(path, data, mode):
            atomic_write(path, data, mode)
            raise OSError('synthetic crash after grouped replacement')
        with patch('akf.engine.atomic_write', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.engine.submit(job['id'], candidate)
        (self.project / 'other.py').write_text('VALUE = 99\n')
        reopened = Engine(self.config)
        self.addCleanup(reopened.db.close)
        with patch('akf.engine.atomic_write', wraps=atomic_write) as write:
            self.assertEqual(reopened.resume(job['id'])['state'], 'local_saved')
        self.assertEqual(write.call_count, 0)
        self.assertEqual(len(reopened._all('fact')), 2)
        self.assertEqual((self.knowledge / 'topics/limits.md').read_text().count('<!-- akf:'), 2)

    def test_unwritten_group_rechecks_each_source(self):
        job, candidate, _ = self.batch()
        with patch('akf.engine.atomic_write', side_effect=OSError('synthetic pre-write crash')):
            with self.assertRaises(OSError):
                self.engine.submit(job['id'], candidate)
        (self.project / 'other.py').write_text('VALUE = 99\n')
        self.assertEqual(self.engine.resume(job['id'])['state'], 'pending_evidence')
        self.assertFalse((self.knowledge / 'topics/limits.md').exists())
        self.assertEqual(self.engine._all('fact'), [])

    def test_pending_resolution_uses_owned_cas_anchor_and_new_write_id(self):
        job, candidate, complete = self.batch(pending=True)
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'local_saved')
        first = self.engine._get('job', job['id'])
        first_block = self.engine._get('fact', 'topics/limits.md:request-limit')['block']
        self.assertIsNone(first['pending_updates'][0]['update']['expected_sha256'])
        self.assertEqual(self.engine.submit(job['id'], {'status': 'completed', 'updates': [complete['updates'][1]]})['state'], 'local_saved')
        second = self.engine._get('job', job['id'])
        self.assertNotEqual(second['write_id'], first['write_id'])
        self.assertEqual(len(second['write_history']), 1)
        self.assertEqual(second['write_history'][0]['plans'], first['plans'])
        self.assertEqual(second['pending_updates'], [])
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit')['block'], first_block)
        before = (self.knowledge / 'topics/limits.md').read_bytes()
        self.engine.resume(job['id'])
        self.assertEqual((self.knowledge / 'topics/limits.md').read_bytes(), before)

    def test_pending_resolution_refuses_manual_cas_refresh(self):
        job, candidate, complete = self.batch(pending=True)
        self.engine.submit(job['id'], candidate)
        note = self.knowledge / 'topics/limits.md'
        note.write_text(note.read_text() + '\nHuman paragraph after partial save.\n')
        before = note.read_bytes()
        for expected in (None, digest(before)):
            update = copy.deepcopy(complete['updates'][1])
            update['expected_sha256'] = expected
            self.assertEqual(self.engine.submit(job['id'], {'status': 'completed', 'updates': [update]})['state'], 'conflict')
            self.assertEqual(note.read_bytes(), before)
            self.assertEqual(self.engine.status(job['id'])['pending_count'], 1)

    def test_cross_batch_undo_preserves_human_text_and_withdraws_pending(self):
        seed = self.job(4)
        self.engine.submit(seed['id'], self.candidate(seed, 'The request limit is four.'))
        job, candidate, complete = self.batch(pending=True)
        third = {**copy.deepcopy(candidate['updates'][1]), 'claim_id': 'future-fact', 'content': 'Unverified: future behavior.'}
        candidate['updates'].append(third)
        self.engine.submit(job['id'], candidate)
        self.engine.submit(job['id'], {'status': 'completed', 'updates': [complete['updates'][1]]})
        note = self.knowledge / 'topics/limits.md'
        note.write_text(note.read_text() + '\nHuman paragraph stays.\n')
        self.assertEqual(self.engine.undo(job['id'])['state'], 'local_saved')
        after = note.read_bytes()
        self.assertIn(b'The request limit is four.', after)
        self.assertIn(b'Human paragraph stays.', after)
        self.assertNotIn(b'The other value is nine.', after)
        self.assertIsNone(self.engine._get('fact', 'topics/limits.md:other-value'))
        saved = self.engine.status(job['id'])
        self.assertEqual(saved['pending_count'], 0)
        self.assertTrue(saved['pending_updates'][0]['withdrawn'])
        self.engine.resume(job['id'])
        self.engine.undo(job['id'])
        self.assertEqual(note.read_bytes(), after)

    def test_legacy_flat_plan_recovers_and_undoes_without_migration(self):
        job = self.job()
        candidate = self.candidate(job)
        with patch('akf.engine.atomic_write', side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):
                self.engine.submit(job['id'], candidate)
        stored = self.engine._get('job', job['id'])
        plan = stored['plans'][0]
        change = plan.get('changes', [plan])[0]
        stored['plans'] = [{**{k: v for k, v in plan.items() if k != 'changes'},
                            **{k: v for k, v in change.items() if k != 'source_update'}}]
        for key in ('write_id', 'write_history', 'write_state'):
            stored.pop(key, None)
        self.engine._put('job', job['id'], stored)
        reopened = Engine(self.config)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.resume(job['id'])['state'], 'local_saved')
        note = self.knowledge / 'topics/limits.md'
        note.write_text(note.read_text() + '\nHuman legacy paragraph.\n')
        self.assertEqual(reopened.undo(job['id'])['state'], 'local_saved')
        self.assertIn('Human legacy paragraph.', note.read_text())
        self.assertNotIn('The request limit is eight.', note.read_text())
