"""The blinded-run pieces, without the network: run ids, the prompt, the header
blanking, the blind check. Case ids here are below case-100, which real cases never use."""
import gzip
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blind_check  # noqa: E402
import fetch_input  # noqa: E402
import run_ids  # noqa: E402

TASKS = {"categories": {"structural": {"tasks": {
    "structural-brain-extraction": {"prompt": {
        "goal": "Skull-strip the T1-weighted image of {subject}.",
        "dataset": {"source": "openneuro", "id": "ds002790", "version_pin": "2.0.0",
                    "subjects": ["sub-9901", "sub-9902"]},
        "required_output": "A mask at submissions/structural-brain-extraction/{subject}/output.nii.gz."}},
    "diffusion-brain-mask": {"prompt": {
        "goal": "Mask the diffusion image of {subject}.",
        "dataset": {"source": "openneuro", "id": "ds001226", "version_pin": "5.0.1", "subjects": ["sub-CON99"]},
        "required_output": "A mask at submissions/diffusion-brain-mask/{subject}/output.nii.gz."}}}}}}


def nifti1(descrip=b"", db_name=b"", ext=b""):
    hdr = bytearray(348)
    struct.pack_into("<i", hdr, 0, 348)
    hdr[14:14 + len(db_name)] = db_name
    hdr[148:148 + len(descrip)] = descrip
    body = bytes(hdr)
    if ext:
        esize = 8 + len(ext) + (-len(ext) % 16)
        body += b"\1\0\0\0" + struct.pack("<ii", esize, 6) + ext + b"\0" * (esize - 8 - len(ext))
    else:
        body += b"\0\0\0\0"
    struct.pack_into("<f", hdr, 108, float(len(body)))
    return bytes(hdr) + body[348:] + b"\0" * 64


def nifti2(descrip=b""):
    hdr = bytearray(540)
    struct.pack_into("<i", hdr, 0, 540)
    hdr[240:240 + len(descrip)] = descrip
    struct.pack_into("<q", hdr, 168, 544)
    return bytes(hdr) + b"\0\0\0\0" + b"\0" * 64


class RunIds(unittest.TestCase):
    def test_a_case_is_recognised_and_splits_like_a_subject(self):
        self.assertTrue(run_ids.is_case("case-012"))
        for bad in ("sub-9902", "case-28", "case-0120", "case-abc", "", None):
            self.assertFalse(run_ids.is_case(bad))
        self.assertEqual(run_ids.split("structural-brain-extraction@case-012"), ("structural-brain-extraction", "case-012"))
        self.assertEqual(run_ids.output_rel("t@case-012"), "submissions/t/case-012/output.nii.gz")


