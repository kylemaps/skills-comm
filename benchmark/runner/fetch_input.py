#!/usr/bin/env python3
"""Fetch a blinded run's input: the case's scan, handed over under a neutral name.

    HF_TOKEN=... python3 fetch_input.py --task TASK --case case-NNN --dest DIR \\
        --cases-repo ORG/DATASET --cases-revision REV \\
        --dataset DATASET_ID --version VERSION [--record FILE] [--export-env FILE]

Resolves the case to its subject through the gated cases.json (the reference dataset,
at the pinned revision), walks the subject's files in the dataset's pinned OpenNeuro
snapshot (its GraphQL file tree, which names a versioned object per file), picks the
ones input_spec.json names for the task (each glob must match exactly one file),
downloads those versioned objects, and writes them under DIR/input/<case>/ as the
spec's neutral names. On the way every NIfTI descriptive header field is blanked
(db_name, descrip, aux_file, intent_name, and the payload of every header extension),
and the gzip wrapper is rewritten without a file name, so neither the file nor its
header carries the subject or the dataset. No JSON sidecar is shipped.

Prints one line per shipped file (the neutral name, its size and sha256) and, with
--record, writes the same as JSON. Nothing printed names the subject. The subject is
handed to the blind check only through --export-env FILE: one line `BLIND_SUBJECT=...`
in a file only the owner can read, which the caller sources and deletes (see run.yml);
it is never printed to stdout.
"""
import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPHQL = "https://openneuro.org/crn/graphql"

# (offset, length) of the descriptive text fields, by header size.
NIFTI1_TEXT = [(14, 18), (148, 80), (228, 24), (328, 16)]     # db_name, descrip, aux_file, intent_name
NIFTI2_TEXT = [(240, 80), (320, 24), (508, 16)]               # descrip, aux_file, intent_name


def resolve(repo, revision, case):
    """The case's entry in the gated cases.json. The table maps every case to its
    subject, so it is read through a cache of its own, deleted before this returns: the
    Hub's default cache (under HF_HOME or XDG_CACHE_HOME) would leave it on disk for
    the agent that runs next."""
    from huggingface_hub import hf_hub_download
    with tempfile.TemporaryDirectory(prefix="cases-") as cache:
        got = hf_hub_download(repo_id=repo, repo_type="dataset", filename="cases.json",
                              revision=revision, cache_dir=cache)
        with open(got, encoding="utf-8") as fh:
            cases = json.load(fh)["cases"]
    if case not in cases:
        sys.exit("FAIL: %s is not a case of the reference dataset at %s" % (case, revision))
    return cases[case]


def _gql(query):
    req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": query}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    if out.get("errors") or not (out.get("data") or {}).get("snapshot"):
        sys.exit("FAIL: the snapshot's file tree could not be read: %s"
                 % json.dumps(out.get("errors") or out)[:300])
    return out["data"]["snapshot"]["files"] or []


def _tree(dataset, version, tree=None):
    arg = '(tree:"%s")' % tree if tree else ""
    return _gql('{ snapshot(datasetId:"%s", tag:"%s") { files%s { id filename directory size urls } } }'
                % (dataset, version, arg))


def snapshot_files(dataset, version, subject):
    """[(path relative to the subject's directory, versioned url)] for every file of
    the subject in the pinned snapshot."""
    root = [f for f in _tree(dataset, version) if f["filename"] == subject and f["directory"]]
    if not root:
        sys.exit("FAIL: the subject has no directory in the snapshot")
    out, todo = [], [(root[0]["id"], "")]
    while todo:
        tree, prefix = todo.pop()
        for f in _tree(dataset, version, tree):
            rel = prefix + f["filename"]
            if f["directory"]:
                todo.append((f["id"], rel + "/"))
            elif f.get("urls"):
                out.append((rel, f["urls"][0]))
    return out


