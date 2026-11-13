"""key_sources.py: a hashed name is found inside URLs, paths and repository ids; no file a
run carries holds one of the hashed names in clear."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import key_sources as ks  # noqa: E402

PLACEHOLDER = "placeholder-key-source"
MINE = frozenset({ks.digest(PLACEHOLDER), ks.digest("zz9qq")})


class NamedSource(unittest.TestCase):
    def test_found_in_the_shapes_a_command_takes(self):
        for line in ("$ hf download someone/placeholder-key-source --repo-type dataset",
                     "% WebFetch https://huggingface.co/datasets/someone/placeholder-key-source/resolve/main/x.json",
                     "$ git clone https://example.org/someone/placeholder-key-source.git",
                     "$ curl -sL https://files.osf.io/v1/resources/zz9qq/providers/osfstorage/",
                     "◈ Web search: osf zz9qq reference masks",
                     "snapshot_download('someone/PLACEHOLDER-KEY-SOURCE')"):
            self.assertTrue(ks.named_source(line, MINE), line)

    def test_not_found_in_lookalikes(self):
        for line in ("$ ls placeholder-key-sources", "$ echo placeholder key source",
                     "$ cat zz9qqx.txt", "$ curl https://osf.io/zz9q/"):
            self.assertFalse(ks.named_source(line, MINE), line)

    def test_the_digest_is_salted(self):
        import hashlib
        self.assertNotEqual(ks.digest("abc"), hashlib.sha256(b"abc").hexdigest())

    def test_no_carried_file_names_a_hashed_source_in_clear(self):
        # Every script, spec and text file of the harness directory except the tests, which
        # a run never carries: none may contain a token whose hash is a hidden source.
        hits = []
        for f in sorted(os.listdir(HERE)):
            p = os.path.join(HERE, f)
            if (not os.path.isfile(p) or f.startswith("test_")
                    or not f.endswith((".py", ".sh", ".json", ".txt", ".md"))):
                continue
            text = open(p, encoding="utf-8", errors="replace").read()
            if ks.named_source(text):
                hits.append(f)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
