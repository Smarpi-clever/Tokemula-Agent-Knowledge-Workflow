"""Search fixtures exercise local derived state and preserve source Markdown."""
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from akf.engine import WorkflowError, digest
from akf.retrieval import SearchIndex, encoded_size
from test_engine import Fixture


class RetrievalTests(Fixture):
    def setUp(self):
        super().setUp()
        self.index = SearchIndex(self.engine)

    def note(self, name, text):
        path = self.knowledge / 'topics' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_chinese_english_mixed_body_path_and_error_search(self):
        self.note('network.md', '# Runtime operations\n\n连接池耗尽后先关闭空闲连接。retry budget 是三次。\n')
        self.note('paths.md', '# Troubleshooting\n\nInspect src/cache/store.py when EADDRINUSE appears.\n')
        for query, expected in [('连接池耗尽', 'network.md'), ('retry budget', 'network.md'),
                                ('连接池 retry', 'network.md'), ('src/cache/store.py', 'paths.md'),
                                ('EADDRINUSE', 'paths.md')]:
            result = self.index.search(query)
            self.assertEqual(result['results'][0]['path'], 'topics/' + expected)
            self.assertEqual(result['results'][0]['source_origin'], 'human_note')
            self.assertEqual(result['results'][0]['evidence_status'], 'unknown')
            self.assertIn('start_line', result['results'][0]['read'])
        self.assertEqual(self.index.search('unrelated-nebula')['results'], [])
        self.assertEqual(self.index.search('！')['results'], [])

    def test_incremental_refresh_reads_only_changed_notes_and_deletes(self):
        first = self.note('first.md', '# First\n\n旧缓存策略。\n')
        second = self.note('second.md', '# Second\n\nStable document.\n')
        self.index.search('缓存策略')
        original = Path.read_bytes
        reads = []

        def observed(path):
            if path.is_relative_to(self.knowledge):
                reads.append(path)
            return original(path)

        with patch.object(Path, 'read_bytes', autospec=True, side_effect=observed):
            self.assertTrue(self.index.search('缓存策略')['results'])
            self.assertEqual(reads, [])
            first.write_text('# First\n\n最新索引采用精确短语。\n')
            self.assertTrue(self.index.search('精确短语')['results'])
            self.assertEqual(reads, [first])
        self.assertEqual(self.index.search('缓存策略')['results'], [])
        second.unlink()
        self.assertEqual(self.index.search('Stable')['results'], [])

    def test_current_history_and_reverification_are_separate(self):
        job = self.job(8)
        self.engine.submit(job['id'], self.candidate(job, 'The request limit is eight.'))
        second = self.job(12)
        self.engine.submit(second['id'], self.candidate(second, 'The request limit is twelve.'))
        current = self.index.search('request limit')['results']
        facts = [value for value in current if value['source_origin'] == 'verified_fact']
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['lifecycle'], 'current')
        self.assertEqual(facts[0]['evidence_status'], 'tool_verified')
        self.assertIn('twelve.', facts[0]['snippet'])
        self.assertFalse(any('eight.' in value['snippet'] for value in current))
        history = self.index.search('eight', history=True)['results']
        old = next(value for value in history if value['source_origin'] == 'history')
        self.assertEqual(old['lifecycle'], 'superseded')
        self.assertIn('settings.py', old['sources'][0]['path'])
        self.assertEqual(self.index.search('eight')['results'], [])

    def test_manual_block_edit_drops_trusted_status(self):
        job = self.job()
        self.engine.submit(job['id'], self.candidate(job))
        self.assertTrue(self.index.search('eight')['results'])
        path = self.knowledge / 'topics/limits.md'
        path.write_text(path.read_text().replace('eight.', 'nineteen.'))
        changed = self.index.search('nineteen')['results'][0]
        self.assertEqual(changed['source_origin'], 'human_modified')
        self.assertEqual(changed['evidence_status'], 'unknown')
        self.assertEqual(changed['review_status'], 'review_required')
        self.assertEqual(changed['sources'], [])
        self.assertEqual(self.index.search('eight')['results'], [])

    def test_changed_source_is_review_required_without_note_read(self):
        job = self.job()
        self.engine.submit(job['id'], self.candidate(job))
        self.index.search('eight')
        (self.project / 'settings.py').write_text('LIMIT = 99\n')
        value = next(result for result in self.index.search('eight')['results']
                     if result['source_origin'] == 'verified_fact')
        self.assertEqual(value['review_status'], 'review_required')
        self.assertTrue(value['review_reasons'])

    def test_pending_candidate_is_labelled_and_removed_after_withdrawal(self):
        job = self.job()
        candidate = self.candidate(job, 'Awaiting exact file evidence.')
        candidate['updates'][0]['evidence'] = []
        self.engine.submit(job['id'], candidate)
        pending = self.index.search('Awaiting')['results'][0]
        self.assertEqual(pending['source_origin'], 'pending')
        self.assertEqual(pending['evidence_status'], 'pending')
        self.assertEqual(pending['lifecycle'], 'candidate')
        self.assertEqual(pending['review_status'], 'review_required')
        self.assertEqual(pending['read']['job'], job['id'])
        saved = self.engine._get('job', job['id'])
        saved['pending_updates'][0]['withdrawn'] = True
        self.engine._put('job', job['id'], saved)
        self.assertEqual(self.index.search('Awaiting')['results'], [])

    def test_search_budget_uses_exact_utf8_json_size_and_limit(self):
        for number in range(6):
            self.note(str(number) + '.md', '# 中文缓存\n\n缓存复核方法：' + '查看记录。' * 80)
        limited = self.index.search('缓存', limit=2)
        self.assertEqual(len(limited['results']), 2)
        result = self.index.search('缓存', limit=5, budget_bytes=1800)
        self.assertLessEqual(encoded_size(result), 1800)
        self.assertEqual(encoded_size(result), result['output_bytes'])
        self.assertTrue(result['has_more'])
        tiny = self.index.search('缓存', budget_bytes=512)
        self.assertEqual(tiny['results'], [])
        self.assertTrue(tiny['has_more'])

    def test_read_has_digest_lines_and_correct_budget_continuation(self):
        path = self.note('read.md', '# Reading\n' + '中文内容。\n' * 50)
        value = self.index.read('topics/read.md', start_line=2, budget_bytes=512)
        self.assertEqual(value['sha256'], digest(path.read_bytes()))
        self.assertEqual(value['lines'][0], '中文内容。')
        self.assertEqual(value['next_line'], value['end_line'] + 1)
        self.assertEqual(value['end_line'], 1 + len(value['lines']))
        self.assertLessEqual(encoded_size(value), 512)
        self.assertEqual(encoded_size(value), value['output_bytes'])
        detail = self.index.read('topics/read.md', start_line=value['next_line'], budget_bytes=6000)
        self.assertFalse(detail['has_more'])
        self.assertEqual(detail['end_line'], 51)

    def test_read_rejects_overbudget_line_without_stuck_cursor(self):
        self.note('long.md', '# Long\n' + '长' * 5000)
        with self.assertRaisesRegex(WorkflowError, 'First requested line exceeds budget'):
            self.index.read('topics/long.md', start_line=2, budget_bytes=512)

    def test_rebuild_preserves_state_and_markdown(self):
        job = self.job()
        self.engine.submit(job['id'], self.candidate(job))
        path = self.knowledge / 'topics/limits.md'
        text = path.read_bytes()
        fact = self.engine._get('fact', 'topics/limits.md:request-limit')
        result = self.index.rebuild()
        self.assertEqual(result['state'], 'rebuilt')
        self.assertEqual(result['files'], 1)
        self.assertEqual(path.read_bytes(), text)
        self.assertEqual(self.engine._get('fact', 'topics/limits.md:request-limit'), fact)
        self.index.path.unlink()
        self.assertTrue(self.index.search('eight')['results'])

    def test_invalid_utf8_note_is_removed_from_index(self):
        path = self.note('bad.md', '# Bad\n\nPreviously valid unicorn.\n')
        self.assertTrue(self.index.search('unicorn')['results'])
        path.write_bytes(b'\xff\xfe')
        self.assertEqual(self.index.search('unicorn')['results'], [])

    def test_input_boundaries_and_symlink_exclusion(self):
        path = self.note('legit.md', '# Legit\n\nAllowed exact content.\n')
        (self.knowledge / 'topics/link.md').symlink_to(path)
        self.assertEqual(len(self.index.topics()), 1)
        for query in ('', ' ' * 2, 'x' * 501, None):
            with self.assertRaises(WorkflowError):
                self.index.search(query)
        for limit in (True, 0, 101):
            with self.assertRaises(WorkflowError):
                self.index.search('x', limit=limit)
        for budget in (True, 511, 1_000_001):
            with self.assertRaises(WorkflowError):
                self.index.search('x', budget_bytes=budget)
        for topic in ('../outside.md', 'topics/link.md', 'README.md'):
            with self.assertRaises(WorkflowError):
                self.index.read(topic)
        for start, end in ((0, None), (True, None), (3, 2)):
            with self.assertRaises(WorkflowError):
                self.index.read('topics/legit.md', start, end)

    def test_readonly_state_uses_explicit_memory_index(self):
        self.note('readonly.md', '# Read only\n\nRead-only notes stay searchable.\n')
        before = (self.engine.state_dir / 'state.sqlite3').read_bytes()
        with patch('akf.retrieval.os.access', return_value=False):
            result = self.index.search('searchable')
            self.assertEqual(result['index_state'], 'memory_read_only_state')
            self.assertTrue(result['results'])
            self.assertEqual(self.index.topics()[0]['path'], 'topics/readonly.md')
            self.assertEqual(self.index.index_state, 'memory_read_only_state')
            self.assertEqual(self.index.read('topics/readonly.md')['index_state'], 'memory_read_only_state')
        self.assertFalse(self.index.path.exists())
        self.assertEqual((self.engine.state_dir / 'state.sqlite3').read_bytes(), before)

    def test_chinese_single_character_query_matches_longer_sequence(self):
        self.note('locking.md', '# Concurrency\n\n读写锁保护共享状态。\n')
        result = self.index.search('锁')
        self.assertEqual(result['results'][0]['path'], 'topics/locking.md')
        self.assertIn('锁', result['results'][0]['snippet'])
        self.assertEqual(self.index.search('读写')['results'][0]['path'], 'topics/locking.md')
        self.assertEqual(self.index.search('写法')['results'], [])

    def test_evidence_only_match_returns_actual_excerpt_and_source(self):
        job = self.job()
        (self.project / 'settings.py').write_text('raise ValueError("SOURCE_ONLY_ERROR")\n')
        candidate = self.candidate(job, 'The parser guards its input.')
        self.engine.submit(job['id'], candidate)
        value = self.index.search('SOURCE_ONLY_ERROR')['results'][0]
        self.assertEqual(value['source_origin'], 'verified_fact')
        self.assertIn('SOURCE_ONLY_ERROR', value['snippet'])
        self.assertEqual(value['snippet_source'], {'kind': 'file', 'path': 'settings.py'})
        self.assertEqual(value['sources'][0]['path'], 'settings.py')

    def test_old_derived_index_is_rebuilt_for_single_character_support(self):
        self.note('upgrade.md', '# Index format\n\n读写锁保护共享状态。\n')
        self.index.search('读写')
        with sqlite3.connect(self.index.path) as db:
            db.execute('PRAGMA user_version=1')
        before = (self.engine.state_dir / 'state.sqlite3').read_bytes()
        self.assertTrue(self.index.search('锁')['results'])
        with sqlite3.connect(self.index.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertEqual((self.engine.state_dir / 'state.sqlite3').read_bytes(), before)

    def test_history_hits_cannot_hide_current_result_before_filtering(self):
        job = self.job()
        candidate = self.candidate(job, 'alpha ' + 'surrounding context ' * 500)
        self.engine.submit(job['id'], candidate)
        key = 'topics/limits.md:request-limit'
        fact = self.engine._get('fact', key)
        fact['history'] = [{'content': 'alpha', 'observed_at': fact['observed_at'],
                            'version_id': 'old-' + str(number), 'evidence_status': 'tool_verified'}
                           for number in range(501)]
        self.engine._put('fact', key, fact)
        result = self.index.search('alpha')
        self.assertTrue(result['results'])
        self.assertEqual(result['results'][0]['lifecycle'], 'current')
        self.assertEqual(result['matched_count'], 1)
        historical = self.index.search('alpha', history=True)
        self.assertEqual(historical['matched_count'], 502)
        self.assertTrue(historical['has_more'])
