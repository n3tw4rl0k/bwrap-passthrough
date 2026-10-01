"""Command dispatch contracts and actual HTTP/SQL/process integration tests."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import bwrap


def run_shim(options, command, **kwargs):
    return subprocess.run([sys.executable, bwrap.__file__, *options, '--', *command],
                          capture_output=True, text=True, timeout=20, **kwargs)


class CommandTests(unittest.TestCase):
    def test_arbitrary_command_dispatch(self):
        commands = [
            ['curl', 'google.com'],
            ['curl', '-H', 'X-Test: a b', '--', 'https://example.com'],
            ['sqlite3', ':memory:', "SELECT 'hello world', 6 * 7;"],
            ['psql', '-c', "SELECT 'a; b', 42;"],
            ['mysql', '--execute', 'SELECT 42;'],
            ['sql', 'SELECT * FROM example'],
            ['bash', '-lc', 'printf "%s\\n" "a b"'],
            ['git', 'status', '--porcelain'],
            ['uninstalled-custom-command', '', '-x', '--', 'ș', '$(id)'],
        ]
        for command in commands:
            with self.subTest(command=command):
                plan = bwrap.parse_cli(['--unshare-all', '--info-fd', '999999',
                                        '--block-fd', '999998', '--', *command], {})
                with patch.object(bwrap.os, 'name', 'posix'), \
                        patch.object(bwrap.os, 'getcwd', return_value='/work'), \
                        patch.object(bwrap.os, 'execvpe') as execute:
                    bwrap.execute(plan)
                execute.assert_called_once_with(command[0], command, {'PWD': '/work'})

    def test_upstream_option_coverage(self):
        spec = json.loads(Path(__file__).with_name('upstream-options.json').read_text())
        expected = spec['options']
        self.assertEqual(set(expected), set(bwrap.SCHEMA))
        for option, arity in expected.items():
            with self.subTest(option=option):
                self.assertEqual(len(bwrap.SCHEMA[option]), arity)

    def test_every_noop_combined_actually_runs(self):
        spec = json.loads(Path(__file__).with_name('upstream-options.json').read_text())
        options = []
        for option, arity in spec['options'].items():
            if option not in bwrap.IMPLEMENTED:
                options.extend([option, *(['no-op-operand'] * arity)])
        result = run_shim(options, [sys.executable, '-c', 'print("EXECUTED")'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'EXECUTED')

    def test_sql_really_executes(self):
        code = ("import sqlite3; db=sqlite3.connect(':memory:'); "
                "db.execute('CREATE TABLE t (v INTEGER)'); "
                "db.executemany('INSERT INTO t VALUES (?)', [(10,), (32,)]); "
                "print(db.execute('SELECT SUM(v) FROM t').fetchone()[0])")
        result = run_shim(['--unshare-all', '--ro-bind', '/missing', '/missing'],
                          [sys.executable, '-c', code])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '42')

    def test_stderr_and_failure_status_are_preserved(self):
        result = run_shim([], [sys.executable, '-c',
                              'import sys; print("failure", file=sys.stderr); sys.exit(23)'])
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stderr.strip(), 'failure')

    def test_missing_command_returns_127(self):
        result = run_shim([], ['bwrap-test-command-that-does-not-exist-194271'])
        self.assertEqual(result.returncode, 127)
        self.assertIn('command not found', result.stderr)

    def test_searches_path_for_executable(self):
        executable_name = Path(sys.executable).name
        result = run_shim(['--clearenv', '--setenv', 'PATH', str(Path(sys.executable).parent)],
                          [executable_name, '-c', 'print("PATH_OK")'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PATH_OK')

    def test_shell_metacharacters_remain_literal(self):
        with tempfile.TemporaryDirectory() as cwd:
            marker = str(Path(cwd) / 'must-not-exist')
            value = f'hello; echo BAD > "{marker}"'
            result = run_shim([], [sys.executable, '-c', 'import sys; print(sys.argv[1])', value])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), value)
            self.assertFalse(Path(marker).exists())

    def test_curl_really_executes_http_request(self):
        curl = shutil.which('curl')
        self.assertIsNotNone(curl, 'Install curl to run the HTTP integration test')
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'HTTP_REQUEST_EXECUTED'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/'
                result = run_shim(['--unshare-net', '--share-net'],
                                  [curl, '--silent', '--show-error', '--noproxy', '*',
                                   '--max-time', '10', url])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'HTTP_REQUEST_EXECUTED')
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_windows_argv0_and_exit_code(self):
        plan = bwrap.parse_cli(['--argv0', 'custom', '--', 'cmd', 'a b'], {})
        with patch.object(bwrap.os, 'name', 'nt'), \
                patch.object(bwrap.os, 'getcwd', return_value='C:/work'), \
                patch.object(bwrap.shutil, 'which', return_value='C:/cmd.exe'), \
                patch.object(bwrap.subprocess, 'call', return_value=19) as call:
            self.assertEqual(bwrap.execute(plan), 19)
        self.assertEqual(call.call_args.args[0], ['custom', 'a b'])
        self.assertEqual(call.call_args.kwargs['executable'], 'C:/cmd.exe')

    def test_huge_args_fd_is_a_clean_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(bwrap.main(['--args', '9' * 5000, 'cmd']), 2)
        self.assertIn('out of range', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
