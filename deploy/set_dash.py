"""Set DASH_USER / DASH_PASS in ~/.env on the server, reading both from stdin.

Run by deploy/set-dash-login.ps1, which prompts for the values on the user's
own machine and pipes them here over ssh, so they never appear on a command
line, in shell history or in a log. Existing DASH_USER / DASH_PASS lines are
replaced; every other line is kept. The file stays mode 600.

    printf 'user\npassword\n' | python3 set_dash.py
"""
import os
import re
import sys

ENV = os.path.expanduser("~/.env")


def quote(v):
    """Single-quoted for bash `source`: ' becomes '\\''."""
    return "'" + v.replace("'", "'\\''") + "'"


def main():
    user = sys.stdin.readline().rstrip("\r\n")
    pw = sys.stdin.readline().rstrip("\r\n")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", user):
        sys.exit("username: 3-32 letters, digits, dot, dash or underscore")
    if len(pw) < 12 or "\n" in pw:
        sys.exit("password: at least 12 characters")
    try:
        lines = open(ENV).read().splitlines()
    except FileNotFoundError:
        lines = []
    keep = [ln for ln in lines if not re.match(r"\s*(export\s+)?DASH_(USER|PASS)=", ln)]
    keep += [f"DASH_USER={quote(user)}", f"DASH_PASS={quote(pw)}"]
    tmp = ENV + ".tmp"
    old = os.umask(0o077)
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(keep) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV)
    finally:
        os.umask(old)
    print(f"set DASH_USER={user} and DASH_PASS ({len(pw)} characters) in {ENV}")


if __name__ == "__main__":
    main()
