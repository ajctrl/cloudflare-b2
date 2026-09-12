import json
import io
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deploy


class AuthenticationTests(unittest.TestCase):
    identity = {'loggedIn': True, 'accounts': [{'id': 'a' * 32, 'name': 'Test'}]}

    def result(self, data, code=0):
        return subprocess.CompletedProcess(['whoami'], code, json.dumps(data))

    def test_existing_login_is_reused_without_device_login(self):
        with patch.object(deploy.subprocess, 'run', return_value=self.result(self.identity)) as cli:
            self.assertEqual(deploy.authenticated_identity(), self.identity)
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(cli.call_args.args[0][-2:], ['whoami', '--json'])

    def test_logged_out_authenticates_once_and_rechecks(self):
        with patch.object(deploy.subprocess, 'run', side_effect=[
            self.result({'loggedIn': False}, 1),
            self.result({}),
            self.result(self.identity),
        ]) as cli:
            self.assertEqual(deploy.authenticated_identity(), self.identity)
        self.assertEqual(cli.call_count, 3)
        self.assertEqual(cli.call_args_list[1].args[0][-3:], ['login', '--device', '--browser=false'])

    def test_network_or_invalid_response_does_not_trigger_login(self):
        for response in [self.result({'error': 'network failure'}, 1),
                         subprocess.CompletedProcess(['whoami'], 1, '')]:
            with self.subTest(response=response), patch.object(deploy.subprocess, 'run', return_value=response) as cli:
                with self.assertRaises((SystemExit, subprocess.CalledProcessError)):
                    deploy.authenticated_identity()
                self.assertEqual(cli.call_count, 1)

    def test_still_logged_out_after_login_stops(self):
        with patch.object(deploy, 'get_identity', return_value=None), patch.object(deploy, 'run') as login:
            with self.assertRaises(SystemExit):
                deploy.authenticated_identity()
            login.assert_called_once_with('login', '--device', '--browser=false')


class ProxyTokenTests(unittest.TestCase):
    def exercise(self, choice='1', dry_run=False, fail=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'name': 'test', 'account_id': 'a' * 32, 'vars': {}}))
            stack.enter_context(patch.object(deploy, 'CONFIG', config))
            stack.enter_context(patch.object(deploy.os, 'isatty', return_value=True))
            stack.enter_context(patch.object(deploy, 'authenticated_identity', return_value={}))
            stack.enter_context(patch.object(deploy, 'select_account', return_value='a' * 32))
            # Exercise the actual menu validation and Enter default.
            stack.enter_context(patch('builtins.input', side_effect=[
                'test-worker', 'test-bucket', 'test-key-id', 'false', 'false', *choice,
            ]))
            stack.enter_context(patch.object(deploy, 'select_endpoint', return_value='s3.us-west-004.backblazeb2.com'))
            stack.enter_context(patch('sys.argv', ['deploy.py', *(['--dry-run'] if dry_run else [])]))
            secret = stack.enter_context(patch.object(deploy, 'secret', side_effect=['b2-key', 'manual-token']))
            generator = stack.enter_context(patch.object(deploy.secure_secrets, 'token_urlsafe', return_value='generated-token'))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            uploaded = {}

            def run(*args, **kwargs):
                if '--secrets-file' in args:
                    uploaded.update(json.loads(Path(args[-1]).read_text()))
                    self.assertNotIn('generated-token', output.getvalue())
                if fail:
                    raise subprocess.CalledProcessError(1, 'deploy')
                return '  https://test-worker.example.workers.dev\n'

            stack.enter_context(patch.object(deploy, 'run', side_effect=run))
            if fail:
                with self.assertRaises(subprocess.CalledProcessError):
                    deploy.main()
            else:
                deploy.main()
            self.assertNotIn('PROXY_TOKEN', config.read_text())
            return uploaded, output.getvalue(), secret.call_count, generator.call_args_list

    def test_generated_token_is_uploaded_and_displayed_after_success(self):
        uploaded, output, prompts, calls = self.exercise(choice=[''])
        self.assertEqual(uploaded['PROXY_TOKEN'], 'generated-token')
        self.assertIn('PROXY_TOKEN: generated-token', output)
        self.assertEqual(prompts, 1)
        self.assertEqual(calls[0].args, (32,))

    def test_manual_token_is_in_config_after_success(self):
        uploaded, output, prompts, calls = self.exercise(choice=['invalid', '2'])
        self.assertEqual(uploaded['PROXY_TOKEN'], 'manual-token')
        self.assertIn('headers = x-proxy-token,manual-token', output)
        self.assertIn('[b2proxy]\ntype = http\nurl = https://test-worker.example.workers.dev/\n', output)
        self.assertEqual(prompts, 2)
        self.assertFalse(calls)

    def test_dry_run_does_not_generate_token(self):
        uploaded, output, prompts, calls = self.exercise(choice=[], dry_run=True)
        self.assertFalse(uploaded or prompts or calls)
        self.assertNotIn('generated-token', output)
        self.assertNotIn('[b2proxy]', output)

    def test_failed_deploy_does_not_display_token(self):
        _, output, _, _ = self.exercise(fail=True)
        self.assertNotIn('generated-token', output)
        self.assertNotIn('[b2proxy]', output)


class RcloneConfigTests(unittest.TestCase):
    def test_token_csv_and_path_placeholder(self):
        output = io.StringIO()
        with redirect_stdout(output):
            deploy.print_rclone_config('  https://test.example.workers.dev\n',
                                      {'BUCKET_NAME': '$path', 'ALLOW_LIST_BUCKET': 'true'}, 'a,b"c')
        self.assertIn('url = https://test.example.workers.dev/YOUR_BUCKET_NAME/', output.getvalue())
        self.assertIn('headers = x-proxy-token,"a,b""c"', output.getvalue())

    def test_missing_url_uses_explicit_placeholder(self):
        output = io.StringIO()
        with redirect_stdout(output):
            deploy.print_rclone_config('', {'BUCKET_NAME': 'bucket'}, 'token')
        self.assertIn('url = https://YOUR_WORKER_HOST/', output.getvalue())
        self.assertIn('Replace the uppercase URL placeholders', output.getvalue())


if __name__ == '__main__':
    unittest.main()
