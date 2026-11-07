"""Explicit matter relationships keep progress separate from source behavior."""
import datetime as dt
import subprocess

from akf.engine import digest, now
from akf.retrieval import SearchIndex
from test_engine import Fixture


class StateRelationTests(Fixture):
    def update(self, claim_id, content, *, matter='gemini-live-probe-access-20261104', kind='progress', stage='waiting', supersedes=None, evidence=True):
        job = self.job()
        candidate = self.candidate(job, content)
        update = candidate['updates'][0]
        update.update(claim_id=claim_id, matter_id=matter, state_kind=kind, stage=stage)
        if supersedes is not None:
            update['supersedes'] = supersedes
        if not evidence:
            update.update(content='Unverified: ' + content, evidence=[])
        return job, candidate

    def save(self, claim_id, content, **kwargs):
        job, candidate = self.update(claim_id, content, **kwargs)
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'local_saved', result)
        return job, self.engine._get('fact', 'topics/limits.md:' + claim_id)

    def test_gemini_explicit_next_stage_is_default_and_history_remains(self):
        note = self.knowledge / 'topics/limits.md'
        note.write_text('# Gemini probe\n\nHuman handoff stays.\n')
        _, first = self.save('gemini-waiting', 'Gemini probe waits for credentials; no requests yet.')
        _, second = self.save('gemini-finished', 'Gemini probe completed four successful requests.', stage='completed',
                              supersedes=['topics/limits.md:gemini-waiting'])
        index = SearchIndex(self.engine)
        result = index.search('Gemini probe')
        self.assertEqual([r['claim_id'] for r in result['results'] if r['claim_id']], ['gemini-finished'])
        history = index.search('Gemini probe', history=True)
        old = next(r for r in history['results'] if r['claim_id'] == 'gemini-waiting')
        self.assertEqual(old['lifecycle'], 'superseded')
        self.assertEqual(old['superseded_by'], second['version_id'])
        self.assertIn('Human handoff stays.', note.read_text())
        self.assertIn(first['block'], note.read_text())

    def test_related_verified_states_without_replacement_show_conflict(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        self.save('finished', 'Gemini probe completed four requests.', stage='completed')
        values = SearchIndex(self.engine).search('Gemini probe')['results']
        self.assertEqual(len(values), 2)
        self.assertTrue(all('unresolved_state_relationship' in r['review_reasons'] for r in values))

    def test_pending_stage_never_hides_verified_state(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        job, candidate = self.update('finished', 'Gemini probe completed four requests.', stage='completed',
                                     supersedes=['topics/limits.md:waiting'], evidence=False)
        self.assertEqual(self.engine.submit(job['id'], candidate)['pending_count'], 1)
        values = SearchIndex(self.engine).search('Gemini probe')['results']
        self.assertEqual({r['claim_id'] for r in values}, {'waiting', 'finished'})
        self.assertEqual(next(r for r in values if r['claim_id'] == 'waiting')['lifecycle'], 'current')

    def test_same_matter_distinct_domains_do_not_replace_each_other(self):
        self.save('source', 'Gemini source behavior guards the probe.', kind='behavior')
        job, candidate = self.update('progress', 'Gemini probe completed.', supersedes=['topics/limits.md:source'])
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'conflict')
        self.assertEqual(self.engine.fact_records()[0]['lifecycle'], 'current')

    def test_cross_matter_relation_is_rejected(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        job, candidate = self.update('other', 'Another task finished.', matter='another-task',
                                     supersedes=['topics/limits.md:waiting'])
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'conflict')

    def test_git_commit_updates_progress_with_unchanged_source_bytes(self):
        _, source = self.save('redeem-behavior', 'Redeem generated code accepts mixed case.',
                              matter='redeem-generated-code-mixed-case', kind='behavior')
        self.save('redeem-local', 'Redeem generated code is currently uncommitted.',
                  matter='redeem-generated-code-mixed-case', kind='progress', stage='uncommitted')
        original = (self.project / 'settings.py').read_bytes()
        for command in (['init', '-b', 'main'], ['config', 'user.name', 'Fixture'], ['config', 'user.email', 'fixture@example.test'], ['add', 'settings.py'], ['commit', '-m', 'redeem mixed case']):
            subprocess.run(['git', '-C', str(self.project), *command], capture_output=True, check=True)
        commit = subprocess.check_output(['git', '-C', str(self.project), 'rev-parse', 'HEAD'], text=True).strip()
        job, candidate = self.update('redeem-committed', 'Redeem generated code committed as ' + commit + '.',
                                     matter='redeem-generated-code-mixed-case', kind='progress', stage='committed',
                                     supersedes=['topics/limits.md:redeem-local'])
        candidate['updates'][0]['claim_type'] = 'change'
        candidate['updates'][0]['evidence'] = [{'kind':'commit','commit':commit,'path':'settings.py',
                                               'sha256':digest(original),'excerpt':original.decode().strip()}]
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'local_saved')
        self.assertEqual((self.project / 'settings.py').read_bytes(), original)
        values = SearchIndex(self.engine).search('Redeem generated code')['results']
        self.assertEqual({r['claim_id'] for r in values}, {'redeem-behavior', 'redeem-committed'})
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:redeem-behavior'), source)

    def test_fact_time_is_independent_of_supplement_time(self):
        job, candidate = self.update('historic', 'Gemini probe completed four requests.', stage='completed')
        observed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        candidate['updates'][0]['observed_at'] = observed
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'local_saved')
        fact = self.engine.fact_records()[0]
        self.assertEqual(fact['observed_at'], observed)
        self.assertGreater(fact['last_verified_at'], observed)

    def test_older_fact_time_cannot_replace_newer_related_state(self):
        self.save('finished', 'Gemini probe completed four requests.', stage='completed')
        job, candidate = self.update('late-supplement', 'Gemini probe waits for credentials.',
                                     supersedes=['topics/limits.md:finished'])
        candidate['updates'][0]['observed_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'stale')

    def test_undo_of_relation_restores_prior_default_without_rewriting_old_fact(self):
        _, first = self.save('waiting', 'Gemini probe waits for credentials.')
        job, _ = self.save('finished', 'Gemini probe completed four requests.', stage='completed',
                           supersedes=['topics/limits.md:waiting'])
        self.engine.undo(job['id'])
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:waiting'), first)
        self.assertEqual(SearchIndex(self.engine).search('Gemini probe')['results'][0]['claim_id'], 'waiting')

    def test_missing_relation_target_remains_pending(self):
        job, candidate = self.update('finished', 'Gemini probe completed.',
                                     supersedes=['topics/limits.md:missing'])
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['pending_count'], 1)
        self.assertIn('Related fact is unavailable', result['pending_updates'][0]['error'])

    def test_same_content_metadata_change_is_retained(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        job, candidate = self.update('waiting', 'Gemini probe waits for credentials.', stage='blocked')
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'local_saved')
        self.assertEqual(self.engine.fact_records()[0]['stage'], 'blocked')

    def test_repeated_pending_identity_keeps_latest_stage_with_history_pointer(self):
        first, old = self.update('gemini-live-probe-access-20261104', 'Gemini probe waits for credentials.', evidence=False)
        self.engine.submit(first['id'], old)
        second, new = self.update('gemini-live-probe-access-20261104', 'Gemini probe completed four requests.', stage='completed', evidence=False)
        self.engine.submit(second['id'], new)
        index = SearchIndex(self.engine)
        values = [r for r in index.search('Gemini probe')['results'] if r['source_origin'] == 'pending']
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['job_id'], second['id'])
        self.assertEqual(values[0]['related_pending'][0]['job'], first['id'])
        history = [r for r in index.search('Gemini probe', history=True)['results'] if r['source_origin'] == 'pending']
        self.assertEqual(len(history), 2)
        self.assertEqual(self.engine.status(first['id'])['pending_count'], 1)

    def test_source_release_installation_domains_remain_independent(self):
        for claim, kind, content in [('source','source','AKF source version is 0.3.0.'),
                                     ('release','release','AKF release report records public 0.3.0.'),
                                     ('install','installation','AKF installation manifest records runtime 0.3.0.')]:
            self.save(claim, content, matter='akf-version', kind=kind, stage='0.3.0')
        values = [r for r in SearchIndex(self.engine).search('AKF')['results'] if r['source_origin']=='verified_fact']
        self.assertEqual({r['state_kind'] for r in values}, {'source','release','installation'})
        self.assertTrue(all(r['review_status'] == 'clear' for r in values))

    def test_commit_domain_requires_fixed_commit_evidence(self):
        job, candidate = self.update('committed', 'Gemini probe commit recorded.', kind='commit')
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['pending_count'], 1)
        self.assertIn('change evidence', result['pending_updates'][0]['error'])

    def test_superseded_status_is_visible_in_direct_fact_readback(self):
        _, old = self.save('waiting', 'Gemini probe waits for credentials.')
        self.save('finished', 'Gemini probe completed four requests.', stage='completed', supersedes=['topics/limits.md:waiting'])
        self.assertEqual(self.engine.fact_status(old)['lifecycle'], 'superseded')

    def test_invalid_relationship_shape_returns_candidate_error(self):
        for value in ([{}], ['topics/limits.md:old', 'topics/limits.md:old']):
            job, candidate = self.update('invalid', 'Gemini probe invalid relation.', supersedes=value)
            result = self.engine.submit(job['id'], candidate)
            self.assertEqual(result['state'], 'rejected')

    def test_legacy_caller_recheck_retains_relationship_metadata(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        self.save('finished', 'Gemini probe completed four requests.', stage='completed', supersedes=['topics/limits.md:waiting'])
        job = self.job()
        candidate = self.candidate(job, 'Gemini probe completed four requests.')
        candidate['updates'][0]['claim_id'] = 'finished'
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'local_saved')
        fact = self.engine._get('fact', 'topics/limits.md:finished')
        self.assertEqual(fact['stage'], 'completed')
        self.assertEqual(fact['supersedes'], ['topics/limits.md:waiting'])
        self.assertEqual(next(f for f in self.engine.fact_records() if f['claim_id']=='waiting')['lifecycle'], 'superseded')

    def test_partial_legacy_resubmission_keeps_original_resolved_identity(self):
        self.save('request-limit', 'The request limit is eight.')
        job = self.job()
        candidate = self.candidate(job)
        candidate['updates'].append({**candidate['updates'][0], 'claim_id':'pending-test',
                                     'claim_type':'test_result', 'content':'Unverified: Focused tests passed.', 'evidence':[]})
        first = self.engine.submit(job['id'], candidate)
        self.assertEqual(first['pending_count'], 1)
        saved = self.engine._get('fact', 'topics/limits.md:request-limit')
        second = self.engine.submit(job['id'], candidate)
        self.assertEqual(second['state'], 'local_saved', second)
        self.assertIsNone(second['error'])
        self.assertEqual(second['pending_count'], 1)
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit'), saved)

    def test_reopened_claim_is_not_hidden_by_old_replacement_edge(self):
        _, waiting = self.save('waiting', 'Gemini probe waits for credentials.')
        self.save('finished', 'Gemini probe completed four requests.', stage='completed', supersedes=['topics/limits.md:waiting'])
        _, reopened = self.save('waiting', 'Gemini probe reopened; now waits for a new credential.', stage='reopened')
        facts = {f['claim_id']:f for f in self.engine.fact_records()}
        self.assertEqual(facts['waiting']['lifecycle'], 'current')
        self.assertIn('superseded_target_changed:topics/limits.md:waiting', facts['finished']['review_reasons'])
        results = SearchIndex(self.engine).search('Gemini probe')['results']
        self.assertIn('waiting', [r['claim_id'] for r in results])
        self.assertEqual(reopened['history'][0]['version_id'], waiting['version_id'])

    def test_replacement_binds_target_version_and_fact_time(self):
        _, waiting = self.save('waiting', 'Gemini probe waits for credentials.')
        _, finished = self.save('finished', 'Gemini probe completed four requests.', stage='completed', supersedes=['topics/limits.md:waiting'])
        self.assertEqual(finished['supersedes_versions']['topics/limits.md:waiting'],
                         {'version_id':waiting['version_id'], 'observed_at':waiting['observed_at']})
        self.save('waiting', 'Gemini probe waits for credentials.', stage='reopened')
        self.assertEqual(next(f for f in self.engine.fact_records() if f['claim_id']=='waiting')['lifecycle'], 'current')

    def test_legacy_relation_uses_fact_time_and_reviews_changed_target(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        _, finished = self.save('finished', 'Gemini probe completed four requests.', stage='completed', supersedes=['topics/limits.md:waiting'])
        finished.pop('supersedes_versions', None)
        self.engine._put('fact','topics/limits.md:finished',finished)
        self.assertEqual(next(f for f in self.engine.fact_records() if f['claim_id']=='waiting')['lifecycle'], 'superseded')
        self.save('waiting', 'Gemini probe reopened for a new request.', stage='reopened')
        facts = {f['claim_id']:f for f in self.engine.fact_records()}
        self.assertEqual(facts['waiting']['lifecycle'], 'current')
        self.assertEqual(facts['finished']['review_status'], 'review_required')

    def test_reopened_claim_can_explicitly_replace_completed_observation(self):
        self.save('waiting', 'Gemini probe waits for credentials.')
        self.save('finished', 'Gemini probe completed four requests.', stage='completed', supersedes=['topics/limits.md:waiting'])
        self.save('waiting', 'Gemini probe reopened for a new request.', stage='reopened', supersedes=['topics/limits.md:finished'])
        values = [r for r in SearchIndex(self.engine).search('Gemini probe')['results'] if r['claim_id']]
        self.assertEqual([r['claim_id'] for r in values], ['waiting'])

    def test_pending_stage_order_uses_actual_time_across_timezones(self):
        first, newer = self.update('probe', 'Gemini later actual progress.', evidence=False)
        newer['updates'][0]['observed_at'] = '2026-11-06T23:00:00+00:00'
        self.engine.submit(first['id'], newer)
        second, older = self.update('probe', 'Gemini earlier actual progress.', evidence=False)
        older['updates'][0]['observed_at'] = '2026-11-07T06:00:00+08:00'
        self.engine.submit(second['id'], older)
        result = SearchIndex(self.engine).search('Gemini')['results']
        self.assertEqual([r['job_id'] for r in result], [first['id']])
        self.assertEqual(result[0]['related_pending'][0]['job'], second['id'])

    def test_search_recent_state_tie_uses_actual_time_across_timezones(self):
        first, older = self.update('older', 'Gemini probe source observation.', kind='source')
        older['updates'][0]['observed_at'] = '2026-11-07T06:00:00+08:00'
        self.engine.submit(first['id'], older)
        second, newer = self.update('newer', 'Gemini probe source observation.', kind='source')
        newer['updates'][0]['observed_at'] = '2026-11-06T23:00:00+00:00'
        self.engine.submit(second['id'], newer)
        values = SearchIndex(self.engine).search('Gemini probe')['results']
        self.assertEqual([r['claim_id'] for r in values], ['newer','older'])
