"""B131: real signals and disposable test trees; no host services or credentials."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def living(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='],
                            capture_output=True, text=True, timeout=2)
    if result.returncode not in (0, 1):
        raise RuntimeError('process query failed: ' + result.stderr)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


class InterruptionTests(unittest.TestCase):
    def setUp(self):
        # Process snapshots are a required dependency, never an assertion about
        # B131. Skip before spawning fixtures if the sandbox denies them.
        try:
            subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,stat=,lstart='],
                           capture_output=True, text=True, timeout=2, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest('process snapshots unavailable: ' + str(exc))
        self.temp = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']).resolve())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'framework'
        # Minimal framework copy: keep the actual runners and shared reaper unchanged.
        shutil.copytree(ROOT / 'bin/frclyo', self.root / 'bin/frclyo')
        (self.root / 'tests/host').mkdir(parents=True)
        for relative in ('tests/run_all.py', 'tests/host/verify_all.py'):
            shutil.copyfile(ROOT / relative, self.root / relative)
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                        FRCLYO_HOME=str(self.base / 'data'), FRCLYO_LAUNCHD_DRY_RUN='1')
        self.env['TMPDIR'] = str(self.base)
        self.env.pop('FRCLYO_CONFIG', None)
        self.env.pop('CODEX_HOME', None)
        self.env.pop('FRCLYO_E2E', None)
        self.procs = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        # Baseline deliberately leaks: the harness, not the tested runner, reaps it.
        for item in self.base.glob('*.pid'):
            pid = int(item.read_text())
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def fake_suite(self, directory, code):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'test_fixture.py').write_text(
            'import unittest\nclass Fixture(unittest.TestCase):\n'
            '    def test_fixture(self):\n' +
            ''.join('        ' + line + '\n' for line in code.splitlines()))

    def fixtures(self):
        # Three generations, two detached sessions; the leaf ignores TERM and INT.
        leaf = self.base / 'leaf.py'
        leaf.write_text('import os, signal, time\nfrom pathlib import Path\n'
                        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                        'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                        f'Path({str(self.base / "leaf.pid")!r}).write_text(str(os.getpid()))\n'
                        'time.sleep(120)\n')
        branch = self.base / 'branch.py'
        branch.write_text('import os, subprocess, sys, time\nfrom pathlib import Path\n'
                          f'Path({str(self.base / "branch.pid")!r}).write_text(str(os.getpid()))\n'
                          f'subprocess.Popen([sys.executable, "-B", {str(leaf)!r}], start_new_session=True)\n'
                          'time.sleep(120)\n')
        slow = ('import os, subprocess, sys, time\nfrom pathlib import Path\n'
                f'Path({str(self.base / "suite.pid")!r}).write_text(str(os.getpid()))\n'
                f'Path({str(self.base / "parent.pid")!r}).write_text(str(os.getppid()))\n'
                f'subprocess.Popen([sys.executable, "-B", {str(branch)!r}], start_new_session=True)\n'
                'time.sleep(120)')
        self.fake_suite(self.root / 'bin/tests', "print('COMPLETED_FIXTURE')")
        self.fake_suite(self.root / 'board', slow)
        self.fake_suite(self.root / 'tests',
                        f'from pathlib import Path\nPath({str(self.base / "should-not-run")!r}).touch()')
        return slow

    def launch(self, command):
        output = self.base / ('runner-%d.log' % len(self.procs))
        with output.open('wb') as stream:
            proc = subprocess.Popen(command, cwd=self.root, env=self.env,
                                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(proc)
        return proc, output

    def verify_command(self, jobs):
        wrapper = self.base / 'verify_fixture.py'
        wrapper.write_text('import sys\n'
                           f'sys.path.insert(0, {str(self.root / "tests/host")!r})\n'
                           'import verify_all as verify\n'
                           f'verify.plan = lambda: {jobs!r}\n'
                           f'raise SystemExit(verify.main(["--output", {str(self.base / "evidence")!r}]))\n')
        return [sys.executable, '-B', str(wrapper)]

    def check_signal(self, runner, sig, repeated=False):
        slow = self.fixtures()
        if runner == 'run_all':
            command = [sys.executable, '-B', str(self.root / 'tests/run_all.py')]
        else:
            pending = ([sys.executable, '-B', str(self.root / 'tests/run_all.py')]
                       if runner == 'nested' else [sys.executable, '-B', '-c', slow])
            command = self.verify_command([
                ('completed', [sys.executable, '-B', '-c', "print('COMPLETED_FIXTURE')"], 'exit'),
                ('pending', pending, 'exit'),
                ('not-started', [sys.executable, '-B', '-c',
                                 f'from pathlib import Path; Path({str(self.base / "should-not-run")!r}).touch()'], 'exit')])
        unrelated, _ = self.launch([sys.executable, '-B', '-c', 'import time; time.sleep(120)'])
        proc, log = self.launch(command)
        if not wait_until(lambda: (self.base / 'leaf.pid').exists() or proc.poll() is not None):
            raise RuntimeError('fixture never became ready: ' + log.read_text())
        if not (self.base / 'leaf.pid').exists():
            raise RuntimeError('fixture failed to start: ' + log.read_text())
        # Allow at least one steady-state descendant sample before interrupting.
        time.sleep(0.3)
        proc.send_signal(sig)
        if repeated:
            time.sleep(0.1)
            if proc.poll() is None:
                proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.fail('B131: runner did not stop after signal')
        pids = {p.name: int(p.read_text()) for p in self.base.glob('*.pid')}
        survivors = {name: pid for name, pid in pids.items() if living(pid)}
        output = log.read_text()
        evidence = self.base / 'evidence'
        if runner == 'run_all':
            paths = [line.split('证据目录：', 1)[1].strip() for line in output.splitlines()
                     if '证据目录：' in line]
            evidence = Path(paths[-1]) if paths else self.base / 'missing-evidence'
        problems = []
        if rc != 128 + sig:
            problems.append('exit=%s expected=%s' % (rc, 128 + sig))
        if survivors:
            problems.append('surviving descendants=' + repr(survivors))
        if not (evidence / 'summary.json').exists():
            problems.append('missing summary.json')
        else:
            rows = json.loads((evidence / 'summary.json').read_text())
            print('SUMMARY:', json.dumps(rows, ensure_ascii=False), flush=True)
            if rows[0]['status'] != '通过':
                problems.append('completed result was lost')
            if not any(row.get('interrupted') and row['status'] == '被中断' for row in rows):
                problems.append('missing interrupted result')
            if '被中断' not in (evidence / 'summary.txt').read_text():
                problems.append('missing interrupted summary text')
        if unrelated.poll() is not None:
            problems.append('unrelated sibling process was terminated')
        if runner == 'nested':
            archived = evidence / '01/evidence-00/summary.json'
            if not archived.exists():
                problems.append('nested runner summary not archived')
            else:
                inner = json.loads(archived.read_text())
                if inner[0]['status'] != '通过' or not any(row.get('interrupted') for row in inner):
                    problems.append('nested completed/interrupted results lost')
        if (self.base / 'should-not-run').exists():
            problems.append('started another check after interruption')
        print(f'{runner} {signal.Signals(sig).name}: rc={rc}; survivors={survivors}; evidence={evidence}', flush=True)
        self.assertFalse(problems, 'B131: ' + '; '.join(problems))

    def test_run_all_term(self):
        self.check_signal('run_all', signal.SIGTERM)

    def test_run_all_int(self):
        self.check_signal('run_all', signal.SIGINT)

    def test_verify_term(self):
        self.check_signal('verify', signal.SIGTERM)

    def test_verify_int(self):
        self.check_signal('verify', signal.SIGINT)

    def test_nested_term(self):
        self.check_signal('nested', signal.SIGTERM)

    def test_nested_int(self):
        self.check_signal('nested', signal.SIGINT)

    def test_repeated_signal(self):
        self.check_signal('nested', signal.SIGTERM, repeated=True)

    def test_run_all_normal(self):
        for group in ('bin/tests', 'board', 'tests'):
            self.fake_suite(self.root / group, "print('NORMAL_FIXTURE')")
        proc, log = self.launch([sys.executable, '-B', str(self.root / 'tests/run_all.py')])
        self.assertEqual(proc.wait(timeout=12), 0, log.read_text())
        self.assertEqual(log.read_text().count('NORMAL_FIXTURE'), 3)

    def test_run_all_failure_continues(self):
        self.fake_suite(self.root / 'bin/tests', 'self.fail("EXPECTED_FAILURE")')
        self.fake_suite(self.root / 'board', "print('AFTER_FAILURE')")
        self.fake_suite(self.root / 'tests', "print('AFTER_FAILURE')")
        proc, log = self.launch([sys.executable, '-B', str(self.root / 'tests/run_all.py')])
        self.assertEqual(proc.wait(timeout=12), 1, log.read_text())
        self.assertEqual(log.read_text().count('AFTER_FAILURE'), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
