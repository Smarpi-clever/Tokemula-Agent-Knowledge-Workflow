import copy
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from akf.engine import Engine, digest, now
from test_engine import Fixture


class FactStateTests(Fixture):
    def save(self, value=8, content='The request limit is eight.'):
        job = self.job(value)
        candidate = self.candidate(job, content)
        self.assertEqual(self.engine.submit(job['id'], candidate)['state'], 'local_saved')
        return self.engine._get('fact', 'topics/limits.md:request-limit')

    def test_content_versions_preserve_evidence_and_supersession(self):
        first = self.save()
        second = self.save(12, 'The request limit is twelve.')
        self.assertEqual(second['history'][0]['version_id'], first['version_id'])
        self.assertEqual(second['history'][0]['superseded_by'], second['version_id'])
        self.assertEqual(second['history'][0]['evidence'], first['evidence'])
        current, historical = self.engine.fact_records(True)
        self.assertEqual(current['lifecycle'], 'current')
        self.assertEqual(historical['lifecycle'], 'superseded')
        self.assertEqual(historical['review_status'], 'clear')
        self.assertIn('[superseded;', second['block'])

    def test_same_content_reverification_refreshes_without_content_history(self):
        first = self.save()
        second = self.save()
        self.assertEqual(first['version_id'], second['version_id'])
        self.assertEqual(second['history'], [])
        self.assertEqual(second['verification_count'], 2)
        self.assertGreater(second['last_verified_at'], first['last_verified_at'])
        self.assertIn('Last verified: ' + second['last_verified_at'], second['block'])

    def test_current_file_changes_and_explicit_deadline_require_review(self):
        first = self.save()
        self.assertEqual(self.engine.fact_status(first)['review_status'], 'clear')
        (self.project / 'settings.py').write_text('LIMIT = 33\n')
        status = self.engine.fact_status(first)
        self.assertEqual(status['review_status'], 'review_required')
        self.assertIn('file_changed:settings.py', status['review_reasons'])
        job = self.job()
        candidate = self.candidate(job)
        candidate['updates'][0]['review_after'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        self.engine.submit(job['id'], candidate)
        status = self.engine.fact_status(self.engine._all('fact')[0])
        self.assertEqual(status['review_reasons'], ['review_after'])

    def test_fixed_commit_fact_retains_historical_meaning(self):
        for command in (['init', '-b', 'main'], ['config', 'user.name', 'Fixture'], ['config', 'user.email', 'fixture@example.test'], ['add', 'settings.py'], ['commit', '-m', 'synthetic source']):
            subprocess.run(['git', '-C', str(self.project), *command], capture_output=True, check=True)
        commit = subprocess.check_output(['git', '-C', str(self.project), 'rev-parse', 'HEAD'], text=True).strip()
        data = (self.project / 'settings.py').read_bytes()
        job = self.job()
        candidate = self.candidate(job, 'At the fixed commit, request limit is four.')
        candidate['updates'][0]['evidence'] = [{'kind': 'commit', 'commit': commit, 'path': 'settings.py', 'sha256': digest(data), 'excerpt': 'LIMIT = 4'}]
        self.engine.submit(job['id'], candidate)
        (self.project / 'settings.py').write_text('LIMIT = 22\n')
        self.assertEqual(self.engine.fact_records()[0]['review_status'], 'clear')

    def test_old_fields_remain_unknown_and_are_additively_upgraded(self):
        fact = self.save()
        old = {k: v for k, v in fact.items() if k in {'topic', 'claim_id', 'title', 'content', 'observed_at', 'block', 'history', 'evidence'}}
        self.engine._put('fact', 'topics/limits.md:request-limit', old)
        result = self.engine.fact_records()[0]
        self.assertEqual(result['evidence_status'], 'unknown')
        self.assertIsNone(result['last_verified_at'])
        self.assertEqual(result['environment'], 'synthetic')
        self.assertEqual(result['version_id'], 'unknown')
        refreshed = self.save()
        self.assertEqual(refreshed['evidence_status'], 'tool_verified')
        self.assertEqual(refreshed['history'], [])

    def test_environment_and_project_identity_never_cross_replace(self):
        first = self.save()
        for change in ({'environment': 'production'}, {'project_root': '/synthetic/other-project'}):
            self.engine._put('fact', 'topics/limits.md:request-limit', {**first, **change})
            job = self.job(12)
            self.assertEqual(self.engine.submit(job['id'], self.candidate(job, 'The limit is twelve.'))['state'], 'conflict')
            self.assertIn('The request limit is eight.', (self.knowledge / 'topics/limits.md').read_text())
        self.engine._put('fact', 'topics/limits.md:request-limit', first)

    def test_unrelated_fact_identity_is_independent(self):
        first = self.save()
        job = self.job(12)
        candidate = self.candidate(job, 'A separate setting has value twelve.')
        candidate['updates'][0]['claim_id'] = 'another-setting'
        self.engine.submit(job['id'], candidate)
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit'), first)
        self.assertEqual(len(self.engine.fact_records()), 2)

    def test_completion_target_tracks_authorized_outputs(self):
        job = self.job()
        self.engine.submit(job['id'], self.candidate(job))
        stored = self.engine._get('job', job['id'])
        self.assertTrue(self.engine.job_complete(stored))
        self.engine.config['git'] = {'auto_commit': True}
        self.assertFalse(self.engine.job_complete(stored))
        stored['write_state'] = 'committed'
        self.assertTrue(self.engine.job_complete(stored))
        self.engine.config['git']['auto_push'] = True
        self.assertFalse(self.engine.job_complete(stored))
        stored['write_state'] = 'remote_confirmed'
        self.assertTrue(self.engine.job_complete(stored))
