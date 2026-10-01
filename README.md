# bwrap-passthrough — a no-op Bubblewrap shim

A Python replacement for the **Bubblewrap `bwrap` command-line interface** that
accepts sandbox options and runs the requested command directly on the host.
Useful when an existing wrapper invokes `bwrap`, but you deliberately want
passthrough execution without Linux namespaces, bind mounts, or sandbox setup.

**This shim provides no sandbox isolation.** Filesystem access, networking,
process visibility, and privileges are those of the user running the command.
Use it only where this behavior is intentional.

Search terms: bubblewrap passthrough, bwrap shim, no-op sandbox, Python argparse
bubblewrap wrapper, disable bwrap isolation, sandbox passthrough, bwrap replacement.

## Features

- Explicit parsing for all **68 options** found in the upstream Bubblewrap parser
  used to generate the included compatibility fixture.
- Filesystem, namespace, capability, security-label, lifecycle, and monitoring
  options are accepted as **no-ops**.
- Working-directory and environment options are applied in their intended order.
- Commands and their arguments are passed without shell interpolation or `eval`.
- Empty arguments, spaces, Unicode, literal wildcards, and values starting with
  `-` are preserved.
- Supports NUL-separated option lists through `--args FD`.
- Linux/POSIX uses `os.execvpe`: the shim process becomes the requested command.
- Windows supports native execution using a child process and preserves its exit
  status. Installing over the real Bubblewrap binary is a Linux operation.
- Backup-first installer, deterministic backup locations, SHA-256 verification,
  atomic replacement, repeatable installation, and restoration.
- Standard-library-only runtime and tests. No pip dependencies.

## Requirements

- Python 3.9 or newer; CI exercises Python 3.10 and 3.13 on Linux and Windows.
- The command you request must be installed and accessible by path or `PATH`.
- `curl` is required for the HTTP integration test.
- System installation normally needs root privileges to replace `/usr/bin/bwrap`
  and write `/var/lib/bwrap-passthrough`.

This project is a standalone command-line utility. `setup.py` is its
backup/install/restore utility, **not a setuptools packaging script**. Do not use
`pip install .` as an installation method.

## Quick start without replacing Bubblewrap

```bash
git clone https://github.com/n3tw4rl0k/bwrap-passthrough.git
cd bwrap-passthrough

python3 bwrap.py --unshare-all --share-net -- curl google.com
python3 bwrap.py --ro-bind /usr /usr -- python3 -c 'print("hello")'
python3 bwrap.py --chdir "$PWD" -- bash -lc 'pwd'
```

The options before the command are interpreted by the shim. Once the command
starts, its arguments are literal and no longer parsed as Bubblewrap options.

```bash
python3 bwrap.py -- curl --header 'X-Example: a b' https://example.com
python3 bwrap.py -- sqlite3 :memory: 'SELECT 6 * 7;'
python3 bwrap.py -- psql -c 'SELECT 42;'
python3 bwrap.py -- mysql --execute 'SELECT 42;'
python3 bwrap.py -- git status --porcelain
```

There is no command allowlist. `curl`, SQL clients, shells, compilers, and custom
executables all use the same dispatch path. A database client still requires its
usual installation, connection settings, and credentials.

## Install over the real `bwrap`

Run these commands on the Linux machine whose Bubblewrap executable you intend
to replace:

```bash
sudo python3 setup.py install --target /usr/bin/bwrap
```

You can omit `install`, or let the installer locate `bwrap` in its current `PATH`:

```bash
sudo python3 setup.py --target /usr/bin/bwrap
sudo python3 setup.py install
```

An explicit target is useful because `sudo` may use a different `PATH` from your
interactive shell. Target symlinks are resolved, and the underlying executable
is replaced while the symlink remains in place.

After installation, an existing wrapper can keep invoking `bwrap`:

```bash
bwrap --unshare-all --share-net --die-with-parent --new-session \
  --proc /proc --dev /dev \
  --ro-bind /usr /usr --ro-bind /lib /lib \
  --bind "$PWD" "$PWD" --chdir "$PWD" \
  --clearenv --setenv PATH /usr/local/bin:/usr/bin:/bin \
  -- curl google.com
```

The installed shim uses an absolute path to the Python interpreter running the
installer. Keep that interpreter installed. The replacement is executable with
mode `0755`; it does not inherit the original binary's setuid/setgid bits.

### Backup location and installation sequence

The backup root is always:

```text
/var/lib/bwrap-passthrough/
```

