import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from akf import git_sync
from akf.engine import Engine, digest
from test_engine import Fixture


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


class GitSyncTests(Fixture):
    def setUp(self):
        super().setUp()
        self.environment = patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                                                  'GIT_TERMINAL_PROMPT': '0'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.engine.db.close()
        (self.root / 'state' / 'state.sqlite3').unlink()
        git(self.knowledge, 'init', '-b', 'main')
        git(self.knowledge, 'config', 'user.name', 'Synthetic Fixture')
        git(self.knowledge, 'config', 'user.email', 'fixture@example.test')
        (self.knowledge / 'README.md').write_text('# Synthetic knowledge\n')
        git(self.knowledge, 'add', '--', 'README.md')
        git(self.knowledge, 'commit', '-m', 'fixture baseline')
        self.remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], capture_output=True, check=True)
        git(self.knowledge, 'remote', 'add', 'origin', str(self.remote))
        git(self.knowledge, 'push', '-u', 'origin', 'main')
        self.settings['git'] = {'auto_commit': True, 'auto_push': True, 'remote': 'origin', 'branch': 'main',
                                'remote_url': str(self.remote), 'transport': 'local', 'repository': 'local-fixture'}
        self.config.write_text(json.dumps(self.settings))
        self.engine = Engine(self.config); self.addCleanup(self.engine.db.close)
        self.baseline = git(self.knowledge, 'rev-parse', 'HEAD')

    def test_exact_commit_push_readback_and_no_duplicate(self):
        (self.knowledge / 'human.txt').write_text('Unrelated human work stays untracked.\n')
        job = self.job(); candidate = self.candidate(job)
        result = self.engine.submit(job['id'], candidate)
        self.assertEqual(result['state'], 'remote_confirmed')
        sha = result['commit']
        self.assertEqual(git(self.knowledge, 'rev-parse', sha + '^'), self.baseline)
        self.assertEqual(git(self.knowledge, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha), 'topics/limits.md')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), sha)
        remote_data = subprocess.check_output(['git', '--git-dir', str(self.remote), 'show', sha + ':topics/limits.md'])
        self.assertEqual(remote_data, (self.knowledge / 'topics' / 'limits.md').read_bytes())
        self.engine.submit(job['id'], candidate); self.engine.resume(job['id'])
        self.assertEqual(git(self.knowledge, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(git(self.knowledge, 'status', '--porcelain'), '?? human.txt')
        self.assertEqual(git(self.knowledge, 'diff', '--cached', '--name-only'), '')

    def test_existing_index_is_preserved_and_not_committed(self):
        (self.knowledge / 'human.txt').write_text('Human staged content.\n')
        git(self.knowledge, 'add', '--', 'human.txt')
        staged = git(self.knowledge, 'ls-files', '--stage')
        job = self.job()
        self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'conflict')
        self.assertEqual(git(self.knowledge, 'rev-parse', 'HEAD'), self.baseline)
        self.assertEqual(git(self.knowledge, 'ls-files', '--stage'), staged)
        self.assertTrue((self.knowledge / 'topics' / 'limits.md').exists())

    def test_human_changes_in_owned_note_are_not_mixed_into_commit(self):
        note = self.knowledge / 'topics' / 'limits.md'
        note.write_text('# Limit\n')
        git(self.knowledge, 'add', '--', 'topics/limits.md'); git(self.knowledge, 'commit', '-m', 'manual topic')
        note.write_text(note.read_text() + 'Uncommitted human paragraph.\n')
        before = git(self.knowledge, 'rev-parse', 'HEAD')
        job = self.job()
        self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'conflict')
        self.assertEqual(git(self.knowledge, 'rev-parse', 'HEAD'), before)
        self.assertIn('Uncommitted human paragraph.', note.read_text())

    def test_crash_after_commit_recovers_same_commit(self):
        job = self.job(); original = git_sync._run
        def crash(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == 'commit':
                raise git_sync.SyncError('retry', 'Synthetic interruption after commit')
            return result
        with patch('akf.git_sync._run', side_effect=crash):
            self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'retry')
        committed = git(self.knowledge, 'rev-parse', 'HEAD')
        self.assertNotEqual(committed, self.baseline)
        self.assertEqual(self.engine.resume(job['id'])['state'], 'remote_confirmed')
        self.assertEqual(git(self.knowledge, 'rev-parse', 'HEAD'), committed)
        self.assertEqual(git(self.knowledge, 'rev-list', '--count', 'HEAD'), '2')

    def test_push_failure_then_recovery_keeps_one_commit(self):
        job = self.job(); original = git_sync._run
        def fail_push(root, *args, **kwargs):
            if args[0] == 'push':
                return subprocess.CompletedProcess(args, 128, '', 'synthetic network failure')
            return original(root, *args, **kwargs)
        with patch('akf.git_sync._run', side_effect=fail_push):
            result = self.engine.submit(job['id'], self.candidate(job))
        self.assertEqual(result['state'], 'retry')
        self.assertEqual(result['write_state'], 'committed')
        self.assertEqual(self.engine.status(job['id'])['write_state'], 'committed')
        local = git(self.knowledge, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.baseline)
        self.assertTrue((self.knowledge / 'topics' / 'limits.md').exists())
        self.assertEqual(self.engine.resume(job['id'])['state'], 'remote_confirmed')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), local)
        self.assertEqual(git(self.knowledge, 'rev-list', '--count', 'HEAD'), '2')

    def test_uncertain_push_queries_remote_before_retry(self):
        job = self.job(); original = git_sync._run; pushes = []
        def uncertain(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == 'push':
                pushes.append(args)
                return subprocess.CompletedProcess(args, 128, '', 'synthetic lost acknowledgement')
            return result
        with patch('akf.git_sync._run', side_effect=uncertain):
            self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'remote_confirmed')
            self.engine.resume(job['id'])
        self.assertEqual(len(pushes), 1)
        self.assertEqual(git(self.knowledge, 'rev-list', '--count', 'HEAD'), '2')

    def test_remote_advance_is_conflict_without_merge_or_force(self):
        other = self.root / 'other'
        subprocess.run(['git', 'clone', '-b', 'main', str(self.remote), str(other)], capture_output=True, check=True)
        git(other, 'config', 'user.name', 'Other Fixture'); git(other, 'config', 'user.email', 'other@example.test')
        (other / 'elsewhere.md').write_text('Independent new remote fact.\n')
        git(other, 'add', '--', 'elsewhere.md'); git(other, 'commit', '-m', 'other change'); git(other, 'push', 'origin', 'main')
        remote_head = git(other, 'rev-parse', 'HEAD')
        job = self.job()
        self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'conflict')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), remote_head)
        self.assertEqual(git(self.knowledge, 'rev-parse', 'HEAD^'), self.baseline)

    def test_changed_remote_binding_refuses_commit_and_push(self):
        other = self.root / 'other.git'
        subprocess.run(['git', 'init', '--bare', str(other)], capture_output=True, check=True)
        git(self.knowledge, 'remote', 'set-url', 'origin', str(other))
        job = self.job()
        self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'conflict')
        self.assertEqual(git(self.knowledge, 'rev-parse', 'HEAD'), self.baseline)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.baseline)

    def test_auto_push_default_off(self):
        self.engine.db.close(); (self.root / 'state' / 'state.sqlite3').unlink()
        self.settings['git'].pop('auto_push')
        self.config.write_text(json.dumps(self.settings)); self.engine = Engine(self.config); self.addCleanup(self.engine.db.close)
        job = self.job()
        self.assertEqual(self.engine.submit(job['id'], self.candidate(job))['state'], 'committed')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.baseline)

    def test_undo_shared_knowledge_uses_new_commit(self):
        job = self.job(); first = self.engine.submit(job['id'], self.candidate(job))
        self.assertEqual(first['state'], 'remote_confirmed')
        undone = self.engine.undo(job['id'])
        self.assertEqual(undone['state'], 'remote_confirmed')
        self.assertEqual(git(self.knowledge, 'rev-parse', undone['commit'] + '^'), first['commit'])
        self.assertEqual(git(self.knowledge, 'rev-list', '--count', 'HEAD'), '3')
        self.assertNotIn('The request limit is eight.', (self.knowledge / 'topics' / 'limits.md').read_text())
        self.engine.undo(job['id'])
        self.assertEqual(git(self.knowledge, 'rev-list', '--count', 'HEAD'), '3')


    def test_commit_hook_extra_path_cannot_escape_on_resume(self):
        # This executable hook exists only in this test's disposable local repository.
        hook = self.knowledge / '.git' / 'hooks' / 'pre-commit'
        hook.write_text("#!/bin/sh\nprintf 'synthetic hook side effect\\n' > hook-owned-extra.md\ngit add -- hook-owned-extra.md\n")
        hook.chmod(0o755)
        job = self.job()
        first = self.engine.submit(job['id'], self.candidate(job))
        self.assertEqual(first['state'], 'conflict')
        unexpected_commit = git(self.knowledge, 'rev-parse', 'HEAD')
        self.assertIn('hook-owned-extra.md', git(self.knowledge, 'diff-tree', '--no-commit-id', '--name-only', '-r', unexpected_commit))
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.baseline)
        second = self.engine.resume(job['id'])
        self.assertEqual(second['state'], 'conflict')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.baseline)
        self.assertEqual(git(self.knowledge, 'rev-parse', 'HEAD'), unexpected_commit)
        self.assertTrue((self.knowledge / 'hook-owned-extra.md').exists())


if __name__ == '__main__':
    unittest.main()
