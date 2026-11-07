"""Subprocess protocol regressions, using a disposable fake Codex executable."""
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
from akf.native import NativeClient
from akf.install import InstallError

STUB = '''#!{python}
import json,os,sys,time
from pathlib import Path
Path(os.environ['NATIVE_TEST_ARGS']).write_text(json.dumps(sys.argv[1:]))
for line in sys.stdin:
 m=json.loads(line)
 with open(os.environ['NATIVE_TEST_METHODS'],'a') as f:f.write(m['method']+'\\n')
 if 'id' not in m:continue
 if m['method']=='initialize':
  print(json.dumps({{'id':m['id'],'result':{{}}}}),flush=True);continue
 scenario=os.environ.get('NATIVE_TEST_SCENARIO','normal')
 if scenario=='error':
  print(json.dumps({{'id':m['id'],'error':{{'code':-32601,'message':'unsupported'}}}}),flush=True)
 elif scenario=='exit':sys.exit(0)
 elif scenario=='timeout':time.sleep(30)
 else:
  print(json.dumps({{'method':'status/notification','params':{{}}}}),flush=True)
  print(json.dumps({{'id':m['id'],'result':{{'data':[]}}}}),flush=True)
'''


class NativeStdioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='akf-native-')
        root = Path(self.temp.name)
        self.args = root/'args.json'
        self.methods = root/'methods.log'
        stub = root/'codex'
        stub.write_text(STUB.format(python=sys.executable))
        stub.chmod(0o755)
        self.env = patch.dict(os.environ, {'PATH': str(root)+os.pathsep+os.environ['PATH'],
            'NATIVE_TEST_ARGS':str(self.args),'NATIVE_TEST_METHODS':str(self.methods),
            'NATIVE_TEST_SCENARIO':'normal'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_no_daemon_hooks_query_and_cleanup(self):
        with NativeClient() as client:
            process = client.process
            self.assertEqual(client.request('hooks/list', {'cwds':['/fixture']}), {'data':[]})
        self.assertIsNotNone(process.poll())
        self.assertEqual(json.loads(self.args.read_text()), ['--no-daemon','app-server','--stdio'])
        self.assertEqual(self.methods.read_text().splitlines(), ['initialize','initialized','hooks/list'])

    def test_rpc_error_keeps_native_review_and_cleans_process(self):
        os.environ['NATIVE_TEST_SCENARIO']='error'
        with self.assertRaisesRegex(InstallError, '尚未提供此查询'):
            with NativeClient() as client:
                process=client.process
                client.request('hooks/list',{})
        self.assertIsNotNone(process.poll())

    def test_native_query_error_records_method_and_numeric_code(self):
        os.environ['NATIVE_TEST_SCENARIO']='error'
        with self.assertRaisesRegex(InstallError, r'thread/items/list, code -32601'):
            with NativeClient() as client:
                client.request('thread/items/list', {'threadId':'fixture','turnId':'turn'})

    def test_trusted_hooks_alone_do_not_enable_maintenance(self):
        from akf.install import Manager, EVENTS
        from akf.setup_cli import inspect
        built = subprocess.run([sys.executable, '-B', str(PACKAGE/'scripts/build_package.py'), '--output', str(Path(self.temp.name)/'build')], capture_output=True, text=True, check=True)
        original = Path(json.loads(built.stdout)['package'])
        home = (Path(self.temp.name)/'home').resolve()
        project = home/'project'
        project.mkdir(parents=True)
        paths = {'HOME':str(home), 'AKF_HOME':str(home/'Library/Application Support/AgentKnowledgeWorkflow'),
                 'AKF_INSTALL_ROOT':str(home/'runtime'), 'AKF_BIN_DIR':str(home/'bin'),
                 'CODEX_HOME':str(home/'.codex')}
        with patch.dict(os.environ, paths):
            manager = Manager()
            with manager.lock():
                manager.install_package(original)
                manager.setup(str(project),str(home/'knowledge'),'local','plugin',
                              {'update':True,'commit':False,'push':False})
            mapping=manager.select(str(project))
            hooks=[{'eventName':name[0].lower()+name[1:], 'currentHash':name,
                    'source':'plugin','pluginId':'agent-knowledge-workflow@fixture',
                    'trustStatus':'trusted','enabled':True} for name in EVENTS]
            class Client:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def request(self,*args):return {'data':[{'cwd':str(project),'errors':[],'hooks':hooks}]}
            with patch('akf.native.NativeClient',return_value=Client()), patch('akf.setup_cli.environment',return_value={}):
                result=inspect(manager,mapping,verify=False)
            self.assertTrue(result['states']['Installed'])
            self.assertEqual(result['states']['Trusted'],'已信任')
            self.assertFalse(result['states']['Enabled'])
            self.assertEqual(result['verification'],'待验证')

    def test_process_exit_is_reported_and_cleaned(self):
        os.environ['NATIVE_TEST_SCENARIO']='exit'
        with self.assertRaisesRegex(InstallError, '进程已关闭'):
            with NativeClient() as client:
                process=client.process
                client.request('hooks/list',{})
        self.assertIsNotNone(process.poll())

    def test_timeout_is_reported_and_own_process_is_terminated(self):
        os.environ['NATIVE_TEST_SCENARIO']='timeout'
        with self.assertRaisesRegex(InstallError, '查询超时'):
            with NativeClient() as client:
                process=client.process
                client.request('hooks/list',{})
        self.assertIsNotNone(process.poll())


if __name__=='__main__':unittest.main()
