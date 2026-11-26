import contextlib
import io
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import bebotrestart as client
from scripts import self_hosted_restart as restart


class RestartClientTests(unittest.TestCase):
    def test_existing_ssh_settings_are_reused_without_evaluating_alias(self):
        route, root = client.deployment_route('ssh -t -p 2222 -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=yes user@example.test "~/broedenbot/deploy-from-git.sh"')
        self.assertEqual(root, '~/broedenbot')
        self.assertEqual(route[-1], 'user@example.test')
        command = client.restart_command(route, root, plan=True, timeout=90)
        self.assertEqual(command[:-1], route)
        remote = shlex.split(command[-1])
        self.assertEqual(remote[:2], ['python3', '-c'])
        self.assertIn("'--plan'", remote[2])
        self.assertIn("'--timeout', '90'", remote[2])
        self.assertNotIn('git pull', remote[2])
        compile(remote[2], '<remote-command>', 'exec')

    def test_remote_paths_are_literal_not_shell_interpolation(self):
        root = '~/a folder;$(touch SHOULD_NOT_RUN)'
        command = client.restart_command(['ssh', '-t', 'user@example.test'], root)
        launcher = shlex.split(command[-1])[2]
        captured = {}
        def inspect_program(code, namespace):
            captured.update(namespace)
        with patch.object(sys, 'argv', ['test']):
            exec(launcher, {'exec': inspect_program})
            self.assertIn(root, sys.argv)
        self.assertTrue(captured['__file__'].endswith('/source/scripts/self_hosted_restart.py'))
        self.assertIn('$(touch SHOULD_NOT_RUN)', captured['__file__'])

    def test_unknown_or_compound_alias_is_rejected(self):
        for alias in ['', 'echo unsafe', 'ssh user@host "~/other-script.sh"',
                      'ssh user@host && "~/broedenbot/deploy-from-git.sh"']:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                client.deployment_route(alias)

    def test_shortcut_installer_is_idempotent_and_forwards_help(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / '.zshrc'
            profile.write_text('alias existing_command="echo existing"\n')
            import os
            env = dict(os.environ, ZDOTDIR=folder)
            for _ in range(2):
                subprocess.run(['zsh', str(repo / 'scripts/install-terminal-shortcuts.zsh')],
                               env=env, check=True, capture_output=True, text=True)
            self.assertEqual(profile.read_text().count('terminal-shortcuts.zsh'), 1)
            self.assertIn('existing_command', profile.read_text())
            result = subprocess.run(['zsh', '-ic', 'bebotrestart --help'], env=env,
                                    check=True, capture_output=True, text=True)
            self.assertIn('--plan', result.stdout)
            self.assertIn('--timeout', result.stdout)


class SelfHostedRestartTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        (self.root / 'state').mkdir()
        for name in restart.deployment.COMPOSE_FILES:
            (self.root / name).write_text('{}')
        (self.root / '.env').write_text('COMPOSE_FILE=keep\n')
        (self.root / 'service.env').write_text('AI_DAILY_BUDGET_USD=2\n')
        (self.root / 'SOURCE-COMMIT').write_text('a' * 40 + '\n')
        for name in restart.deployment.DATABASES:
            with sqlite3.connect(self.root / 'state' / name) as db:
                db.execute('CREATE TABLE keep(value TEXT)')
                db.execute("INSERT INTO keep VALUES ('existing member data')")
        self.config = {'services': {'app': {
            'image': 'broedenbot:deployed', 'environment': {'BROEDEN_SEED_MODE': 'false'},
            'volumes': [{'type': 'bind', 'source': str(self.root / 'state'), 'target': '/data'}],
            'ports': [{'host_ip': '127.0.0.1'}], 'restart': 'unless-stopped',
        }}}
        self.commands = []
        self.image = 'sha256:' + 'b' * 64
        def capture(args):
            if 'config' in args:
                return json.dumps(self.config)
            if 'ps' in args:
                return 'current-container'
            if '{{.Image}}' in args or '{{.Id}}' in args:
                return self.image
            raise AssertionError(args)
        self.capture = patch.object(restart.deployment, 'capture', side_effect=capture)
        self.command = patch.object(restart.deployment, 'command', side_effect=lambda args: self.commands.append(args))
        self.ready = patch.object(restart.deployment, 'wait_ready')
        for context in [self.capture, self.command, self.ready]:
            mock = context.start()
            self.addCleanup(context.stop)
            if context is self.ready:
                self.wait_ready = mock
        self.args = SimpleNamespace(root=self.root, plan=False, timeout=90)

    def test_restart_recreates_only_current_app_and_verifies_without_changing_data_or_release(self):
        metadata_before = {name: (self.root / name).read_bytes() for name in ['.env', 'service.env', 'SOURCE-COMMIT']}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            restart.restart(self.args)
        operations = [command for command in self.commands if 'up' in command]
        self.assertEqual(len(operations), 1)
        args = operations[0]
        self.assertIn('--no-build', args)
        self.assertIn('--no-deps', args)
        self.assertEqual(args[args.index('--pull') + 1], 'never')
        self.assertIn('--force-recreate', args)
        self.assertEqual(args[-1], 'app')
        self.assertTrue(all('git' not in command and 'down' not in command for command in self.commands))
        self.wait_ready.assert_called_once()
        self.assertEqual(self.wait_ready.call_args.args, (self.root, self.image))
        self.assertEqual(self.wait_ready.call_args.kwargs['timeout'], 90)
        self.assertIn('BOT RESTART VERIFIED', output.getvalue())
        for name, data in metadata_before.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        for name in restart.deployment.DATABASES:
            with sqlite3.connect(self.root / 'state' / name) as db:
                self.assertEqual(db.execute('SELECT value FROM keep').fetchall(), [('existing member data',)])

    def test_plan_performs_no_restart(self):
        self.args.plan = True
        with contextlib.redirect_stdout(io.StringIO()) as output:
            restart.restart(self.args)
        self.assertEqual(self.commands, [['sudo', '-v']])
        self.wait_ready.assert_not_called()
        self.assertIn('BOT RESTART PLAN VERIFIED', output.getvalue())
        self.assertFalse((self.root / 'restarts').exists())

    def test_changed_image_and_seed_mode_are_rejected_before_restart(self):
        for case in ['image', 'seed']:
            with self.subTest(case=case):
                self.commands.clear()
                if case == 'image':
                    original = restart.deployment.capture
                    def different_image(args):
                        return 'sha256:other' if '{{.Id}}' in args else original(args)
                    context = patch.object(restart.deployment, 'capture', side_effect=different_image)
                else:
                    self.config['services']['app']['environment']['BROEDEN_SEED_MODE'] = 'true'
                    context = contextlib.nullcontext()
                with context, self.assertRaises(RuntimeError):
                    restart.restart(self.args)
                self.assertFalse(any('up' in command for command in self.commands))

    def test_an_active_event_window_prevents_restart(self):
        with patch.object(restart.deployment, 'check_drop_window', side_effect=RuntimeError('An Event Drop is accepting claims.')):
            with self.assertRaisesRegex(RuntimeError, 'accepting claims'):
                restart.restart(self.args)
        self.assertFalse(any('up' in command for command in self.commands))

    def test_shared_deployment_lock_prevents_overlapping_restart(self):
        import fcntl
        with (self.root / '.deploy.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                restart.restart(self.args)
        self.assertEqual(self.commands, [])

    def test_failed_health_check_does_not_claim_success(self):
        self.wait_ready.side_effect = RuntimeError('Dashboard did not become healthy')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'Private startup log'):
                restart.restart(self.args)
        self.assertNotIn('BOT RESTART VERIFIED', output.getvalue())


if __name__ == '__main__':
    unittest.main()
