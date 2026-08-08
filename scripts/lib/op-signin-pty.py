#!/usr/bin/env python3
"""Answer `op`'s interactive prompts from files, and print the session line.

WHY A PTY. The 1Password CLI cannot take an account password on stdin — that
path broke in op 1.0 and never came back. Piping a password to `op signin` does
not even fail informatively: it prints the generic "You are not currently
signed in" and exits, which reads exactly like a wrong password. The only
remaining non-interactive route is to give op what it insists on, a terminal,
and answer the prompt on the other side of it.

WHY NOT --raw. Without --raw, op prints a ready-to-eval line:

    export OP_SESSION_abc123="…"

Capturing that whole line means nothing here has to know how op names the
variable, which has changed across major versions. Callers just eval it.

TWO MODES, because a machine with no account added needs both steps:

    signin       answers: password
    add-account  answers: Secret Key, then password
                 (`op account add` has --address and --email flags but no
                 --secret-key, so the key has to be typed like the password)

Secrets are read from files whose PATHS are passed in argv — the values
themselves never appear in argv or the environment, where /proc would expose
them to every process on the machine.
"""

import argparse
import os
import pty
import re
import select
import sys
import time

# op draws prompts with cursor/colour control sequences; strip them before
# matching so a styling change upstream cannot break the match.
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
EXPORT = re.compile(r'^\s*export\s+(OP_SESSION_[A-Za-z0-9_]+)="(.*)"\s*$')

PROMPT_SECRET_KEY = re.compile(rb"secret key", re.IGNORECASE)
PROMPT_PASSWORD = re.compile(rb"password", re.IGNORECASE)


def read_secret(path):
    with open(path, "rb") as fh:
        data = fh.read()
    # Exactly one trailing newline, the one `printf '%s\n' … > file` leaves.
    # Stripping all trailing whitespace would corrupt a password ending in a
    # space, which is legal and which nothing here could ever debug.
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        sys.exit(f"op-signin-pty: {path} is empty")
    return data


def run(argv, answers, timeout):
    """Spawn argv under a pty, answering prompts in order. Returns (text, status).

    `answers` is a list of (compiled pattern, bytes). Each is used once, in
    order: op asks for the Secret Key before the password, and matching out of
    order would send the password to the wrong prompt.
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("OP_SESSION", None)
        os.execvp(argv[0], argv)
        os._exit(127)  # unreachable; execvp raises on failure

    buf = b""
    cursor = 0        # everything before this has already been matched against
    pending = list(answers)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if not select.select([fd], [], [], remaining)[0]:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break  # child closed the pty: normal EOF here
            if not chunk:
                break
            buf += chunk
            if pending:
                clean = ANSI.sub(b"", buf)
                if pending[0][0].search(clean[cursor:]):
                    _, value = pending.pop(0)
                    os.write(fd, value + b"\n")
                    cursor = len(clean)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        _, status = os.waitpid(pid, 0)

    clean = ANSI.sub(b"", buf)
    # op runs the PASSWORD prompt with terminal echo off, but it echoes the
    # Secret Key straight back — verified against op 2.38, which printed the key
    # inline with its prompt. Anything typed is therefore scrubbed before this
    # text can reach a log or a systemd journal.
    for _, value in answers:
        if value:
            clean = clean.replace(value, b"***")
    return clean.decode("utf-8", "replace"), status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("signin", "add-account"), default="signin")
    ap.add_argument("--account", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--secret-key-file")
    ap.add_argument("--address")
    ap.add_argument("--email")
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("OP_SIGNIN_TIMEOUT", "40")))
    args = ap.parse_args()

    password = read_secret(args.password_file)

    if args.mode == "signin":
        # -f is required, not optional. op checks whether stdout is a terminal
        # BEFORE prompting, and refuses outright with "Output of 'op signin' is
        # meant to be executed by your terminal" — which under a pty it always
        # is. Without -f the password prompt never even appears.
        argv = ["op", "signin", "--account", args.account, "-f"]
        answers = [(PROMPT_PASSWORD, password)]
    else:
        for required in ("secret_key_file", "address", "email"):
            if not getattr(args, required):
                sys.exit(f"op-signin-pty: --{required.replace('_', '-')} is required for add-account")
        argv = ["op", "account", "add",
                "--address", args.address,
                "--email", args.email,
                "--shorthand", args.account,
                "--signin"]
        answers = [(PROMPT_SECRET_KEY, read_secret(args.secret_key_file)),
                   (PROMPT_PASSWORD, password)]

    text, status = run(argv, answers, args.timeout)

    for line in text.splitlines():
        m = EXPORT.match(line)
        if m:
            # Re-emit rather than echoing op's line verbatim: whatever quoting
            # op used, this is quoting we control and the caller can eval.
            print(f'export {m.group(1)}="{m.group(2)}"')
            return 0

    # op's own diagnostic is the useful part, and run() has already scrubbed
    # anything this process typed, so it is safe to surface.
    for line in text.splitlines():
        if line.strip() and not EXPORT.match(line):
            sys.stderr.write(f"op-signin-pty: op said: {line.strip()}\n")
    sys.stderr.write(f"op-signin-pty: {args.mode} failed (exit status {status})\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
