"""Run with: python -m unittest -v test_bwrap"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bwrap


class ParserTests(unittest.TestCase):
    def parse(self, *args, env=None):
        return bwrap.parse_cli(args, {} if env is None else env)

    def test_original_wrapper(self):
        plan = self.parse(
            '--unshare-all', '--share-net', '--die-with-parent', '--new-session',
            '--proc', '/proc', '--dev', '/dev', '--ro-bind', '/usr', '/usr',
            '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
            '--ro-bind', '/bin', '/bin', '--ro-bind', '/etc/resolv.conf',
            '/etc/resolv.conf', '--dir', '/tmp', '--bind', '/work', '/work',
            '--chdir', '/work', '--clearenv', '--setenv', 'PATH',
            '/usr/local/bin:/usr/bin:/bin', '--', 'python3', 'a b.py', '')
        self.assertEqual(plan.command, ['python3', 'a b.py', ''])
        self.assertEqual(plan.cwd, '/work')
        self.assertEqual(plan.env, {'PATH': '/usr/local/bin:/usr/bin:/bin'})

    def test_all_command_arguments_are_literal(self):
        args = ['cmd', '--clearenv', '--', '-x', '', '*', 'a b', '$(id)', 'ș']
        self.assertEqual(self.parse('--', *args).command, args)

    def test_command_without_separator(self):
        self.assertEqual(self.parse('--unshare-all', 'cmd', '--bogus').command,
                         ['cmd', '--bogus'])

    def test_dash_and_empty_operands(self):
        plan = self.parse('--setenv', 'A', '--', '--setenv', 'B', '-x',
                          '--setenv', 'C', '', '--', 'cmd')
        self.assertEqual(plan.env, {'A': '--', 'B': '-x', 'C': ''})

    def test_environment_order(self):
        plan = self.parse('--setenv', 'A', '1', '--clearenv', '--setenv', 'B',
                          '2', '--unsetenv', 'B', '--setenv', 'A', '3', 'cmd',
                          env={'A': 'old', 'SECRET': 'x', 'PWD': '/work'})
        self.assertEqual(plan.env, {'PWD': '/work', 'A': '3'})

    def test_last_directory_and_argv0_win(self):
        plan = self.parse('--chdir', '/a', '--chdir', '/b', '--argv0', 'x',
                          '--argv0', '-new', 'cmd')
        self.assertEqual((plan.cwd, plan.argv0), ('/b', '-new'))

    def test_no_mutation_of_inherited_environment(self):
        env = {'A': '1'}
        self.parse('--unsetenv', 'A', 'cmd', env=env)
        self.assertEqual(env, {'A': '1'})

    def test_unknown_options_are_noops(self):
        for option in ['--unshare', '--future-option', '-h']:
            with self.subTest(option=option):
                self.assertEqual(self.parse(option, 'cmd').command, ['cmd'])

    def test_unknown_operands_with_explicit_separator(self):
        plan = self.parse('--future-option', 'value', '-another-value',
                          '--setenv', 'A', '--', '--', 'cmd', '--literal')
        self.assertEqual(plan.command, ['cmd', '--literal'])
        self.assertEqual(plan.env, {'A': '--'})

    def test_missing_operands(self):
        for args in [('--setenv', 'A'), ('--bind', '/a'), ('--chdir',)]:
            with self.subTest(args=args), self.assertRaises(bwrap.ShimError):
                self.parse(*args)

    def test_missing_command(self):
        for args in [(), ('--',), ('--unshare-all',), ('--', '')]:
            with self.subTest(args=args), self.assertRaises(bwrap.ShimError):
                self.parse(*args)

    def test_invalid_values(self):
        for args in [('--setenv', 'A=B', 'x'), ('--unsetenv', ''), ('--chdir', '')]:
            with self.subTest(args=args), self.assertRaises(bwrap.ShimError):
                self.parse(*args, 'cmd')

    def test_external_protocols_are_noops(self):
        for option in ['--lock-file', '--sync-fd', '--block-fd', '--userns-block-fd',
                       '--info-fd', '--json-status-fd']:
            value = '/a' if option == '--lock-file' else '3'
            with self.subTest(option=option):
                self.assertEqual(self.parse(option, value, 'cmd').command, ['cmd'])

    def test_ignored_operands_are_not_validated_or_opened(self):
        plan = self.parse('--info-fd', '9999999', '--uid', '-1', '--perms', 'xyz',
                          '--ro-bind', '/nonexistent', '', '--', 'cmd')
        self.assertEqual(plan.command, ['cmd'])

    def test_fd_mount_options_without_separator(self):
        plan = self.parse('--bind-fd', '3', '/work', '--ro-bind-fd', '4', '/data',
                          'cmd', '-x')
        self.assertEqual(plan.command, ['cmd', '-x'])

    def test_every_ignored_option_arity(self):
        samples = {'FD': '3', 'ID': '1000', 'BYTES': '100', 'OCTAL': '0700',
                   'CAP': 'ALL'}
        for option, operands in bwrap.SCHEMA.items():
            if option in bwrap.IMPLEMENTED:
                continue
            values = [samples.get(kind, '/sample') for kind in operands]
            with self.subTest(option=option):
                self.assertEqual(self.parse(option, *values, '--', 'cmd', '-x').command,
                                 ['cmd', '-x'])

    def test_args_fd_expansion_and_close(self):
        with tempfile.TemporaryFile() as stream:
            stream.write(b'--setenv\0A\0-x\0--setenv\0B\0\0')
            stream.seek(0)
            fd = os.dup(stream.fileno())
            plan = self.parse('--setenv', 'A', 'before', '--args', str(fd),
                              '--setenv', 'A', 'after', 'cmd')
            self.assertEqual(plan.env, {'A': 'after', 'B': ''})
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_args_fd_without_final_nul(self):
        with tempfile.TemporaryFile() as stream:
            stream.write(b'--hostname\0name')
            stream.seek(0)
            self.assertEqual(self.parse('--args', str(os.dup(stream.fileno())),
                                        'cmd').command, ['cmd'])

    def test_nested_args_and_commands_in_file_rejected(self):
        for data in [b'--args\0' + b'3\0', b'cmd\0', b'--\0cmd\0']:
            with tempfile.TemporaryFile() as stream:
                stream.write(data)
                stream.seek(0)
                with self.assertRaises(bwrap.ShimError):
                    self.parse('--args', str(os.dup(stream.fileno())), 'cmd')

    def test_help_and_version(self):
        for option in ['--help', '--version']:
            with self.subTest(option=option), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as result:
                    self.parse(option)
                self.assertEqual(result.exception.code, 0)
                self.assertIn('isolation', out.getvalue().lower())


class ExecutionTests(unittest.TestCase):
    def test_exec_uses_original_executable_and_custom_argv0(self):
        plan = bwrap.parse_cli(['--argv0', '-custom', '--', 'cmd', '', '-x'], {})
        with patch.object(bwrap.os, 'name', 'posix'), \
                patch.object(bwrap.os, 'getcwd', return_value='/work'), \
                patch.object(bwrap.os, 'execvpe') as execute:
            bwrap.execute(plan)
        execute.assert_called_once_with('cmd', ['-custom', '', '-x'], {'PWD': '/work'})

    def test_chdir_precedes_exec(self):
        plan = bwrap.parse_cli(['--chdir', '/work', '--', 'cmd'], {})
        calls = []
        with patch.object(bwrap.os, 'name', 'posix'), \
                patch.object(bwrap.os, 'getcwd', return_value='/work'), \
                patch.object(bwrap.os, 'chdir', side_effect=lambda p: calls.append(('cwd', p))), \
                patch.object(bwrap.os, 'execvpe', side_effect=lambda *a: calls.append(('exec', a))):
            bwrap.execute(plan)
        self.assertEqual([call[0] for call in calls], ['cwd', 'exec'])

    def test_exec_error_codes(self):
        for error, code in [(FileNotFoundError('missing'), 127),
                            (PermissionError('denied'), 126), (OSError('bad format'), 126)]:
            with self.subTest(code=code), patch.object(bwrap.os, 'name', 'posix'), \
                    patch.object(bwrap.os, 'execvpe', side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bwrap.execute(bwrap.parse_cli(['cmd'], {})), code)

    def test_cli_parse_failure(self):
        result = subprocess.run([sys.executable, bwrap.__file__, '--setenv'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires 2 operand', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_real_exec_arguments_environment_stdio_and_exit_status(self):
        code = ('import os,sys,json; '
                'print(json.dumps([sys.argv[1:],os.environ.get("A"),os.getcwd(),'
                'os.environ["PWD"],sys.stdin.read()])); sys.exit(37)')
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, bwrap.__file__, '--unshare-all', '--chdir', cwd,
                 '--clearenv', '--setenv', 'A', '-value', '--', sys.executable,
                 '-c', code, '', '-x', 'a b', '*'], input='input text',
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 37, result.stderr)
            import json
            args, value, actual_cwd, pwd, stdin = json.loads(result.stdout)
            self.assertEqual(args, ['', '-x', 'a b', '*'])
            self.assertEqual(value, '-value')
            self.assertTrue(os.path.samefile(actual_cwd, cwd))
            self.assertEqual(pwd, actual_cwd)
            self.assertEqual(stdin, 'input text')


if __name__ == '__main__':
    unittest.main()
