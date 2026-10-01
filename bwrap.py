#!/usr/bin/env python3
"""Bubblewrap CLI passthrough shim. NO sandbox or filesystem isolation.

Requires Python 3.9+. POSIX uses exec; Windows waits for a native child process.
Option schema: https://github.com/containers/bubblewrap/blob/main/bwrap.xml
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence

VERSION = "bwrap-passthrough 1.2.0 (NO ISOLATION)"
MAX_ARGS_BYTES = 8 * 1024 * 1024
MAX_ARGS = 100_000

# Every documented bwrap option has an explicit arity. Unknown options are
# ignored too; use an explicit -- to delimit their otherwise unknown operands.
SCHEMA: dict[str, tuple[str, ...]] = {}


def register(names: str, *operands: str) -> None:
    for name in names.split():
        SCHEMA["--" + name] = operands


register("help version level-prefix unshare-user unshare-user-try unshare-ipc "
         "unshare-pid unshare-net unshare-uts unshare-cgroup unshare-cgroup-try "
         "unshare-all share-net disable-userns assert-userns-disabled "
         "clearenv new-session die-with-parent as-pid-1 not-a-security-boundary")
register("args userns userns2 pidns sync-fd seccomp add-seccomp-fd block-fd "
         "userns-block-fd info-fd json-status-fd", "FD")
register("uid gid", "ID")
register("argv0", "VALUE")
register("hostname", "HOSTNAME")
register("chdir lock-file remount-ro overlay-src tmp-overlay ro-overlay "
         "proc dev tmpfs mqueue dir", "PATH")
register("setenv", "VAR", "VALUE")
register("unsetenv", "VAR")
register("perms", "OCTAL")
register("size", "BYTES")
register("bind bind-try dev-bind dev-bind-try ro-bind ro-bind-try symlink", "SRC", "DEST")
register("overlay", "RWSRC", "WORKDIR", "DEST")
register("file bind-data ro-bind-data bind-fd ro-bind-fd", "FD", "DEST")
register("chmod", "OCTAL", "PATH")
register("exec-label file-label", "LABEL")
register("cap-add cap-drop", "CAP")

IMPLEMENTED = frozenset({"--args", "--chdir", "--setenv", "--unsetenv",
                         "--clearenv", "--argv0", "--help", "--version"})


class ShimError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ShimError(message)


class Record(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        # Values were replaced by inert placeholders before argparse sees them.
        # This preserves operands such as '--', '-x', and empty strings, which
        # argparse would otherwise interpret as option syntax.
        originals = tuple(parser.operands[value] for value in values)
        namespace.events.append((option_string, originals))


def make_parser() -> Parser:
    parser = Parser(
        prog="bwrap-passthrough", add_help=False, allow_abbrev=False,
        usage="%(prog)s [BWRAP OPTIONS] [--] COMMAND [ARG ...]",
        description="Runs COMMAND directly on the host. NO sandbox/isolation.",
        epilog="Filesystem, namespace, capability and lifecycle flags are no-ops. "
               "Mount destinations are not created or translated. "
               "Monitoring/synchronization options are also no-ops. "
               "Unknown options are ignored; use -- to delimit their operands. "
               "Without --, unknown options are treated as flags without operands. "
               "Parsing stops at COMMAND; all subsequent arguments are literal.",
    )
    parser.operands = {}
    for option, operands in SCHEMA.items():
        behavior = "implemented" if option in IMPLEMENTED else "accepted, ignored"
        parser.add_argument(option, nargs=len(operands), action=Record,
                            metavar=operands or None, help=behavior)
    return parser


def validate(option: str, values: tuple[str, ...]) -> None:
    for kind, value in zip(SCHEMA[option], values):
        if "\0" in value:
            raise ShimError(f"{option}: NUL is not valid in an argument")
        if kind in {"PATH", "SRC", "DEST", "RWSRC", "WORKDIR"} and not value:
            raise ShimError(f"{option}: {kind} must not be empty")
        if kind in {"FD", "ID", "BYTES"} and not re.fullmatch(r"[0-9]+", value):
            raise ShimError(f"{option}: {kind} must be a nonnegative decimal integer")
        if kind == "OCTAL" and (not re.fullmatch(r"[0-7]+", value)
                                or int(value, 8) > 0o7777):
            raise ShimError(f"{option}: invalid permission mode {value!r}")
        if kind == "VAR" and (not value or "=" in value):
            raise ShimError(f"{option}: invalid environment variable name {value!r}")


def read_args_fd(value: str) -> list[str]:
    significant = value.lstrip('0') or '0'
    if len(significant) > 10:
        raise ShimError("--args: file descriptor is out of range")
    fd = int(significant)
    if fd > 2**31 - 1:
        raise ShimError("--args: file descriptor is out of range")
    chunks = []
    total = 0
    try:
        while True:
            chunk = os.read(fd, min(65536, MAX_ARGS_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ARGS_BYTES:
                raise ShimError("--args: argument file exceeds 8 MiB")
    except OSError as exc:
        raise ShimError(f"--args: cannot read FD {fd}: {exc.strerror}") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    data = b"".join(chunks)
    if not data:
        return []
    fields = data.split(b"\0")
    if fields[-1] == b"":
        fields.pop()  # A trailing separator is not an extra empty argument.
    return [os.fsdecode(field) for field in fields]


@dataclass
class Plan:
    command: list[str]
    events: list[tuple[str, tuple[str, ...]]]
    env: dict[str, str]
    cwd: str | None
    argv0: str | None


def parse_cli(argv: Sequence[str], environ: Mapping[str, str] | None = None) -> Plan:
    parser = make_parser()
    normalized: list[str] = []
    command: list[str] = []
    count = 0

    def scan(tokens: Sequence[str], in_file: bool = False) -> None:
        nonlocal count, command
        index = 0
        unknown_end = None
        while index < len(tokens):
            token = tokens[index]
            if token == "--" or not token.startswith("-") or token == "-":
                if unknown_end is not None and index < unknown_end:
                    index += 1
                    continue
                if in_file:
                    raise ShimError("--args: argument files must contain options only")
                command = list(tokens[index + 1:] if token == "--" else tokens[index:])
                return
            if token not in SCHEMA:
                # Future/foreign options never cause failure. Search for a
                # command delimiter while respecting known operand arities
                # (a -- passed as --setenv's value is not a delimiter).
                if unknown_end is None:
                    probe = index + 1
                    while probe < len(tokens):
                        if tokens[probe] == "--":
                            unknown_end = probe
                            break
                        probe += 1 + len(SCHEMA.get(tokens[probe], ()))
                    if in_file and unknown_end is None:
                        unknown_end = len(tokens)
                index += 1
                continue
            arity = len(SCHEMA[token])
            if len(tokens) - index - 1 < arity:
                raise ShimError(f"{token} requires {arity} operand(s)")
            values = tuple(tokens[index + 1:index + 1 + arity])
            if token in IMPLEMENTED:
                validate(token, values)
            count += 1 + arity
            if count > MAX_ARGS:
                raise ShimError("too many wrapper arguments")
            if token == "--help":
                parser.print_help()
                raise SystemExit(0)
            if token == "--version":
                print(VERSION)
                raise SystemExit(0)
            if token == "--args":
                if in_file:
                    raise ShimError("--args is not allowed inside an argument file")
                scan(read_args_fd(values[0]), in_file=True)
            else:
                normalized.append(token)
                for value in values:
                    key = f"operand_{len(parser.operands)}"
                    parser.operands[key] = value
                    normalized.append(key)
            index += 1 + arity

    scan(list(argv))
    namespace = parser.parse_args(normalized, namespace=argparse.Namespace(events=[]))
    if not command or not command[0]:
        raise ShimError("COMMAND is required; use --help for usage")
    env = dict(os.environ if environ is None else environ)
    cwd = None
    argv0 = None
    for option, values in namespace.events:
        if option == "--clearenv":
            env = {"PWD": env["PWD"]} if "PWD" in env else {}
        elif option == "--setenv":
            env[values[0]] = values[1]
        elif option == "--unsetenv":
            env.pop(values[0], None)
        elif option == "--chdir":
            cwd = values[0]
        elif option == "--argv0":
            argv0 = values[0]
    return Plan(command, namespace.events, env, cwd, argv0)


def execute(plan: Plan) -> int:
    try:
        if plan.cwd is not None:
            os.chdir(plan.cwd)
        # bwrap sets PWD to the final working directory after environment setup.
        plan.env["PWD"] = os.getcwd()
    except OSError as exc:
        raise ShimError(f"cannot enter working directory: {exc}") from exc
    argv = list(plan.command)
    if plan.argv0 is not None:
        argv[0] = plan.argv0
    try:
        if os.name == "nt":
            # Windows has no POSIX process replacement. Run a native executable
            # without a shell, inherit stdio, wait, and propagate its exit code.
            # Native Windows runtimes need SystemRoot even with --clearenv.
            if "SystemRoot" in os.environ:
                plan.env.setdefault("SystemRoot", os.environ["SystemRoot"])
            executable = shutil.which(plan.command[0], path=plan.env.get("PATH", os.defpath))
            if executable is None:
                raise FileNotFoundError(plan.command[0])
            return subprocess.call(argv, executable=executable, env=plan.env)
        # No shell, no eval, no intermediate child: PID, signals, inherited FDs,
        # stdio and command exit status follow exec semantics.
        os.execvpe(plan.command[0], argv, plan.env)
    except FileNotFoundError as exc:
        print(f"bwrap-passthrough: command not found: {plan.command[0]!r}: {exc}",
              file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"bwrap-passthrough: cannot execute {plan.command[0]!r}: {exc}",
              file=sys.stderr)
        return 126
    return 0  # execvpe never returns on success.


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return execute(parse_cli(sys.argv[1:] if argv is None else argv))
    except ShimError as exc:
        print(f"bwrap-passthrough: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