class Prompt(unittest.TestCase):
    def render(self, run_id):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tasks.json")
            json.dump(TASKS, open(p, "w"))
            r = subprocess.run([sys.executable, os.path.join(HERE, "mkprompt.py"), p, run_id],
                               capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def test_a_case_gets_an_input_line_and_no_dataset_line(self):
        rc, out, err = self.render("structural-brain-extraction@case-012")
        self.assertEqual(rc, 0, err)
        self.assertIn("image of case-012", out)
        self.assertIn("INPUT: a T1-weighted MRI of the head, already in your working directory at `input/case-012/anat/T1w.nii.gz`", out)
        self.assertIn("submissions/structural-brain-extraction/case-012/output.nii.gz", out)
        for s in ("DATASET", "ds002790", "sub-9901", "sub-9902", "openneuro", "Fetch it yourself"):
            self.assertNotIn(s, out)

    def test_a_subject_still_gets_the_dataset_line(self):
        rc, out, _ = self.render("structural-brain-extraction@sub-9902")
        self.assertEqual(rc, 0)
        self.assertIn("DATASET: openneuro ds002790 (version 2.0.0), subject(s) sub-9902. Fetch it yourself.", out)
        self.assertNotIn("INPUT:", out)

    def test_an_unlisted_subject_is_still_refused(self):
        rc, _, err = self.render("structural-brain-extraction@sub-9999")
        self.assertEqual(rc, 1)
        self.assertIn("not a subject", err)

    def test_a_case_of_a_task_without_an_input_spec_is_refused(self):
        rc, _, err = self.render("diffusion-brain-mask@case-013")
        self.assertEqual(rc, 1)
        self.assertIn("input_spec.json", err)


class HeaderBlanking(unittest.TestCase):
    def test_nifti1_text_fields_and_extension_are_zeroed(self):
        raw = nifti1(descrip=b"sub-9902 ds002790", db_name=b"sub-9902", ext=b"dcm2niix sub-9902")
        self.assertIn(b"sub-9902", raw)
        out = fetch_input.blank_header(raw)
        self.assertEqual(len(out), len(raw))
        self.assertNotIn(b"sub-9902", out)
        self.assertNotIn(b"ds002790", out)
        self.assertEqual(out[:4], raw[:4])          # the header size and dims are untouched
        self.assertEqual(out[108:112], raw[108:112])

    def test_nifti2_descrip_is_zeroed(self):
        raw = nifti2(descrip=b"sub-901 ses-003")
        out = fetch_input.blank_header(raw)
        self.assertNotIn(b"sub-901", out)
        self.assertEqual(len(out), len(raw))

    def test_not_a_nifti_is_refused(self):
        with self.assertRaises(SystemExit):
            fetch_input.blank_header(b"\0" * 400)

    def test_neutral_gzip_carries_no_name_or_time(self):
        z = fetch_input.neutral_gzip(b"payload")
        self.assertEqual(gzip.decompress(z), b"payload")
        self.assertEqual(z[3], 0)                   # FLG: no FNAME, no comment
        self.assertEqual(z[4:8], b"\0\0\0\0")       # MTIME 0


class InputKinds(unittest.TestCase):
    """fetch_input's three kinds of input file: under the subject's directory, from the
    dataset's root (a derivative of the subject) and at a fixed address (a template)."""

    TREE = {
        None: [{"id": "d1", "filename": "derivatives", "directory": True},
               {"id": "s1", "filename": "sub-9901", "directory": True}],
        "d1": [{"id": "d2", "filename": "fmriprep", "directory": True}],
        "d2": [{"id": "d3", "filename": "sub-9901", "directory": True}],
        "d3": [{"id": "d4", "filename": "anat", "directory": True}],
        "d4": [{"id": "f1", "filename": "sub-9901_dseg.nii.gz", "directory": False, "urls": ["https://x/dseg"]},
               {"id": "f2", "filename": "sub-9901_dseg.json", "directory": False, "urls": ["https://x/json"]}],
    }

    def setUp(self):
        self.real = (fetch_input._tree, fetch_input.download, fetch_input.resolve, fetch_input.snapshot_files)
        fetch_input._tree = lambda ds, ver, tree=None: self.TREE[tree]

    def tearDown(self):
        (fetch_input._tree, fetch_input.download, fetch_input.resolve, fetch_input.snapshot_files) = self.real

    def test_a_root_pattern_walks_its_directories(self):
        got = fetch_input.dataset_file("ds000000", "1.0.0", "sub-9901",
                                       "/derivatives/fmriprep/{subject}/anat/{subject}_dseg.nii.gz")
        self.assertEqual(got, ("derivatives/fmriprep/sub-9901/anat/sub-9901_dseg.nii.gz", "https://x/dseg"))

    def test_a_root_pattern_must_find_one_file(self):
        with self.assertRaises(SystemExit):     # two files match
            fetch_input.dataset_file("ds000000", "1.0.0", "sub-9901", "/derivatives/fmriprep/{subject}/anat/*")
        with self.assertRaises(SystemExit):     # a directory on the path is missing
            fetch_input.dataset_file("ds000000", "1.0.0", "sub-9901", "/derivatives/qsiprep/{subject}/anat/x.nii.gz")

    def test_all_three_kinds_are_shipped_under_neutral_names(self):
        spec = {"t": {"files": [
            {"match": "anat/{subject}_T1w.nii.gz", "as": "anat/T1w.nii.gz"},
            {"match": "/derivatives/fmriprep/{subject}/anat/{subject}_dseg.nii.gz", "as": "anat/dseg.nii.gz"},
            {"url": "https://templates.example/tpl_T1w.nii.gz", "as": "template/T1w.nii.gz"}], "scan": "x"}}
        fetch_input.resolve = lambda repo, rev, case: {"task": "t", "subject": "sub-9901", "dataset": "ds000000"}
        fetch_input.snapshot_files = lambda ds, ver, s: [("anat/sub-9901_T1w.nii.gz", "https://x/t1")]
        fetched = []

        def download(url):
            fetched.append(url)
            return gzip.compress(nifti1(descrip=b"sub-9901"))
        fetch_input.download = download
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "spec.json")
            with open(sp, "w") as fh:
                json.dump(spec, fh)
            rec = os.path.join(d, "record.json")
            fetch_input.main(["--task", "t", "--case", "case-012", "--dest", d, "--cases-repo", "o/r",
                              "--cases-revision", "v", "--dataset", "ds000000", "--version", "1.0.0",
                              "--spec", sp, "--record", rec])
            root = os.path.join(d, "input", "case-012")
            names = sorted(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
                           for dp, _, fs in os.walk(root) for f in fs)
            self.assertEqual(names, ["anat/T1w.nii.gz", "anat/dseg.nii.gz", "template/T1w.nii.gz"])
            self.assertEqual(fetched, ["https://x/t1", "https://x/dseg", "https://templates.example/tpl_T1w.nii.gz"])
            for n in names:
                with open(os.path.join(root, n), "rb") as fh:
                    self.assertNotIn(b"sub-9901", gzip.decompress(fh.read()))
            with open(rec) as fh:
                self.assertEqual(len(json.load(fh)["files"]), 3)