Each resolved executable path receives a deterministic subdirectory: the full
SHA-256 hash of that absolute path. The same target always maps to the same
backup directory, independent of the working directory used to run `setup.py`.
Separate targets have separate backups.

```text
/var/lib/bwrap-passthrough/<sha256-of-resolved-target-path>/
    bwrap.original     Original executable bytes
    backup.json        Original SHA-256, target path, mode, UID, GID
    operation.lock     Installer operation lock on Linux
```

The installer prints the exact backup location when it finishes. It performs
these steps in order:

1. Read the replacement and resolve the target path.
2. Lock the target's backup state directory.
3. Exclusively create the original backup, flush it to disk, and save metadata.
4. Read the saved original back and verify its SHA-256 and recorded target.
5. Confirm that the target has not changed during preparation.
6. Prepare and flush the replacement beside the target, then atomically replace
   the target file.
7. Verify the installed bytes.

An existing original backup is **never overwritten**, including on repeat
installation. Missing, incomplete, or corrupt backup state causes installation
to stop before replacement. A target already containing this shim without an
original backup is rejected. If a package manager replaces the shim with a
different binary, the installer refuses to overwrite that binary or silently
replace the saved original.

### Status and restore

```bash
sudo python3 setup.py status --target /usr/bin/bwrap
sudo python3 setup.py restore --target /usr/bin/bwrap
```

Restoration verifies the backup, restores the original bytes and recorded Unix
mode/UID/GID, and keeps the backup available. It refuses to overwrite an
unrelated replacement at the target. The backup records bytes and basic Unix
ownership/permissions; extended attributes, ACLs, file capabilities, and original
timestamps are not reproduced.

## Option behavior

| Option group | Behavior |
| --- | --- |
| `--chdir DIR` | Change to an existing host directory; the last value wins. |
| `--setenv VAR VALUE` | Set a variable in the command environment. |
| `--unsetenv VAR` | Remove a variable from the command environment. |
| `--clearenv` | Clear the environment, preserving `PWD` until final directory setup. |
| `--argv0 VALUE` | Override the argument vector's first element while executing the original command path. |
| `--args FD` | Read NUL-separated wrapper options from an inherited file descriptor, then close it. |
| `--help`, `--version` | Display the shim's help/version and exit. |
| `--bind`, `--ro-bind`, `--dev-bind`, `--bind-fd`, `--ro-bind-fd`, and `-try` variants | Consume the correct operands; no mount is performed. |
| `--dir`, `--tmpfs`, `--symlink`, `--file`, `--chmod`, overlay options | Consume operands; no host files or directories are created or modified. |
| Namespace, UID/GID, capability, seccomp, and SELinux options | Accepted and ignored. |
| `--new-session`, `--die-with-parent`, `--as-pid-1` | Accepted and ignored. |
| `--lock-file`, `--sync-fd`, `--block-fd`, `--userns-block-fd` | Accepted and ignored. |
| `--info-fd`, `--json-status-fd` | Accepted and ignored; no status data is written. |
| Unknown options | Ignored; see command-boundary rules below. |

`PWD` is set to the actual working directory immediately before execution.
On Windows, `SystemRoot` is retained when needed by native runtime initialization,
including after `--clearenv`.

Use `python3 bwrap.py --help` for the complete option list and operand names.

### Command boundaries and unknown options

Known options have explicit operand counts, so their values can themselves be
`--`, `-x`, or empty strings where applicable. Both forms work:

```bash
python3 bwrap.py --unshare-all curl google.com
python3 bwrap.py --unshare-all -- curl google.com
```

For an unknown option with operands, use an explicit command delimiter:

```bash
python3 bwrap.py --future-option arbitrary-value -- curl google.com
```

Without `--`, an unknown option is treated as a flag with no operands: the next
non-option token starts the command. No parser can infer the operand count of an
option whose definition it does not know. This permissive fallback does not
emulate possible new future Bubblewrap semantics.

### `--args FD`

Argument files contain wrapper options only, separated by NUL bytes. A trailing
NUL is optional. Multiple `--args` options are supported, in encounter order;
nested `--args` inside an argument file is rejected. The file descriptor must
already be open in the shim process.

Example on Linux:

```bash
python3 - <<'PY'
import os
import subprocess
import tempfile

with tempfile.TemporaryFile() as options:
    options.write(b'--unshare-all\0--setenv\0EXAMPLE\0hello world\0')
    options.seek(0)
    subprocess.run(
        ['python3', 'bwrap.py', '--args', str(options.fileno()), '--',
         'python3', '-c', 'import os; print(os.environ["EXAMPLE"])'],
        pass_fds=(options.fileno(),), check=True,
    )
PY
```

