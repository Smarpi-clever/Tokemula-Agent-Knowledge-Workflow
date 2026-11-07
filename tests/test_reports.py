"""Exact private reports use explicit selection; source reports and deployment stay separate."""
import copy
from pathlib import Path

from akf.engine import WorkflowError, digest
from akf.reports import select_report, report_evidence
from test_engine import Fixture


class ReportTests(Fixture):
    def setUp(self):
        super().setUp()
        self.relative = '.local/production/releases/synthetic/report.json'
        self.report = self.project / self.relative
        self.report.parent.mkdir(parents=True)
        self.report.write_text('{"release":"0.3.1","status":"published"}\n')

    def evidence(self):
        selected = select_report(self.engine, self.relative)
        return {'kind': 'report', 'selection_id': selected['selection_id'], 'excerpt': '"status":"published"'}

    def test_report_requires_explicit_selection_and_unchanged_content(self):
        with self.assertRaisesRegex(WorkflowError, 'Explicitly select'):
            report_evidence(self.engine, {'selection_id': 'a' * 24, 'excerpt': 'published'})
        evidence = self.evidence()
        verified = report_evidence(self.engine, evidence)
        self.assertEqual(verified['scope'], 'report_content')
        self.assertEqual(verified['path'], self.relative)
        self.report.write_text('{"status":"draft"}\n')
        with self.assertRaisesRegex(WorkflowError, 'changed'):
            report_evidence(self.engine, evidence)

    def test_hidden_discovery_stays_blocked_selected_report_can_be_saved(self):
        with self.assertRaisesRegex(WorkflowError, 'Hidden'):
            self.engine._path(self.project, self.relative)
        job = self.job()
        candidate = self.candidate(job, '发布报告记录 status=published。', 'report_fact')
        candidate['updates'][0]['evidence'] = [self.evidence()]
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'local_saved', result)
        fact = self.engine._get('fact', 'topics/limits.md:request-limit')
        self.assertEqual(fact['evidence'][0]['kind'], 'report')
        self.assertIn(self.relative, (self.knowledge / 'topics/limits.md').read_text())

    def test_report_claim_does_not_promote_deployment(self):
        job = self.job()
        candidate = self.candidate(job, 'Report records a deployment.', 'deployment')
        candidate['updates'][0]['evidence'] = [self.evidence()]
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'pending_evidence', result)
        self.assertIsNone(self.engine._get('fact', 'topics/limits.md:request-limit'))

    def test_exact_scope_and_sensitive_data_guards(self):
        for relative in ('.local/production/releases', 'report.json',
                         '.local/production/releases/../secret.json',
                         '.local/production/releases/a/.env',
                         '.local/production/releases/a//report.json',
                         str(self.report), '.local/production/releases/a/report.sqlite3'):
            with self.subTest(relative=relative), self.assertRaises(WorkflowError):
                select_report(self.engine, relative)
        self.report.write_text('api_key = sk-proj-SYNTHETIC0000000000000\n')
        with self.assertRaisesRegex(WorkflowError, 'Secret'):
            select_report(self.engine, self.relative)
        self.assertEqual(self.engine._all('report_selection'), [])

    def test_exact_selection_accepts_existing_akf_release_report(self):
        relative = '.local/v0.3.0/release/download-verification.json'
        report = self.project / relative
        report.parent.mkdir(parents=True)
        report.write_text('{"source":"synthetic-public-readback","version":"0.3.0"}\n')
        selected = select_report(self.engine, relative)
        self.assertEqual(selected['path'], relative)
        with self.assertRaisesRegex(WorkflowError, 'Hidden'):
            self.engine._path(self.project, relative)

    def test_symlink_and_knowledge_sources_stay_rejected(self):
        other = self.root / 'other.json'
        other.write_text('{"release":"synthetic"}')
        link = self.report.parent / 'link.json'
        link.symlink_to(other)
        with self.assertRaisesRegex(WorkflowError, 'Symlink'):
            select_report(self.engine, link.relative_to(self.project).as_posix())

    def test_reselection_retains_id_but_refreshes_exact_digest(self):
        first = select_report(self.engine, self.relative)
        self.report.write_text('{"status":"installed"}\n')
        second = select_report(self.engine, self.relative)
        self.assertEqual(first['selection_id'], second['selection_id'])
        self.assertNotEqual(first['sha256'], second['sha256'])
        result = report_evidence(self.engine, {'selection_id': second['selection_id'], 'excerpt': 'installed'})
        self.assertEqual(result['sha256'], digest(self.report.read_bytes()))

    def test_json_credential_keys_are_rejected_before_report_selection(self):
        for payload in ('{"api_key":"synthetic-private-value-for-review"}',
                        '{"password": "synthetic-private-value-for-review"}',
                        "{'access_token':'synthetic-private-value-for-review'}"):
            self.report.write_text(payload)
            with self.subTest(payload=payload), self.assertRaisesRegex(WorkflowError, 'Secret'):
                select_report(self.engine, self.relative)
        self.assertEqual(self.engine._all('report_selection'), [])