class CaseTable(unittest.TestCase):
    def test_the_case_table_leaves_no_copy_behind(self):
        """resolve() reads cases.json through a cache of its own and deletes it."""
        import types
        seen = {}

        def fake_download(repo_id, repo_type, filename, revision, cache_dir):
            seen["cache"] = cache_dir
            p = os.path.join(cache_dir, "snap", filename)
            os.makedirs(os.path.dirname(p))
            json.dump({"cases": {"case-012": {"task": "t", "subject": "sub-a"}}}, open(p, "w"))
            return p

        real = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = types.SimpleNamespace(hf_hub_download=fake_download)
        try:
            got = fetch_input.resolve("org/data", "rev", "case-012")
            self.assertEqual(got["subject"], "sub-a")
            self.assertFalse(os.path.exists(seen["cache"]))
            with self.assertRaises(SystemExit):
                fetch_input.resolve("org/data", "rev", "case-099")
        finally:
            if real is None:
                sys.modules.pop("huggingface_hub", None)
            else:
                sys.modules["huggingface_hub"] = real


class BlindCheck(unittest.TestCase):
    def run_check(self, work, env_extra=None, *args):
        env = dict(os.environ, BLIND_SUBJECT="sub-9902", BLIND_DATASET="ds002790", **(env_extra or {}))
        r = subprocess.run([sys.executable, os.path.join(HERE, "blind_check.py"), "--prompt", os.path.join(work, "prompt.txt"),
                            "--scan", os.path.join(work, "input"), "--environment"] + list(args),
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout

    def make(self, work, descrip=b"", name="T1w.nii.gz", prompt="TASK: case-012\n"):
        os.makedirs(os.path.join(work, "input", "case-012", "anat"))
        with open(os.path.join(work, "input", "case-012", "anat", name), "wb") as f:
            f.write(fetch_input.neutral_gzip(nifti1(descrip=descrip)))
        open(os.path.join(work, "prompt.txt"), "w").write(prompt)

    def test_clean_passes(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w)
            rc, out = self.run_check(w)
            self.assertEqual(rc, 0, out)

    def test_subject_inside_a_gzipped_header_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w, descrip=b"sub-9902")
            rc, out = self.run_check(w)
            self.assertEqual(rc, 1)
            self.assertIn("content:", out)
            self.assertNotIn("sub-9902", out)     # the string itself is never printed

    def test_subject_in_a_file_name_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w, name="sub-9902_T1w.nii.gz")
            rc, out = self.run_check(w)
            self.assertEqual(rc, 1)
            self.assertIn("name:", out)

    def test_dataset_in_the_prompt_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w, prompt="TASK: case-012 from ds002790\n")
            rc, out = self.run_check(w)
            self.assertEqual(rc, 1)

    def test_subject_in_the_environment_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w)
            rc, out = self.run_check(w, {"SOMETHING": "sub-9902"})
            self.assertEqual(rc, 1)
            self.assertIn("environment: SOMETHING", out)

    def test_needles_must_be_set(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "blind_check.py")],
                           capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if not k.startswith("BLIND_")})
        self.assertNotEqual(r.returncode, 0)


class Retries(unittest.TestCase):
    """fetch_input's network calls: a transient failure is tried again, the last failure says
    FETCH FAILED and exits 75, an HTTP answer such as 404 is final, and no line names a URL."""

    URL = "https://example.org/dataset-x/sub-x/anat/scan.nii.gz"

    def call(self, failures, final=b"ok"):
        import io
        import urllib.error
        from contextlib import redirect_stdout
        seq = list(failures)
        waits = []

        def fn():
            if seq:
                code = seq.pop(0)
                if code is None:
                    raise urllib.error.URLError("no answer from %s" % self.URL)
                raise urllib.error.HTTPError(self.URL, code, "x", {}, None)
            return final

        out = io.StringIO()
        with redirect_stdout(out):
            try:
                got = fetch_input.with_retries("the scan", fn, sleep=waits.append)
            except SystemExit as e:
                got = ("exit", e.code)
            except Exception as e:  # noqa: BLE001
                got = ("raised", type(e).__name__)
        return got, waits, out.getvalue()

    def test_a_transient_failure_is_tried_again(self):
        got, waits, out = self.call([None, 503])
        self.assertEqual(got, b"ok")
        self.assertEqual(waits, [10, 30])
        self.assertIn("try 2 of 4", out)

    def test_the_last_failure_says_fetch_failed_and_exits_75(self):
        got, waits, out = self.call([503, 503, 429, 502])
        self.assertEqual(got, ("exit", 75))
        self.assertEqual(waits, [10, 30, 60])
        self.assertIn("FETCH FAILED: the scan after 4 tries (HTTPError 502)", out)

    def test_an_http_answer_is_final(self):
        got, waits, _ = self.call([404])
        self.assertEqual((got, waits), (("raised", "HTTPError"), []))

    def test_no_line_names_a_url_a_dataset_or_a_subject(self):
        _, _, out = self.call([None, 503, None, 500])
        for needle in ("example.org", "dataset-x", "sub-x", "scan.nii.gz"):
            self.assertNotIn(needle, out)


if __name__ == "__main__":
    unittest.main()
