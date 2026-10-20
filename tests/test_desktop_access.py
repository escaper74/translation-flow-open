from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from translation_flow.codex_runtime import inherit_windows_proxy, resolve_codex_executable
from translation_flow import desktop_access as access


class DesktopAccessTests(unittest.TestCase):
    def test_existing_system_proxy_is_only_copied_into_child_environment(self):
        parent = {'Path': 'test'}
        with patch('translation_flow.codex_runtime.urllib.request.getproxies_registry', create=True,
                   return_value={'https': 'http://127.0.0.1:1234', 'no': 'localhost'}):
            child = inherit_windows_proxy(parent)
        self.assertEqual('http://127.0.0.1:1234', child['HTTPS_PROXY'])
        self.assertNotIn('HTTPS_PROXY', parent)

    def test_explicit_proxy_environment_is_not_overridden(self):
        parent = {'https_proxy': 'http://chosen.invalid:1234'}
        with patch('translation_flow.codex_runtime.urllib.request.getproxies_registry', create=True) as registry:
            self.assertEqual(parent, inherit_windows_proxy(parent))
        registry.assert_not_called()

    def test_path_preferred_over_desktop_fallback(self):
        with patch('translation_flow.codex_runtime.shutil.which', return_value='configured/codex.exe'):
            self.assertEqual('configured/codex.exe', resolve_codex_executable())

    def test_explorer_without_cli_path_finds_latest_desktop_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'OpenAI' / 'Codex' / 'bin'
            old = root / 'old-release' / 'codex.exe'
            new = root / 'new-release' / 'codex.exe'
            for executable, timestamp in ((old, 100), (new, 200)):
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b'not executed')
                os.utime(executable, (timestamp, timestamp))
            with patch.dict(os.environ, {'LOCALAPPDATA': temp}), patch('translation_flow.codex_runtime.shutil.which', return_value=None):
                self.assertEqual(str(new), resolve_codex_executable())

    def test_missing_cli_is_not_a_login_or_network_diagnosis(self):
        from translation_flow.translation.codex_provider import codex_status
        runner = Mock()
        with patch('translation_flow.translation.codex_provider.resolve_codex_executable', return_value=None):
            result = codex_status(process_runner=runner)
        self.assertEqual('cli_not_found', result['reason'])
        runner.assert_not_called()

    def test_login_uses_official_browser_flow_without_leaking_output(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, b'private callback URL', b'private token'))
        with patch.object(access, 'resolve_codex_executable', return_value='codex.exe'), patch.object(access.subprocess, 'run', run), patch.object(access, 'codex_status', return_value={'status': 'available', 'message': 'ready'}):
            result = access.login_codex()
        self.assertEqual(['codex.exe', 'login'], run.call_args.args[0])
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertNotIn('private', json.dumps(result))

    def test_missing_tools_prevent_any_service_request(self):
        with patch.object(access, 'inspect_environment', return_value={'checks': [{'name': 'whisper', 'present': False}]}), patch.object(access, 'create_api_provider') as factory, patch.object(access, 'CodexProvider') as codex:
            result = access.check_readiness(mode='video', provider='codex', probe=True)
        self.assertEqual('blocked', result['status'])
        factory.assert_not_called()
        codex.assert_not_called()

    def test_repackage_requires_no_login_or_provider(self):
        with patch.object(access, 'inspect_environment', return_value={'checks': []}), patch.object(access, 'codex_status') as status, patch.object(access, 'CodexProvider') as factory:
            result = access.check_readiness(mode='repackage', provider='codex', probe=True)
        self.assertEqual('ready', result['status'])
        status.assert_not_called()
        factory.assert_not_called()

    def test_local_check_is_explicitly_not_a_live_service_test(self):
        with patch.object(access, 'inspect_environment', return_value={'checks': []}), patch.object(access, 'codex_status', return_value={'status': 'available'}), patch.object(access, 'CodexProvider') as factory:
            result = access.check_readiness(mode='srt', provider='codex')
        self.assertFalse(result['service_tested'])
        factory.assert_not_called()

    def test_codex_probe_sends_only_fixed_public_text(self):
        factory = Mock()
        factory.return_value.chat.return_value = 'OK'
        with patch.object(access, 'inspect_environment', return_value={'checks': []}), patch.object(access, 'codex_status', return_value={'status': 'available'}), patch.object(access, 'CodexProvider', factory):
            result = access.check_readiness(mode='srt', provider='codex', probe=True)
        self.assertTrue(result['service_tested'])
        self.assertEqual('system', factory.return_value.chat.call_args.args[0][0]['role'])
        self.assertEqual('Connection test only. Reply with OK.', factory.return_value.chat.call_args.args[0][0]['content'])

    def test_bad_key_stops_before_task_and_is_not_echoed(self):
        factory = Mock()
        factory.return_value.check_connection.side_effect = RuntimeError('invalid secret-test-key')
        with patch.object(access, 'inspect_environment', return_value={'checks': []}), patch.object(access, 'create_api_provider', factory):
            result = access.check_readiness(mode='srt', provider='qwen', api_key='secret-test-key', model='test', probe=True)
        self.assertEqual('blocked', result['status'])
        self.assertNotIn('secret-test-key', json.dumps(result))
        self.assertFalse(result['service_tested'])

    def test_access_cli_removes_key_before_probe(self):
        def inspect(**kwargs):
            self.assertNotIn('SUBTITLE_API_KEY', os.environ)
            self.assertEqual('test-key', kwargs['api_key'])
            return {'status': 'ready'}
        with patch.dict(os.environ, {'SUBTITLE_API_KEY': 'test-key'}), patch.object(access, 'check_readiness', side_effect=inspect), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(0, access.main(['--check', '--provider', 'qwen']))


if __name__ == '__main__':
    unittest.main()
