"""The blinded-run pieces, without the network: run ids, the prompt, the header
blanking, the blind check."""
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
                    "subjects": ["sub-0004", "sub-0008"]},
        "required_output": "A mask at submissions/structural-brain-extraction/{subject}/output.nii.gz."}},
    "diffusion-brain-mask": {"prompt": {
        "goal": "Mask the diffusion image of {subject}.",
        "dataset": {"source": "openneuro", "id": "ds001226", "version_pin": "5.0.1", "subjects": ["sub-CON02"]},
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
        self.assertTrue(run_ids.is_case("case-288"))
        for bad in ("sub-0008", "case-28", "case-2880", "case-abc", "", None):
            self.assertFalse(run_ids.is_case(bad))
        self.assertEqual(run_ids.split("structural-brain-extraction@case-288"), ("structural-brain-extraction", "case-288"))
        self.assertEqual(run_ids.output_rel("t@case-288"), "submissions/t/case-288/output.nii.gz")


class Prompt(unittest.TestCase):
    def render(self, run_id):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tasks.json")
            json.dump(TASKS, open(p, "w"))
            r = subprocess.run([sys.executable, os.path.join(HERE, "mkprompt.py"), p, run_id],
                               capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def test_a_case_gets_an_input_line_and_no_dataset_line(self):
        rc, out, err = self.render("structural-brain-extraction@case-288")
        self.assertEqual(rc, 0, err)
        self.assertIn("image of case-288", out)
        self.assertIn("INPUT: a T1-weighted MRI of the head, already in your working directory at `input/case-288/anat/T1w.nii.gz`", out)
        self.assertIn("submissions/structural-brain-extraction/case-288/output.nii.gz", out)
        for s in ("DATASET", "ds002790", "sub-0004", "sub-0008", "openneuro", "Fetch it yourself"):
            self.assertNotIn(s, out)

    def test_a_subject_still_gets_the_dataset_line(self):
        rc, out, _ = self.render("structural-brain-extraction@sub-0008")
        self.assertEqual(rc, 0)
        self.assertIn("DATASET: openneuro ds002790 (version 2.0.0), subject(s) sub-0008. Fetch it yourself.", out)
        self.assertNotIn("INPUT:", out)

    def test_an_unlisted_subject_is_still_refused(self):
        rc, _, err = self.render("structural-brain-extraction@sub-9999")
        self.assertEqual(rc, 1)
        self.assertIn("not a subject", err)

    def test_a_case_of_a_task_without_an_input_spec_is_refused(self):
        rc, _, err = self.render("diffusion-brain-mask@case-893")
        self.assertEqual(rc, 1)
        self.assertIn("input_spec.json", err)


class HeaderBlanking(unittest.TestCase):
    def test_nifti1_text_fields_and_extension_are_zeroed(self):
        raw = nifti1(descrip=b"sub-0008 ds002790", db_name=b"sub-0008", ext=b"dcm2niix sub-0008")
        self.assertIn(b"sub-0008", raw)
        out = fetch_input.blank_header(raw)
        self.assertEqual(len(out), len(raw))
        self.assertNotIn(b"sub-0008", out)
        self.assertNotIn(b"ds002790", out)
        self.assertEqual(out[:4], raw[:4])          # the header size and dims are untouched
        self.assertEqual(out[108:112], raw[108:112])

    def test_nifti2_descrip_is_zeroed(self):
        raw = nifti2(descrip=b"sub-025 ses-003")
        out = fetch_input.blank_header(raw)
        self.assertNotIn(b"sub-025", out)
        self.assertEqual(len(out), len(raw))

    def test_not_a_nifti_is_refused(self):
        with self.assertRaises(SystemExit):
            fetch_input.blank_header(b"\0" * 400)

    def test_neutral_gzip_carries_no_name_or_time(self):
        z = fetch_input.neutral_gzip(b"payload")
        self.assertEqual(gzip.decompress(z), b"payload")
        self.assertEqual(z[3], 0)                   # FLG: no FNAME, no comment
        self.assertEqual(z[4:8], b"\0\0\0\0")       # MTIME 0


class CaseTable(unittest.TestCase):
    def test_the_case_table_leaves_no_copy_behind(self):
        """resolve() reads cases.json through a cache of its own and deletes it."""
        import types
        seen = {}

        def fake_download(repo_id, repo_type, filename, revision, cache_dir):
            seen["cache"] = cache_dir
            p = os.path.join(cache_dir, "snap", filename)
            os.makedirs(os.path.dirname(p))
            json.dump({"cases": {"case-288": {"task": "t", "subject": "sub-0004"}}}, open(p, "w"))
            return p

        real = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = types.SimpleNamespace(hf_hub_download=fake_download)
        try:
            got = fetch_input.resolve("org/data", "rev", "case-288")
            self.assertEqual(got["subject"], "sub-0004")
            self.assertFalse(os.path.exists(seen["cache"]))
            with self.assertRaises(SystemExit):
                fetch_input.resolve("org/data", "rev", "case-999")
        finally:
            if real is None:
                sys.modules.pop("huggingface_hub", None)
            else:
                sys.modules["huggingface_hub"] = real


class BlindCheck(unittest.TestCase):
    def run_check(self, work, env_extra=None, *args):
        env = dict(os.environ, BLIND_SUBJECT="sub-0008", BLIND_DATASET="ds002790", **(env_extra or {}))
        r = subprocess.run([sys.executable, os.path.join(HERE, "blind_check.py"), "--prompt", os.path.join(work, "prompt.txt"),
                            "--scan", os.path.join(work, "input"), "--environment"] + list(args),
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout

    def make(self, work, descrip=b"", name="T1w.nii.gz", prompt="TASK: case-288\n"):
        os.makedirs(os.path.join(work, "input", "case-288", "anat"))
        with open(os.path.join(work, "input", "case-288", "anat", name), "wb") as f:
            f.write(fetch_input.neutral_gzip(nifti1(descrip=descrip)))
        open(os.path.join(work, "prompt.txt"), "w").write(prompt)

    def test_clean_passes(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w)
            rc, out = self.run_check(w)
            self.assertEqual(rc, 0, out)

    def test_subject_inside_a_gzipped_header_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w, descrip=b"sub-0008")
            rc, out = self.run_check(w)
            self.assertEqual(rc, 1)
            self.assertIn("content:", out)
            self.assertNotIn("sub-0008", out)     # the string itself is never printed

    def test_subject_in_a_file_name_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w, name="sub-0008_T1w.nii.gz")
            rc, out = self.run_check(w)
            self.assertEqual(rc, 1)
            self.assertIn("name:", out)

    def test_dataset_in_the_prompt_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w, prompt="TASK: case-288 from ds002790\n")
            rc, out = self.run_check(w)
            self.assertEqual(rc, 1)

    def test_subject_in_the_environment_fails(self):
        with tempfile.TemporaryDirectory() as w:
            self.make(w)
            rc, out = self.run_check(w, {"SOMETHING": "sub-0008"})
            self.assertEqual(rc, 1)
            self.assertIn("environment: SOMETHING", out)

    def test_needles_must_be_set(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "blind_check.py")],
                           capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if not k.startswith("BLIND_")})
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
