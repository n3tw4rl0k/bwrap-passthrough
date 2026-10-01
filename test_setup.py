"""Installer tests operate exclusively on temporary fake executables."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import setup


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / 'bwrap'
        self.original = b'\x7fELF original bubblewrap bytes\x00\xff'
        self.target.write_bytes(self.original)
        self.root = self.base / 'backups'

    def install(self):
        return setup.install(self.target, self.root)

    def test_verified_original_exists_before_replacement(self):
        writer = setup.atomic_write
        checked = []
        def intercept(path, data, mode, owner=None):
            if path == self.target.resolve():
                directory = setup.backup_directory(self.target, self.root)
                original, metadata = setup.load_backup(directory, self.target.resolve())
                self.assertEqual(original, self.original)
                self.assertEqual(metadata['sha256'], setup.digest(self.original))
                self.assertEqual(self.target.read_bytes(), self.original)
                checked.append(True)
            return writer(path, data, mode, owner)
        with patch.object(setup, 'atomic_write', side_effect=intercept):
            target, directory = self.install()
        self.assertEqual(checked, [True])
        self.assertIn(setup.MARKER, target.read_bytes())
        self.assertEqual((directory / 'bwrap.original').read_bytes(), self.original)

    def test_backup_location_is_deterministic(self):
        _, first = self.install()
        _, second = self.install()
        self.assertEqual(first, second)
        self.assertEqual(first.parent, self.root)
        self.assertEqual((first / 'bwrap.original').read_bytes(), self.original)

    def test_different_targets_have_separate_backups(self):
        other = self.base / 'other-bwrap'
        other.write_bytes(b'other original')
        _, first = self.install()
        _, second = setup.install(other, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual((second / 'bwrap.original').read_bytes(), b'other original')

    def test_restore_and_reinstall_preserve_original(self):
        _, directory = self.install()
        setup.restore(self.target, self.root)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.install()
        self.assertIn(setup.MARKER, self.target.read_bytes())
        self.assertEqual((directory / 'bwrap.original').read_bytes(), self.original)

    def test_tampered_backup_refuses_install_and_restore(self):
        _, directory = self.install()
        installed = self.target.read_bytes()
        (directory / 'bwrap.original').write_bytes(b'tampered')
        for action in (setup.install, setup.restore):
            with self.subTest(action=action.__name__), self.assertRaises(setup.InstallError):
                action(self.target, self.root)
            self.assertEqual(self.target.read_bytes(), installed)

    def test_backup_write_failure_leaves_original_untouched(self):
        with patch.object(setup, 'atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.target.read_bytes(), self.original)
        with self.assertRaisesRegex(setup.InstallError, 'incomplete'):
            self.install()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_original_backup_never_overwritten_by_a_new_binary(self):
        _, directory = self.install()
        self.target.write_bytes(b'updated system binary')
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertEqual(self.target.read_bytes(), b'updated system binary')
        self.assertEqual((directory / 'bwrap.original').read_bytes(), self.original)

    def test_shim_without_backup_is_not_saved_as_original(self):
        self.target.write_bytes(setup.shim_bytes())
        with self.assertRaisesRegex(setup.InstallError, 'no original backup'):
            self.install()
        directory = setup.backup_directory(self.target, self.root)
        self.assertFalse((directory / 'bwrap.original').exists())

    def test_manifest_corruption_does_not_change_target(self):
        _, directory = self.install()
        before = self.target.read_bytes()
        (directory / 'backup.json').write_text('{invalid')
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertEqual(self.target.read_bytes(), before)

    def test_atomic_replace_failure_keeps_target_and_backup(self):
        replace = setup.os.replace
        def fail_target(source, destination):
            if destination == self.target.resolve():
                raise OSError('replacement denied')
            return replace(source, destination)
        with patch.object(setup.os, 'replace', side_effect=fail_target):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.target.read_bytes(), self.original)
        directory = setup.backup_directory(self.target, self.root)
        self.assertEqual((directory / 'bwrap.original').read_bytes(), self.original)
        self.assertEqual(list(self.base.glob('.bwrap-install-*')), [])

    def test_missing_target_is_not_created(self):
        missing = self.base / 'absent'
        with self.assertRaises(FileNotFoundError):
            setup.install(missing, self.root)
        self.assertFalse(missing.exists())

    def test_installed_shebang_uses_absolute_python(self):
        self.install()
        expected = b'#!' + os.fsencode(str(Path(setup.sys.executable).resolve()))
        self.assertEqual(self.target.read_bytes().splitlines()[0], expected)

    def test_installed_copy_actually_runs_a_command(self):
        self.install()
        result = subprocess.run([sys.executable, str(self.target), '--unshare-all',
                                 '--', sys.executable, '-c', 'print("INSTALLED_OK")'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'INSTALLED_OK')

    @unittest.skipUnless(os.name == 'posix', 'POSIX executable permissions')
    def test_permissions_and_setuid_restore(self):
        self.target.chmod(0o6751)
        _, directory = self.install()
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((directory / 'bwrap.original').stat().st_mode), 0o600)
        setup.restore(self.target, self.root)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o6751)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink support')
    def test_target_symlink_remains_a_symlink(self):
        link = self.base / 'bwrap-link'
        link.symlink_to(self.target)
        _, directory = setup.install(link, self.root)
        self.assertTrue(link.is_symlink())
        self.assertEqual(directory, setup.backup_directory(self.target, self.root))
        setup.restore(link, self.root)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.target.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
