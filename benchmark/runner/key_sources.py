"""Answer-key sources matched by a salted hash of the name, not the name.

finalize_run.py flags a run that fetches or searches for grading material. Some of those
sources are named nowhere a run can read: the gated reference dataset on Hugging Face and
the OSF projects that hold reference outputs. The harness is on disk while the agent
works, so naming them here would tell the agent where they are. This file holds only
the SHA-256 of each name, salted, and checks the tokens of a line against them.

A token is a run of letters and digits, also taken with '-', and with '-', '.' and '_',
in lower case, so a name is found inside a URL, a path or a repository id.
"""
import hashlib
import re

SALT = b"skills-benchmark/answer-key/v1:"

# The reference dataset's repository name, then the three OSF projects.
KEY_TOKENS = frozenset((
    "396bc85adb5395e0e2b8c31dca16fffcf4a40ee91a67ba9cb831e16a32aeef53",
    "770dd60a69b9fa1283c0598a7d6a9aa800a4fec4083da9a5f104ed0aa67cb703",
    "476938e8324b31b141c6b2892b87d914b579d0d1babeb61c23f615c5f51bf8f5",
    "fde266b79a00d1a5ebc40d9b52923a00bca11eb68078c8d2ab92ee3d74baf63d",
))

_SPLITS = (re.compile(r"[a-z0-9]+"), re.compile(r"[a-z0-9-]+"), re.compile(r"[a-z0-9._-]+"))


def digest(token):
    return hashlib.sha256(SALT + token.lower().encode()).hexdigest()


def tokens(text):
    low = text.lower()
    return {t for rx in _SPLITS for t in rx.findall(low)}


def named_source(text, key_tokens=KEY_TOKENS):
    """True when a token of text hashes to one of key_tokens."""
    return any(digest(t) in key_tokens for t in tokens(text))