Argument files are limited to 8 MiB; recognized wrapper arguments are limited
to 100,000 tokens. Malformed operational options, unreadable argument FDs, a
missing command, or an invalid requested working directory still produce errors.
No-op options do not inspect mount sources, open synchronization FDs, or validate
ignored permission/namespace values.

## Compatibility boundaries

The goal is passthrough **command-line compatibility**, not reproduction of
Bubblewrap's runtime environment. All 68 recognized options and their arities
are checked against `upstream-options.json`, generated independently from
upstream documentation and verified against the upstream C parser. The fixture
records source URLs and SHA-256 hashes. This is a snapshot, not a promise about
every future Bubblewrap release.

Because mounting is a no-op, a command must refer to paths that exist on the
host. For example, `--bind /project /app --chdir /app` does not make `/app` exist.
Use the actual host path for `--chdir`, the executable, and its file arguments.

Monitoring options are no-ops too. A launcher that requires JSON messages from
`--info-fd` or `--json-status-fd`, or a namespace handshake, needs additional
adaptation; accepting those arguments does not implement that protocol. Parent
death handling, session changes, locks, PID-1 behavior, and seccomp filtering are
not provided.

Shell pipelines, redirects, and variable expansion require an explicitly
invoked shell, just as they do when directly launching a command:

```bash
python3 bwrap.py -- bash -lc 'curl -s https://example.com | head -n 5'
```

On POSIX, inherited file descriptors and stdio follow `exec` semantics. On
Windows, child-process inheritance follows Python's `subprocess` behavior;
there is no POSIX process replacement or Linux namespace support.

### Exit codes

| Code | Meaning |
| --- | --- |
| Command's exit code | Returned unchanged for a normally executed command. |
| `2` | Shim option/setup error. |
| `126` | Executable exists but cannot be executed. |
| `127` | Command not found. |
| `130` | Shim interrupted before command execution. |

The installer separately returns `0` on success and `1` on installation or
restoration failure.

## Tests

Run the default suite from the project directory:

```bash
python3 -m unittest -v
```

The default suite includes:

- Independent upstream option-name and operand-count coverage.
- Original wrapper argument parsing, literal argument preservation, environment
  ordering, NUL-separated FD expansion, and malformed input handling.
- Mocked POSIX dispatch of `curl google.com`, SQLite/PostgreSQL/MySQL clients,
  shells, Git, and arbitrary custom executables.
- Real subprocess execution with stdin/stdout/stderr, directory changes, exit
  codes, missing commands, and executable lookup through `PATH`.
- A real `curl` HTTP request to a local test server, without public-internet
  dependence.
- Real SQL execution using Python's SQLite engine.
- Real execution with every no-op option combined in the same invocation.
- Installer backup ordering, corruption detection, repeat installation,
  restoration, simulated write/replacement failures, and execution of the
  installed copy. POSIX-only tests cover symlinks and setuid/setgid restoration.

An optional live-network smoke test executes `curl google.com` through the shim:

```bash
python3 smoke_network.py
```

That test needs Internet access. It verifies curl's successful exit and a
nonempty HTTP response; it does not require a particular page body or HTTP
status. CI runs the deterministic suite on Linux and Windows and does not rely
on public Internet responses for command correctness.

Installer tests use temporary fake executables and temporary backup roots. They
never replace the machine's real `bwrap`.

## Project files

| File | Purpose |
| --- | --- |
| `bwrap.py` | Passthrough CLI and execution runtime. |
| `setup.py` | Backup-first system install, status, and restore. |
| `test_bwrap.py` | Parser and execution behavior tests. |
| `test_commands.py` | Compatibility, HTTP, SQL, and command-dispatch tests. |
| `test_setup.py` | Installer tests against temporary files. |
| `smoke_network.py` | Optional public-network curl test. |
| `upstream-options.json` | Independent option-coverage snapshot. |

## License and upstream

MIT licensed; see [LICENSE](LICENSE). This is an independent project, not an
official Bubblewrap component.

Upstream references:

- [Bubblewrap repository](https://github.com/containers/bubblewrap)
- [Bubblewrap CLI documentation](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)
- [Bubblewrap argument parser](https://github.com/containers/bubblewrap/blob/main/bubblewrap.c)
