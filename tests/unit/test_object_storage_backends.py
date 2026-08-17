"""Standard-library unit tests for the object_storage backends
(local.LocalFilesystemBackend, memory.InMemoryBackend): both must uphold
the exact same immutability contract (base.StorageBackend's docstring),
proven here against BOTH backends via a shared mixin so neither backend
can silently drift from the other's behavior.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.object_storage.base import ObjectAlreadyExistsError, ObjectNotFoundError  # noqa: E402
from tuva_ingest.object_storage.local import LocalFilesystemBackend  # noqa: E402
from tuva_ingest.object_storage.memory import InMemoryBackend  # noqa: E402


class _BackendContractMixin:
    """Shared assertions run against whatever `self.backend` is."""

    def test_put_then_get_round_trips(self):
        meta = self.backend.put("a/b.txt", b"hello world")
        self.assertEqual(meta.size_bytes, 11)
        self.assertEqual(self.backend.get("a/b.txt"), b"hello world")

    def test_exists_false_before_put_true_after(self):
        self.assertFalse(self.backend.exists("x.txt"))
        self.backend.put("x.txt", b"data")
        self.assertTrue(self.backend.exists("x.txt"))

    def test_get_missing_key_raises(self):
        with self.assertRaises(ObjectNotFoundError):
            self.backend.get("does/not/exist")

    def test_head_missing_key_returns_none(self):
        self.assertIsNone(self.backend.head("does/not/exist"))

    def test_head_returns_size_and_sha256(self):
        self.backend.put("k.txt", b"abc")
        meta = self.backend.head("k.txt")
        self.assertEqual(meta.size_bytes, 3)
        import hashlib

        self.assertEqual(meta.sha256, hashlib.sha256(b"abc").hexdigest())

    def test_identical_content_rewrite_is_a_safe_no_op(self):
        self.backend.put("k.txt", b"same content")
        # Writing the exact same bytes again must not raise.
        meta = self.backend.put("k.txt", b"same content")
        self.assertEqual(self.backend.get("k.txt"), b"same content")
        self.assertEqual(meta.size_bytes, len(b"same content"))

    def test_different_content_rewrite_raises(self):
        self.backend.put("k.txt", b"version 1")
        with self.assertRaises(ObjectAlreadyExistsError):
            self.backend.put("k.txt", b"version 2 -- different")
        # The original content must be unchanged after the refused write.
        self.assertEqual(self.backend.get("k.txt"), b"version 1")

    def test_list_returns_keys_under_prefix_only(self):
        self.backend.put("raw/a/1.txt", b"1")
        self.backend.put("raw/a/2.txt", b"2")
        self.backend.put("raw/b/1.txt", b"3")
        self.backend.put("other/1.txt", b"4")
        found = self.backend.list("raw/a/")
        self.assertEqual(sorted(found), ["raw/a/1.txt", "raw/a/2.txt"])

    def test_list_empty_prefix_returns_empty(self):
        self.assertEqual(self.backend.list("nothing/here/"), [])


class TestInMemoryBackend(_BackendContractMixin, unittest.TestCase):
    def setUp(self):
        self.backend = InMemoryBackend()


class TestLocalFilesystemBackend(_BackendContractMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.backend = LocalFilesystemBackend(Path(self._tmp.name))

    def test_rejects_path_traversal_key(self):
        from tuva_ingest.errors import ObjectStorageError

        with self.assertRaises(ObjectStorageError):
            self.backend.put("../../etc/passwd", b"evil")


if __name__ == "__main__":
    unittest.main()
