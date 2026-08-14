from __future__ import annotations

import unittest

from src.document_reader import extract_document_text, is_supported_document


class DocumentReaderTests(unittest.TestCase):
    def test_reads_plain_text(self) -> None:
        extracted = extract_document_text(filename="note.txt", data=b"hello from a file")
        self.assertEqual(extracted.text, "hello from a file")

    def test_recognizes_supported_document(self) -> None:
        self.assertTrue(is_supported_document(filename="notes.pdf", content_type="application/pdf"))
        self.assertFalse(is_supported_document(filename="song.mp3", content_type="audio/mpeg"))


if __name__ == "__main__":
    unittest.main()
