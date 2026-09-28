#!/usr/bin/env python3
"""Mask secret values in every file under a directory, in place.

    redact.py DIR VAR [VAR...]

Each VAR names an environment variable holding a secret. Every occurrence of its
value is overwritten with '*' of the same length, so binary formats such as SQLite
keep their structure. Longer secrets are masked first, so a secret that contains
another is masked whole.

Only regular files are read. Directories the upload excludes (data, tmp,
site-packages, node_modules, .git) and files over MAX_BYTES are skipped. A file that
cannot be masked, or still contains a secret afterwards, is deleted. The script does
not stop on a per-file error.

Exit 0 when done. Exit 2, before touching anything, if a secret is shorter than 8
characters: masking it would overwrite unrelated bytes. Finding a secret is reported
as a GitHub warning.
"""
import os
import stat
import sys

MAX_BYTES = 256 * 1024 * 1024
SKIP_DIRS = {"data", "tmp", "site-packages", "node_modules", ".git"}


def warn(msg):
    print("::warning::" + msg)


def mask_file(path, secrets):
    """Return occurrences masked. Deletes the file if it cannot be made clean."""
    with open(path, "rb") as fh:
        data = fh.read()
    hits, new = 0, data
    for name, v in secrets:
        c = new.count(v)
        if c:
            new = new.replace(v, b"*" * len(v))
            hits += c
            warn("%s found %d time(s) in %s; masked" % (name, c, path))
    if hits:
        try:
            fh = open(path, "wb")
        except PermissionError:
            os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
            fh = open(path, "wb")
        with fh:
            fh.write(new)
        with open(path, "rb") as fh:
            if any(v in fh.read() for _, v in secrets):
                raise ValueError("secret still present after masking")
    return hits


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    root, names = sys.argv[1], sys.argv[2:]
    secrets = []
    for n in names:
        v = os.environ.get(n, "").strip()
        if not v:
            print("skip  %s is unset" % n)
            continue
        if len(v) < 8:
            print("ERROR: %s is shorter than 8 characters; refusing to mask it" % n)
            return 2
        secrets.append((n, v.encode()))
    secrets.sort(key=lambda s: len(s[1]), reverse=True)

    hits = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size > MAX_BYTES:
                    print("skip  %s is larger than %d bytes" % (p, MAX_BYTES))
                    continue
                hits += mask_file(p, secrets)
            except Exception as e:
                try:
                    os.chmod(p, stat.S_IWUSR | stat.S_IRUSR)
                    os.remove(p)
                    warn("could not mask %s (%s); deleted" % (p, e))
                except OSError as e2:
                    warn("could not mask or delete %s (%s; %s)" % (p, e, e2))
    print("redact: %d occurrence(s) masked under %s" % (hits, root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
