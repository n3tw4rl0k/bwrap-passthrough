#!/usr/bin/env python3
"""Install/restore the passthrough bwrap. Run installation on Linux with sudo.

Backup root is always /var/lib/bwrap-passthrough. Each resolved target path has
its own deterministic subdirectory. An existing original backup is never replaced.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

BACKUP_ROOT = Path('/var/lib/bwrap-passthrough')
SHIM = Path(__file__).resolve().with_name('bwrap.py')
MARKER = b'# bwrap-passthrough managed replacement\n'


class InstallError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def backup_directory(target, root=BACKUP_ROOT):
    key = digest(os.fsencode(str(Path(target).resolve())))
    return Path(root) / key


def sync_directory(directory):
    if os.name == 'posix':
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path, data, mode, owner=None):
    """Prepare and flush a sibling file before atomically replacing the target."""
    fd, name = tempfile.mkstemp(prefix='.bwrap-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            if owner is not None and os.name == 'posix':
                os.fchown(stream.fileno(), *owner)
            if os.name == 'posix':
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(directory):
    lock = directory / 'operation.lock'
    if lock.is_symlink():
        raise InstallError(f'Lock must not be a symlink: {lock}')
    if os.name == 'posix':
        import fcntl
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
    else:
        # Portable fallback for tests; the actual installer CLI requires Linux.
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise InstallError(f'Another operation holds {lock}') from exc
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink()


def prepare_directory(directory):
    # Reject symlinked state paths before writing privileged backups.
    for path in [directory, *directory.parents]:
        if path.is_symlink():
            raise InstallError(f'Backup directory must not traverse symlinks: {path}')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix':
        for path in (directory.parent, directory):
            info = path.stat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise InstallError(f'Backup directory has unsafe owner/permissions: {path}')


def load_backup(directory, target):
    original = directory / 'bwrap.original'
    manifest = directory / 'backup.json'
    if original.is_symlink() or manifest.is_symlink():
        raise InstallError('Backup files must not be symlinks')
    if not original.exists() or not manifest.exists():
        raise InstallError('Backup is incomplete; target has not been replaced')
    try:
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        data = original.read_bytes()
        if metadata['target'] != str(target) or metadata['sha256'] != digest(data):
            raise InstallError('Backup verification failed; refusing to replace target')
        for key in ('mode', 'uid', 'gid'):
            if not isinstance(metadata[key], int):
                raise InstallError('Invalid backup metadata')
        return data, metadata
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError('Invalid backup metadata') from exc


def shim_bytes(shim_path=SHIM):
    source = Path(shim_path).read_bytes()
    if b'bwrap-passthrough' not in source or not source.startswith(b'#!'):
        raise InstallError('Replacement is not the expected passthrough shim')
    interpreter = str(Path(sys.executable).resolve())
    if os.name == 'posix' and any(c.isspace() for c in interpreter):
        raise InstallError('Python interpreter path cannot contain whitespace in a shebang')
    # Absolute interpreter works even when the caller has cleared PATH.
    return b'#!' + os.fsencode(interpreter) + b'\n' + MARKER + source.split(b'\n', 1)[1]


def install(target, root=BACKUP_ROOT, shim_path=SHIM):
    target = Path(target).resolve(strict=True)
    if not target.is_file():
        raise InstallError(f'Target must be a regular file: {target}')
    replacement = shim_bytes(shim_path)
    directory = backup_directory(target, root)
    prepare_directory(directory)
    with locked(directory):
        current = target.read_bytes()
        info = target.stat()
        original_path = directory / 'bwrap.original'
        manifest = directory / 'backup.json'
        if original_path.exists() or manifest.exists() or original_path.is_symlink() or manifest.is_symlink():
            original, metadata = load_backup(directory, target)
        else:
            if MARKER in current or b'Bubblewrap CLI passthrough shim' in current:
                raise InstallError('Target is already a shim but no original backup exists')
            metadata = {'target': str(target), 'sha256': digest(current),
                        'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid,
                        'gid': info.st_gid}
            # Exclusive creation guarantees we never overwrite an old original.
            with original_path.open('xb') as stream:
                stream.write(current)
                stream.flush()
                if os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
            sync_directory(directory)
            atomic_write(manifest, (json.dumps(metadata, indent=2) + '\n').encode(), 0o600)
            original, metadata = load_backup(directory, target)
        if current != original and MARKER not in current:
            raise InstallError('Target differs from the saved original and is not our shim; '
                               'refusing to overwrite it or its backup')
        # Verify the saved original again immediately before any target change.
        load_backup(directory, target)
        if target.read_bytes() != current:
            raise InstallError('Target changed during installation; refusing to replace it')
        if current != replacement:
            atomic_write(target, replacement, 0o755, (metadata['uid'], metadata['gid']))
        if target.read_bytes() != replacement:
            raise InstallError('Installed shim verification failed')
    return target, directory


def restore(target, root=BACKUP_ROOT):
    target = Path(target).resolve()
    directory = backup_directory(target, root)
    if not directory.is_dir():
        raise InstallError(f'No backup exists for {target}')
    prepare_directory(directory)
    with locked(directory):
        original, metadata = load_backup(directory, target)
        current = target.read_bytes() if target.exists() else None
        if current is not None and current != original and MARKER not in current:
            raise InstallError('Target is neither our shim nor the saved original; refusing to overwrite it')
        atomic_write(target, original, metadata['mode'], (metadata['uid'], metadata['gid']))
        if target.read_bytes() != original:
            raise InstallError('Restored original verification failed')
    return target, directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'restore', 'status'], nargs='?', default='install')
    parser.add_argument('--target', help='bwrap executable path; default: find bwrap in PATH')
    args = parser.parse_args(argv)
    try:
        if os.name != 'posix':
            raise InstallError('Run this installer on Linux; Windows tests do not replace a system binary')
        candidate = args.target or shutil.which('bwrap')
        if not candidate:
            raise InstallError('bwrap not found; provide --target /usr/bin/bwrap')
        target = Path(candidate).resolve()
        if args.action == 'status':
            directory = backup_directory(target)
            load_backup(directory, target)
            installed = target.is_file() and MARKER in target.read_bytes()
            print(f'Target: {target}\nBackup: {directory}\nShim installed: {installed}')
        else:
            target, directory = (install if args.action == 'install' else restore)(target)
            print(f'{args.action}: {target}\nVerified original backup: {directory / "bwrap.original"}')
        return 0
    except (InstallError, OSError) as exc:
        print(f'setup: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
