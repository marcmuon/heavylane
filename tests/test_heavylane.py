"""Behavior checks for the CLI boundary, config trust, snapshot and host guard."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load('heavylane_cli', ROOT / 'bin/heavylane')
host = load('heavylane_host', ROOT / 'lib/rr_job.py')


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


class ConfigAndSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'example-sim'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.projects = self.base / 'config/projects'
        self.projects.mkdir(parents=True)
        self.hosts = self.base / 'config/hosts.json'
        write_json(self.hosts, {'default': 'buildbox', 'hosts': {'buildbox': {}}})
        self.patches = mock.patch.multiple(cli, PROJECTS_DIR=str(self.projects), HOSTS_FILE=str(self.hosts))
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def config(self, cwd=None, explicit=None, trust=False):
        return cli.load_project(str(self.repo), str(cwd or self.repo), explicit, trust)

    def test_precedence_and_nearest_workdir(self):
        sub = self.repo / 'nested'
        sub.mkdir()
        write_json(self.projects / 'example-sim.json', {'notes': ['fallback']})
        self.assertEqual(self.config()['notes'], ['fallback'])
        write_json(self.projects / 'root.json', {'roots': [str(self.repo)], 'notes': ['root']})
        write_json(self.projects / 'sub.json', {'roots': [str(sub)], 'notes': ['sub']})
        self.assertEqual(self.config(sub)['notes'], ['sub'])
        write_json(self.repo / '.heavylane.json', {'notes': ['local']})
        self.assertEqual(self.config(sub)['notes'], ['local'])
        write_json(sub / '.heavylane.json', {'notes': ['nearest']})
        selected = self.config(sub)
        self.assertEqual((selected['notes'], selected['workdir']), (['nearest'], 'nested'))
        explicit = self.base / 'explicit.json'
        write_json(explicit, {'notes': ['explicit']})
        self.assertEqual(self.config(sub, str(explicit))['notes'], ['explicit'])

    def test_trust_is_user_owned_and_exact_root(self):
        path = self.repo / '.heavylane.json'
        write_json(path, {'external': ['/example/input'], 'setup': 'echo unsafe', 'env': {'X': 'Y'},
                          'fetch': ['outputs', '/example/output'], 'trusted_roots': [str(self.repo)]})
        with contextlib.redirect_stderr(io.StringIO()):
            selected = self.config()
        self.assertEqual(selected['fetch'], ['outputs'])
        self.assertEqual(set(selected['_ignored_repo_config']), {'external', 'setup', 'env', 'absolute fetch'})
        self.assertNotIn('env', selected)
        self.assertEqual(self.config(trust=True)['env'], {'X': 'Y'})
        self.assertEqual(self.config(explicit=str(path))['setup'], 'echo unsafe')
        write_json(self.hosts, {'trusted_roots': [str(self.base)]})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.config()['_repo_config_trusted'])
        write_json(self.hosts, {'trusted_roots': [str(self.repo)]})
        self.assertEqual(self.config()['external'], ['/example/input'])
        write_json(self.hosts, {})
        write_json(self.projects / 'trust.json', {'trusted_roots': [str(self.repo)]})
        self.assertEqual(self.config()['fetch'], ['outputs', '/example/output'])

    def test_real_dry_run_uses_config_override_and_reports_trust(self):
        write_json(self.repo / '.heavylane.json', {'env': {'UNTRUSTED': '1'}, 'fetch': ['/example/output']})
        env = dict(os.environ, HEAVYLANE_HOSTS_FILE=str(self.hosts), HEAVYLANE_PROJECTS_DIR=str(self.projects))
        r = subprocess.run([sys.executable, str(ROOT / 'bin/heavylane'), 'submit', '--dry-run', '--', 'echo', 'hi'],
                           cwd=self.repo, env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual(plan['host'], 'buildbox')
        self.assertEqual(plan['command'], 'echo hi')
        self.assertNotIn('UNTRUSTED', plan['env'])
        self.assertIn('absolute fetch', plan['ignored_repo_config'])
        self.assertIn('untrusted repo config', r.stderr)

    def test_explicit_data_cannot_bypass_snapshot_secret_filter(self):
        (self.repo / '.env').write_text('synthetic private value')
        inputs = self.repo / 'inputs'
        inputs.mkdir()
        (inputs / 'safe.bin').symlink_to(self.repo / '.env')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'value.txt').write_text('outside')
        (inputs / 'escape').symlink_to(outside, target_is_directory=True)
        env = dict(os.environ, HEAVYLANE_HOSTS_FILE=str(self.hosts), HEAVYLANE_PROJECTS_DIR=str(self.projects))
        for data in ['.env', 'inputs/safe.bin', 'inputs/escape', 'inputs', str(inputs)]:
            write_json(self.repo / '.heavylane.json', {'data': [data]})
            r = subprocess.run([sys.executable, str(ROOT / 'bin/heavylane'), 'submit', '--dry-run', '--', 'echo', 'hi'],
                               cwd=self.repo, env=env, text=True, capture_output=True)
            with self.subTest(data=data):
                self.assertEqual(r.returncode, 78, r.stderr)
                self.assertIn('refusing' if not os.path.isabs(data) else 'relative', r.stderr)

    def test_snapshot_uncommitted_excludes_and_deleted_files(self):
        files = {'code.py': 'old', 'gone.py': 'gone', '.env': 'private', '.envrc': 'private',
                 'secrets/x': 'private', 'Config/Credentials.JSON': 'private', 'data/input.csv': 'data',
                 'trim/large.txt': 'trim', '.gitignore': 'ignored.txt\n', 'ignored.txt': 'ignored'}
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        (self.repo / 'code.py').write_text('changed')
        (self.repo / 'gone.py').unlink()
        (self.repo / 'new.txt').write_text('untracked')
        out = self.base / 'snapshot.tgz'
        meta = cli.build_snapshot(str(self.repo), {'workdir': '', 'project': 'example-sim',
                                                  'data': ['data'], 'exclude': ['trim']}, str(out))
        with tarfile.open(out) as tar:
            self.assertEqual(set(tar.getnames()), {'src/.gitignore', 'src/code.py', 'src/new.txt', 'meta/snapshot.json'})
            self.assertEqual(tar.extractfile('src/code.py').read(), b'changed')
        self.assertEqual(meta['files'], 3)

    def test_snapshot_refuses_followed_ancestor_escape(self):
        inputs = self.repo / 'inputs'
        inputs.mkdir()
        tracked = inputs / 'token.txt'
        tracked.write_text('tracked')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'inputs/token.txt'], check=True)
        tracked.unlink()
        inputs.rmdir()
        outside = self.base / 'private'
        outside.mkdir()
        (outside / 'token.txt').write_text('synthetic private value')
        inputs.symlink_to(outside, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failed:
            cli.build_snapshot(str(self.repo), {'workdir': '', 'project': 'example-sim'}, str(self.base/'a.tgz'))
        self.assertEqual(failed.exception.code, 78)


class RuntimeBoundaries(unittest.TestCase):
    def test_denied_case_and_path_components(self):
        for path in ['.ENV', '.envrc', 'secrets/x', 'Config/Credentials.JSON', 'x/.SSH/key', 'x/.venv/code.py']:
            with self.subTest(path=path):
                self.assertIsNotNone(cli.denied(path))
        self.assertIsNone(cli.denied('config/settings.json'))
        self.assertEqual(cli.denied('outputs/a', ['outputs']), 'config-exclude')

    def test_exit_codes_and_signals(self):
        for state, code in [('done', 0), ('busy', 75), ('cancelled', 130), ('killed_mem', 137), ('error', 70)]:
            self.assertEqual(cli.exit_code_for({'state': state}), code)
        self.assertEqual(cli.exit_code_for({'state': 'failed', 'exit_code': -9}), 137)
        self.assertEqual(cli.exit_code_for({'state': 'failed', 'exit_code': -15}), 143)
        self.assertEqual(cli.exit_code_for({'state': 'failed', 'exit_code': 7}), 7)

    def test_path_drops_empty_components_in_python_and_remote_shell(self):
        for builder in (cli.build_path, host.build_path):
            self.assertEqual(builder('', ':/opt/tools::', '/usr/bin:/bin:'), '/opt/tools:/usr/bin:/bin')
        rm = cli.Remote({'name': 'buildbox', 'path_prefix': ":/opt/tool's path::"})
        r = subprocess.run(['/bin/bash', '-c', rm.shell_command('printf "%s" "$PATH"')],
                           env={'PATH': ':/usr/bin::/bin:'}, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "/opt/tool's path:/usr/bin:/bin")

    def test_wake_order_and_dedup(self):
        rm = cli.Remote({'name': 'buildbox', 'tailscale_ip': '100.64.1.2',
                         'wake_lan_ip': '192.168.1.51', 'wake_lan_host': 'buildbox.local'})
        peers = {'Peer': {'a': {'TailscaleIPs': ['100.64.1.2'], 'CurAddr': '192.168.1.50:123'},
                          'b': {'TailscaleIPs': ['100.64.1.2'], 'CurAddr': '192.168.1.50:456'}}}
        with mock.patch.object(cli.shutil, 'which', return_value='/example/tailscale'), \
                mock.patch.object(cli, 'run', return_value=mock.Mock(stdout=json.dumps(peers))), \
                mock.patch.object(rm, '_cached_lan_ip', return_value='192.168.1.50'):
            self.assertEqual(rm.wake_targets(), ['192.168.1.50', '192.168.1.51', 'buildbox.local'])
        with mock.patch.object(cli.shutil, 'which', return_value=None), \
                mock.patch.object(rm, '_cached_lan_ip', return_value='192.168.1.52'):
            self.assertEqual(rm.wake_targets(), ['192.168.1.52', '192.168.1.51', 'buildbox.local'])

    def test_missing_program_is_readable_config_error(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PATH=temp)
            r = subprocess.run([sys.executable, str(ROOT / 'bin/heavylane'), 'snapshot', '-o', str(Path(temp) / 'a.tgz')],
                               env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 78)
        self.assertIn('git', r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_root_and_python_with_spaces_through_real_shell(self):
        with tempfile.TemporaryDirectory(prefix='heavylane space ') as temp:
            base = Path(temp)
            py = base / "python's link"
            py.symlink_to(sys.executable)
            rm = cli.Remote({'name': 'buildbox', 'root': "job's root", 'python': str(py)})
            rm.home = temp
            helper = Path(rm.root) / 'bin/rr_job.py'
            helper.parent.mkdir(parents=True)
            helper.write_bytes((ROOT / 'lib/rr_job.py').read_bytes())
            r = subprocess.run(['/bin/bash', '-c', rm.tool('--version')], text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), cli.__version__)
            jobdir = rm.jobdir('heavylane-example-123')
            parsed = shlex.split(rm.tool('status', jobdir))
            self.assertEqual(parsed[1:], [str(helper), 'status', jobdir])

    def test_external_guard_protects_dotfiles_and_symlink_targets_before_rsync(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'home'
            home.mkdir()
            jobdir = str(Path(temp) / 'job')
            (home / '.zshrc').write_text('keep')
            safe = home / 'inputs'
            safe.mkdir()
            (safe / 'alias').symlink_to(home / '.zshrc')
            with mock.patch.object(host.os.path, 'expanduser', return_value=str(home)), \
                    mock.patch.object(host.subprocess, 'run') as transfer:
                for path in [home / '.zshrc', home / '.gitconfig', home / 'notes_history', home / '.envrc',
                             home / 'secrets/x', home / 'Config/Credentials.JSON', safe / 'alias']:
                    with self.subTest(path=path), self.assertRaises(RuntimeError):
                        host.apply_external(jobdir, [str(path)])
                staged = Path(jobdir) / 'external' / str(safe).lstrip('/')
                staged.mkdir(parents=True)
                (staged / '.bashrc').write_text('bad')
                with self.assertRaises(RuntimeError):
                    host.apply_external(jobdir, [str(safe)])
                transfer.assert_not_called()
            self.assertEqual((home / '.zshrc').read_text(), 'keep')

    def test_external_followed_targets_checked_before_real_rsync(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            inputs = home / 'inputs'
            inputs.mkdir()
            key = home / '.ssh/id_ed25519'
            key.parent.mkdir()
            key.write_text('synthetic credential')
            (inputs / 'reference.bin').symlink_to(key)
            with mock.patch.object(cli, 'HOME', str(home)), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as failed:
                    cli.check_external(str(inputs))
                self.assertEqual(failed.exception.code, 78)
            (inputs / 'reference.bin').unlink()
            data = home / 'reference.csv'
            data.write_text('valid data')
            (inputs / 'reference.bin').symlink_to(data)
            with mock.patch.object(cli, 'HOME', str(home)):
                cli.check_external(str(inputs))
            dest = home / 'staged'
            dest.mkdir()
            excludes = [arg for pat in sorted(cli.DENY_DIRS) + cli.DENY_GLOBS for arg in ('--exclude', pat)]
            subprocess.run(['rsync', '-aL'] + excludes + [str(inputs) + '/', str(dest) + '/'], check=True)
            self.assertEqual((dest / 'reference.bin').read_text(), 'valid data')

    def test_rsync_excludes_mixed_case_denied_input_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            inputs=root/'inputs'
            inputs.mkdir()
            for name in ['.env','.ENV','.ENVRC','Config/Credentials.JSON','SeCrEtS/value','.VENV/code.py','safe.csv']:
                p=inputs/name
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text('synthetic '+name)
            cli.check_data(str(inputs),str(root))
            dest=root/'staged'
            dest.mkdir()
            excludes=(cli.rsync_excludes() if hasattr(cli,'rsync_excludes') else
                      [arg for pat in sorted(cli.DENY_DIRS)+cli.DENY_GLOBS for arg in ('--exclude',pat)])
            cli.Remote({'name':'buildbox'}).rsync(str(inputs)+'/',str(dest)+'/',['--delete']+excludes)
            shipped=sorted(p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file())
            self.assertEqual(shipped,['safe.csv'])

    def test_external_protected_case_alias_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp).resolve()
            config=home/'.config'
            config.mkdir()
            (config/'settings.json').write_text('synthetic private config')
            # APFS resolves the uppercase alias; Linux uses an explicit uppercase directory.
            alias=home/'.CONFIG'
            if not alias.exists():
                alias.mkdir()
                (alias/'settings.json').write_text('synthetic private config')
            inputs=home/'inputs'
            inputs.mkdir()
            (inputs/'reference.bin').symlink_to(alias/'settings.json')
            with mock.patch.object(cli,'HOME',str(home)), \
                    mock.patch.object(cli,'DENY_EXTERNAL',[str(config)]), \
                    contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as failed:
                cli.check_external(str(inputs))
            self.assertEqual(failed.exception.code,78)

    def test_version_mismatch_reinstalls_once(self):
        rm = cli.Remote({'name': 'buildbox'})
        rm.home = '/example'
        responses = [mock.Mock(stdout='0.0.0'), mock.Mock(stdout='oldhash'),
                     mock.Mock(stdout=''), mock.Mock(stdout=cli.__version__)]
        with mock.patch.object(rm, 'sh', side_effect=responses) as remote, \
                contextlib.redirect_stderr(io.StringIO()) as messages:
            rm.install_tool()
        self.assertEqual(sum('cat >' in call.args[0] for call in remote.call_args_list), 1)
        self.assertIn('reinstalling once', messages.getvalue())

    def test_telemetry_failure_stops_real_child_and_records_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobdir = root/'jobs/example'
            (jobdir/'src').mkdir(parents=True)
            write_json(jobdir/'job.json', {'remote_root':str(root), 'job_id':'example',
                       'command':shlex.quote(sys.executable)+' -c '+shlex.quote('import time; time.sleep(30)'),
                       'mem_limit_gb':0.5, 'grace_seconds':0})
            real_run = subprocess.run
            def fail_ps(args, **kw):
                if args[0] == 'ps':
                    return subprocess.CompletedProcess(args, 1, '', 'synthetic ps failure')
                return real_run(args, **kw)
            old_signals = {s: host.signal.getsignal(s) for s in (host.signal.SIGTERM,host.signal.SIGINT,host.signal.SIGHUP)}
            try:
                with mock.patch.object(host.subprocess,'run',side_effect=fail_ps), \
                        mock.patch.object(host.shutil,'which',return_value=None), \
                        mock.patch.object(host,'host_memory',return_value={'swap_used_gb':0,'free_pct':90}), \
                        self.assertRaisesRegex(RuntimeError,'cannot enumerate process tree'):
                    host.cmd_run(str(jobdir), False)
            finally:
                for sig, handler in old_signals.items():
                    host.signal.signal(sig,handler)
            st = json.loads((jobdir/'status.json').read_text())
            self.assertEqual((st['state'],st['exit_code'],st['version']),('error',70,host.__version__))
            self.assertFalse(host.alive(st['child_pid']))
            self.assertIn('synthetic ps failure', st['error'])
        with mock.patch.object(host.subprocess,'run',side_effect=FileNotFoundError('missing ps')):
            with self.assertRaisesRegex(RuntimeError,'cannot enumerate'):
                host.ps_table()

    def test_terminal_submit_returns_status_exit(self):
        # Exercise the submission boundary with an isolated transport, no remote commands.
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(['git','init','-q',str(repo)],check=True)
            cfg={'project':'example','workdir':'','_config_path':None}
            rm=mock.Mock(home='/example',root='/example/heavylane',alias='buildbox',py='python3')
            rm.hostinfo.return_value={'busy':False}
            rm.jobdir.return_value='/example/job'
            rm.ssh_argv.return_value=['transport']
            a=mock.Mock(root=str(repo),config=None,trust_repo_config=False,host=None,no_config_inputs=False,
                        data=None,external=None,command=['echo','hi'],setup='',env=None,git=False,snapshot=None,
                        dry_run=False,queue=False,label=None,mem_limit_gb=None,wait=False)
            for state, rc, expected in [('killed_mem',-9,137),('cancelled',None,130),('lost',None,70),('failed',7,7),('done',0,0)]:
                rm.status.return_value={'state':state,'exit_code':rc}
                with mock.patch.object(cli,'load_project',return_value=dict(cfg)), \
                        mock.patch.object(cli,'repo_root',return_value=str(repo)), \
                        mock.patch.object(cli,'host_config',return_value={}), \
                        mock.patch.object(cli,'Remote',return_value=mock.Mock(reach=mock.Mock(return_value=rm))), \
                        mock.patch.object(cli,'build_snapshot',side_effect=lambda root,cfg,out: (Path(out).write_bytes(b'x') and
                            {'snapshot_sha256':'a'*64,'files':0,'bytes':0,'git_head':None,'git_status_porcelain':[],'excluded':[]})), \
                        mock.patch.object(cli.subprocess,'run',return_value=mock.Mock(returncode=0)), \
                        contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_submit(a),expected,state)


if __name__ == '__main__':
    unittest.main()
