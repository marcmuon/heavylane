"""Run the CLI against an isolated SSH transport and real host processes."""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.test_heavylane import ROOT, cli, host, write_json


class FailurePaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.repo = self.base / 'example-sim'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'run.py').write_text('print("example")\n')
        self.tools = self.base / 'tools'
        self.tools.mkdir()
        self.install_stub('ssh', r'''
import os, subprocess, sys, time
args = sys.argv[1:]
command = ' '.join(args[args.index('buildbox') + 1:])
with open(os.environ['LOCAL_CALLS'], 'a') as calls:
    calls.write(command + '\n')
if 'rr_job.py cancel ' in command and 'LOCAL_CANCEL_RC' in os.environ:
    print('synthetic host cancellation failure', file=sys.stderr)
    raise SystemExit(int(os.environ['LOCAL_CANCEL_RC']))
if 'hold-lock' in command and os.environ.get('LOCAL_DROP_LOCK'):
    child = subprocess.Popen(['/bin/bash', '-c', command], stdout=subprocess.PIPE, text=True)
    print(child.stdout.readline().strip(), flush=True)
    time.sleep(0.2)
    child.terminate()
    child.wait()
    raise SystemExit(0)
os.execl('/bin/bash', 'bash', '-c', command)
''')
        self.install_stub('tmux', r'''
import os, subprocess, sys
args = sys.argv[1:]
subprocess.Popen(['/bin/bash', '-c', args[-1]], cwd=args[args.index('-c') + 1],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, start_new_session=True)
''')
        self.install_stub('caffeinate', '')
        self.hosts = self.base / 'hosts.json'
        self.limits = {'mem_limit_gb': 0.5, 'min_free_pct': 0,
                       'swap_growth_kill_gb': 0, 'swap_growth_free_pct': 25}
        write_json(self.hosts, {'default': 'buildbox', 'hosts': {'buildbox': {
            'ssh': 'buildbox', 'python': sys.executable,
            'path_prefix': str(self.tools), 'grace_seconds': 0, **self.limits}}})
        self.projects = self.base / 'projects'
        self.projects.mkdir()
        self.environment = dict(os.environ, HOME=str(self.home),
                                PATH=str(self.tools) + os.pathsep + os.environ['PATH'],
                                HEAVYLANE_HOSTS_FILE=str(self.hosts),
                                HEAVYLANE_PROJECTS_DIR=str(self.projects),
                                LOCAL_RSYNC=shutil.which('rsync'),
                                LOCAL_CALLS=str(self.base / 'calls.log'))
        self.remote_root = self.home / 'heavylane'
        helper = self.remote_root / 'bin/rr_job.py'
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT / 'lib/rr_job.py').read_bytes())

    def install_stub(self, name, source):
        path = self.tools / name
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)

    def invoke(self, *arguments, extra_env=None):
        environment = dict(self.environment, **(extra_env or {}))
        return subprocess.run([sys.executable, str(ROOT / 'bin/heavylane'), *arguments],
                              cwd=self.repo, env=environment, text=True, capture_output=True, timeout=25)

    def test_cancel_host_errors_never_report_success(self):
        for code in [1, 70, 78]:
            with self.subTest(code=code):
                result = self.invoke('cancel', 'heavylane-example-1',
                                     extra_env={'LOCAL_CANCEL_RC': str(code)})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('synthetic host cancellation failure', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        missing = self.invoke('cancel', 'heavylane-example-1', extra_env={'LOCAL_CANCEL_RC': '3'})
        self.assertEqual(missing.returncode, 2, missing.stderr)

    def test_preview_and_real_submit_use_the_same_host_thresholds(self):
        write_json(self.projects / 'example-sim.json', {'min_free_pct': 99,
                   'swap_growth_kill_gb': 0.01, 'swap_growth_free_pct': 99})
        preview = self.invoke('submit', '--dry-run', '--', 'echo', 'ready')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        submitted = self.invoke('submit', '--queue', '--wait', '--interval', '1', '--timeout', '10',
                                '--', 'echo', 'ready')
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        job_id = submitted.stdout.splitlines()[0]
        job = json.loads((self.remote_root / 'jobs' / job_id / 'job.json').read_text())
        observed = {key: job[key] for key in self.limits}
        self.assertEqual(json.loads(preview.stdout)['limits'], observed)
        self.assertEqual(observed, self.limits)

    def test_fetch_refuses_lost_job_with_a_live_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        job_id = 'heavylane-example-lost'
        jobdir = self.remote_root / 'jobs' / job_id
        write_json(jobdir / 'job.json', {'project': 'example', 'fetch': []})
        write_json(jobdir / 'status.json', {'state': 'running', 'wrapper_pid': 2147483647,
                                           'child_pid': child.pid})
        result = self.invoke('fetch', job_id, '--dest', str(self.base / 'results'))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('partial', result.stderr)
        self.assertFalse((self.base / 'results').exists())
        self.assertIsNone(child.poll())
        partial = self.invoke('fetch', job_id, '--partial', '--dest', str(self.base / 'results'))
        self.assertEqual(partial.returncode, 0, partial.stderr)
        self.assertIn('warning:', partial.stderr)
        self.assertIn('partial', partial.stderr)
        child.terminate()
        child.wait()

    def test_bad_config_types_fail_before_connecting(self):
        for configuration, field in [({'env': {'X': 1}}, 'env'), ({'data': 'inputs'}, 'data'),
                                     ({'fetch': [1]}, 'fetch'), ({'roots': [1]}, 'roots'),
                                     ({'mem_limit_gb': float('nan')}, 'mem_limit_gb'),
                                     ({'mem_limit_gb': 10**400}, 'mem_limit_gb'),
                                     ({'mem_limit_gb': 1e308}, 'mem_limit_gb')]:
            with self.subTest(field=field):
                write_json(self.projects / 'example-sim.json', configuration)
                result = self.invoke('submit', '--dry-run', '--', 'echo', 'ready')
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn(field, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.base / 'calls.log').exists())

    def test_cached_rsync_uses_the_configured_host_python_path(self):
        host_tools = self.base / 'host-tools'
        host_tools.mkdir()
        (host_tools / 'host-python').symlink_to(sys.executable)
        configuration = json.loads(self.hosts.read_text())
        configuration['hosts']['buildbox'].update(python='host-python', path_prefix=str(host_tools))
        write_json(self.hosts, configuration)
        (self.repo / 'inputs').mkdir()
        (self.repo / 'inputs/value.csv').write_text('42\n')
        write_json(self.projects / 'example-sim.json', {'data': ['inputs']})
        command = 'from pathlib import Path; Path("result.txt").write_text(Path("inputs/value.csv").read_text())'
        result = self.invoke('submit', '--queue', '--wait', '--interval', '1', '--timeout', '10',
                             '--', sys.executable, '-c', command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = self.remote_root / 'jobs' / result.stdout.splitlines()[0]
        self.assertEqual((job / 'src/result.txt').read_text(), '42\n')
        self.assertEqual((self.remote_root / 'cache/example-sim/inputs/value.csv').read_text(), '42\n')
        self.assertEqual(json.loads((job / 'status.json').read_text())['state'], 'done')
        self.assertFalse((self.remote_root / 'cache/example-sim.lock.owner.json').exists())

    def test_unobservable_detached_worker_requires_partial_fetch(self):
        job_id = 'heavylane-example-error'
        jobdir = self.remote_root / 'jobs' / job_id
        source = jobdir / 'src'
        source.mkdir(parents=True)
        pid_path = source / 'worker.pid'
        heartbeat = source / 'worker.heartbeat'
        worker = ('import time; stream=open("worker.heartbeat","ab",buffering=0); '
                  'exec("for _ in range(750): stream.write(bytes([46])); time.sleep(0.02)")')
        command = ('import pathlib,subprocess,sys,time; '
                   'worker=subprocess.Popen([sys.executable,"-c",%r],start_new_session=True); '
                   'pathlib.Path("worker.pid").write_text(str(worker.pid)); time.sleep(15)' % worker)
        write_json(jobdir / 'job.json', {'remote_root': str(self.remote_root), 'job_id': job_id,
                   'project': 'example-sim', 'fetch': [], 'grace_seconds': 0,
                   'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(command)})
        real_run = subprocess.run

        def no_process_sample(args, **kwargs):
            if args[0] == 'ps':
                deadline = time.monotonic() + 3
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_path.exists(), 'worker did not start')
                return subprocess.CompletedProcess(args, 1, '', 'synthetic unavailable process table')
            return real_run(args, **kwargs)

        old_signals = {sig: host.signal.getsignal(sig) for sig in
                       (host.signal.SIGTERM, host.signal.SIGINT, host.signal.SIGHUP)}
        try:
            with mock.patch.object(host.subprocess, 'run', side_effect=no_process_sample), \
                    mock.patch.object(host.shutil, 'which', return_value=None), \
                    mock.patch.object(host, 'host_memory', return_value={'swap_used_gb': 0, 'free_pct': 90}), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(host.ProcessTableError):
                host.cmd_run(str(jobdir), False)
            size = heartbeat.stat().st_size
            time.sleep(0.1)
            self.assertGreater(heartbeat.stat().st_size, size, 'detached worker did not survive')
            result = self.invoke('fetch', job_id, '--dest', str(self.base / 'results'))
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('partial', result.stderr)
            self.assertFalse((self.base / 'results').exists())
            status = json.loads((jobdir / 'status.json').read_text())
            self.assertEqual(status['state'], 'error')
            self.assertTrue(status['cleanup_error'])
            partial = self.invoke('fetch', job_id, '--partial', '--dest', str(self.base / 'results'))
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertIn('warning:', partial.stderr)
        finally:
            for sig, handler in old_signals.items():
                host.signal.signal(sig, handler)
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_cache_lock_loss_interrupts_the_transfer_before_launch(self):
        (self.repo / 'inputs').mkdir()
        (self.repo / 'inputs/value.csv').write_text('42\n')
        write_json(self.projects / 'example-sim.json', {'data': ['inputs']})
        self.install_stub('rsync', r'''
import os, pathlib, sys, time
if '--server' not in sys.argv:
    time.sleep(2)
    pathlib.Path(os.environ['LOCAL_TRANSFER_FINISHED']).write_text('transfer continued without lock')
rsync = os.environ['LOCAL_RSYNC']
os.execv(rsync, [rsync] + sys.argv[1:])
''')
        result = self.invoke('submit', '--queue', '--', 'echo', 'ready', extra_env={
            'LOCAL_DROP_LOCK': '1', 'LOCAL_TRANSFER_FINISHED': str(self.base / 'transfer-finished')})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('data-cache lock', result.stderr)
        self.assertFalse((self.base / 'transfer-finished').exists())
        # The half-built job directory goes too: without job.json nothing could reclaim it.
        self.assertFalse(list((self.remote_root / 'jobs').glob('*')))

    def test_submit_collision_preserves_the_existing_job_and_results(self):
        job_id = 'heavylane-example-sim-20000101-000000-beef'
        jobdir = self.remote_root / 'jobs' / job_id
        write_json(jobdir / 'job.json', {'project': 'example-sim', 'job_id': job_id})
        write_json(jobdir / 'status.json', {'state': 'done', 'exit_code': 0})
        (jobdir / 'result.txt').write_text('previous completed result\n')
        (jobdir / '.submit-owner').write_text('previous-submission\n')
        expected = {path.name: path.read_bytes() for path in jobdir.iterdir()}
        sibling = jobdir.parent / 'unrelated-job'
        sibling.mkdir()
        (sibling / 'keep.txt').write_text('unrelated result\n')
        (self.repo / 'inputs').mkdir()
        (self.repo / 'inputs/value.csv').write_text('42\n')
        self.install_stub('rsync', 'import sys; print("synthetic failed transfer", file=sys.stderr); sys.exit(1)\n')
        real_strftime = time.strftime

        def fixed_stamp(fmt, *args):
            return '20000101-000000' if fmt == '%Y%m%d-%H%M%S' else real_strftime(fmt, *args)

        original_cwd = os.getcwd()
        diagnostic = io.StringIO()
        try:
            os.chdir(self.repo)
            with mock.patch.dict(os.environ, self.environment, clear=True), \
                    mock.patch.multiple(cli, HOSTS_FILE=str(self.hosts), PROJECTS_DIR=str(self.projects),
                                        HOME=str(self.home), STATE_DIR=str(self.home / '.local/share/heavylane')), \
                    mock.patch.object(cli.secrets, 'token_hex', side_effect=lambda n: 'beef' if n == 2 else 'a' * 32), \
                    mock.patch.object(cli.time, 'strftime', side_effect=fixed_stamp), \
                    contextlib.redirect_stderr(diagnostic), self.assertRaises(SystemExit) as stopped:
                cli.main(['submit', '--queue', '--data', 'inputs', '--', 'echo', 'ready'])
        finally:
            os.chdir(original_cwd)
        self.assertEqual(stopped.exception.code, 70, diagnostic.getvalue())
        self.assertTrue(jobdir.exists(), 'failed submit deleted an existing job')
        observed = {path.relative_to(jobdir).as_posix(): path.read_bytes()
                    for path in jobdir.rglob('*') if path.is_file()}
        self.assertEqual(observed, expected)
        self.assertEqual((sibling / 'keep.txt').read_text(), 'unrelated result\n')
        self.assertNotIn('tmux new-session', (self.base / 'calls.log').read_text())

    def test_submit_reports_failed_cleanup_and_preserves_the_transfer_error(self):
        parent = self.remote_root / 'jobs'
        parent.mkdir()
        self.addCleanup(lambda: parent.chmod(0o700))
        sibling = parent / 'unrelated-job'
        sibling.mkdir()
        (sibling / 'keep.txt').write_text('unrelated result\n')
        (self.repo / 'inputs').mkdir()
        (self.repo / 'inputs/value.csv').write_text('42\n')
        self.install_stub('rsync', '''
import os, pathlib, sys
pathlib.Path(os.environ['LOCAL_DENY_REMOVE_PARENT']).chmod(0o500)
print('synthetic failed transfer', file=sys.stderr)
raise SystemExit(1)
''')
        result = self.invoke('submit', '--queue', '--data', 'inputs', '--', 'echo', 'ready',
                             extra_env={'LOCAL_DENY_REMOVE_PARENT': str(parent)})
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIn('synthetic failed transfer', result.stderr)
        self.assertIn('warning: could not remove', result.stderr)
        remaining = [path for path in parent.iterdir() if path != sibling]
        self.assertEqual(len(remaining), 1)
        self.assertFalse((remaining[0] / 'job.json').exists())
        self.assertEqual((sibling / 'keep.txt').read_text(), 'unrelated result\n')
        self.assertNotIn('tmux new-session', (self.base / 'calls.log').read_text())


class HostTelemetry(unittest.TestCase):
    def test_one_failed_process_sample_recovers_and_the_real_job_finishes(self):
        for failure in ('exit', 'timeout'):
            with self.subTest(failure=failure):
                self.assert_sample_recovers(failure)

    def assert_sample_recovers(self, failure):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobdir = root / 'jobs/example'
            (jobdir / 'src').mkdir(parents=True)
            command = 'import pathlib,time; time.sleep(0.5); pathlib.Path("completed").write_text("42")'
            write_json(jobdir / 'job.json', {'remote_root': str(root), 'job_id': 'example',
                       'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(command),
                       'mem_limit_gb': 0.5, 'grace_seconds': 0})
            real_run = subprocess.run
            failed = []

            def one_ps_error(args, **kwargs):
                if args[0] == 'ps' and not failed:
                    failed.append(True)
                    if failure == 'timeout':
                        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
                    return subprocess.CompletedProcess(args, 1, '', 'synthetic transient ps failure')
                return real_run(args, **kwargs)

            old_signals = {sig: host.signal.getsignal(sig) for sig in
                           (host.signal.SIGTERM, host.signal.SIGINT, host.signal.SIGHUP)}
            try:
                with mock.patch.object(host.subprocess, 'run', side_effect=one_ps_error), \
                        mock.patch.object(host.shutil, 'which', return_value=None), \
                        mock.patch.object(host, 'host_memory', return_value={'swap_used_gb': 0, 'free_pct': 90}), \
                        contextlib.redirect_stderr(io.StringIO()) as diagnostic:
                    self.assertEqual(host.cmd_run(str(jobdir), False), 0)
            finally:
                for sig, handler in old_signals.items():
                    host.signal.signal(sig, handler)
            self.assertTrue(failed)
            self.assertIn('retry', diagnostic.getvalue())
            self.assertEqual((jobdir / 'src/completed').read_text(), '42')
            self.assertEqual(json.loads((jobdir / 'status.json').read_text())['state'], 'done')

    def test_malformed_job_fails_before_taking_the_host_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobdir = root / 'jobs/example'
            (jobdir / 'src').mkdir(parents=True)
            write_json(jobdir / 'job.json', {'remote_root': str(root), 'job_id': 'example',
                       'command': 'touch should-not-run', 'env': {'X': 1}})
            result = subprocess.run([sys.executable, str(ROOT / 'lib/rr_job.py'), 'run', str(jobdir)],
                                    text=True, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn('env', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse((root / 'host.lock').exists())
            self.assertFalse((jobdir / 'src/should-not-run').exists())
            self.assertEqual((jobdir / 'exit_code').read_text(), '78\n')
            self.assertEqual(json.loads((jobdir / 'status.json').read_text())['exit_code'], 78)

    def test_cache_writer_retains_its_lock_after_the_lease_process_dies(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            lock = str(base / 'cache.lock')
            helper = [sys.executable, str(ROOT / 'lib/rr_job.py')]
            first = subprocess.Popen(helper + ['hold-lock', lock], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
            second = operation = None

            def line(process):
                self.assertTrue(select.select([process.stdout], [], [], 3)[0], 'cache handshake timed out')
                return process.stdout.readline().strip()

            try:
                token = line(first).split()[1]
                started, release = base / 'started', base / 'release'
                writer = ('import pathlib,time; '
                          'pathlib.Path(%r).touch(); '
                          'exec("while not pathlib.Path(%r).exists(): time.sleep(0.02)")' %
                          (str(started), str(release)))
                operation = subprocess.Popen(helper + ['cache-command', lock, token,
                                                        sys.executable, '-c', writer])
                deadline = time.monotonic() + 3
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(started.exists())
                first.terminate()
                first.wait(timeout=3)
                second = subprocess.Popen(helper + ['hold-lock', lock], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, text=True)
                self.assertEqual(line(second), 'WAITING')
                self.assertFalse(select.select([second.stdout], [], [], 0.2)[0],
                                 'a new cache owner entered while the old writer was active')
                release.touch()
                self.assertEqual(operation.wait(timeout=3), 0)
                replacement = line(second).split()[1]
                self.assertNotEqual(token, replacement)
                stale = subprocess.run(helper + ['cache-command', lock, token,
                                                  sys.executable, '-c', 'print("unsafe")'],
                                       text=True, capture_output=True, timeout=3)
                self.assertEqual(stale.returncode, 70, stale.stderr)
                self.assertNotIn('unsafe', stale.stdout)
                self.assertIn('lease was lost', stale.stderr)
            finally:
                (base / 'release').touch()
                for process in [operation, first, second]:
                    if process is None:
                        continue
                    if process.stdin is not None:
                        process.stdin.close()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    if process.stdout is not None:
                        process.stdout.close()


if __name__ == '__main__':
    unittest.main()