def pick(files, subject, pattern):
    want = pattern.replace("{subject}", subject)
    hits = [(rel, url) for rel, url in files if fnmatch.fnmatchcase(rel, want)]
    if len(hits) != 1:
        sys.exit("FAIL: input pattern %r matches %d files of the subject, not 1" % (pattern, len(hits)))
    return hits[0]


def download(url):
    with urllib.request.urlopen(url, timeout=900) as r:
        return r.read()


def blank_header(raw):
    """The NIfTI bytes with every descriptive field and extension payload zeroed."""
    if len(raw) < 352:
        sys.exit("FAIL: the input is too short to be a NIfTI file")
    buf = bytearray(raw)
    size = struct.unpack("<i", raw[:4])[0]
    if size == 348:
        fields, vox_offset = NIFTI1_TEXT, int(struct.unpack("<f", raw[108:112])[0])
    elif size == 540:
        fields, vox_offset = NIFTI2_TEXT, int(struct.unpack("<q", raw[168:176])[0])
    elif struct.unpack(">i", raw[:4])[0] in (348, 540):
        sys.exit("FAIL: a big-endian NIfTI header is not handled")
    else:
        sys.exit("FAIL: the input is not a NIfTI file (header size %d)" % size)
    for off, n in fields:
        buf[off:off + n] = b"\0" * n
    # Extensions: 4 extender bytes after the header, then (esize, ecode, data) records
    # up to vox_offset. Their payloads are zeroed; the record sizes are kept.
    pos = size + 4
    if raw[size] != 0 and vox_offset > pos:
        while pos + 8 <= vox_offset:
            esize = struct.unpack("<i", raw[pos:pos + 4])[0]
            if esize < 8 or pos + esize > vox_offset:
                break
            buf[pos + 8:pos + esize] = b"\0" * (esize - 8)
            pos += esize
    return bytes(buf)


def neutral_gzip(data):
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as g:
        g.write(data)
    return out.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--dest", required=True, help="the agent's working directory")
    ap.add_argument("--cases-repo", required=True)
    ap.add_argument("--cases-revision", required=True)
    ap.add_argument("--dataset", required=True, help="the task's dataset id, from tasks.json")
    ap.add_argument("--version", required=True, help="the task's version pin, from tasks.json")
    ap.add_argument("--spec", default=os.path.join(HERE, "input_spec.json"))
    ap.add_argument("--record", help="write the shipped-file record here (JSON)")
    ap.add_argument("--export-env", help="write BLIND_SUBJECT=<subject> to this file (mode 600)")
    a = ap.parse_args(argv)

    spec = json.load(open(a.spec, encoding="utf-8")).get(a.task)
    if not spec:
        sys.exit("FAIL: %s has no entry in %s" % (a.task, a.spec))
    c = resolve(a.cases_repo, a.cases_revision, a.case)
    if c["task"] != a.task:
        sys.exit("FAIL: %s is a case of another task" % a.case)
    if c.get("dataset") and c["dataset"] != a.dataset:
        sys.exit("FAIL: the case's dataset differs from the task's")
    subject = c["subject"]
    files = snapshot_files(a.dataset, a.version, subject)
    if not files:
        sys.exit("FAIL: the subject has no files in the snapshot")
    # The record goes into run.json beside the agent's working directory, so it names
    # the case, the task and the version, not the dataset or the subject.
    record = {"case": a.case, "task": a.task, "version": a.version, "files": []}
    for f in spec["files"]:
        rel, url = pick(files, subject, f["match"])
        raw = download(url)
        if rel.endswith(".gz"):
            raw = gzip.decompress(raw)
        data = neutral_gzip(blank_header(raw)) if f["as"].endswith(".gz") else blank_header(raw)
        out = os.path.join(a.dest, "input", a.case, f["as"])
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(data)
        entry = {"as": f["as"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        record["files"].append(entry)
        print("input: %s  %d bytes  sha256 %s" % (entry["as"], entry["bytes"], entry["sha256"][:12]))
    if a.record:
        with open(a.record, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1)
    if a.export_env:
        fd = os.open(a.export_env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("BLIND_SUBJECT=%s\n" % subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
