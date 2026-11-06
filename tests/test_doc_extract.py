"""Uploaded documents are untrusted: bombs are refused cheaply, and parsing happens in a resource-limited child."""

import importlib.util
import io
import os
import sys
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doc_extract  # noqa: E402

HAVE_DOCX = importlib.util.find_spec("docx") is not None
HAVE_PPTX = importlib.util.find_spec("pptx") is not None

def pdf_document(*objects: bytes) -> bytes:
    """A small but well-formed PDF: objects 1..n plus the cross-reference table and trailer every reader expects.
    PyPDF2, the PDF reader on the Oracle VM, refuses a file without "startxref" (pdfplumber guesses its way through)."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % number + body + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)
    return bytes(out)


_CATALOG = b"<</Type/Catalog/Pages 2 0 R>>"
_PAGES = b"<</Type/Pages/Kids[3 0 R]/Count 1>>"
_PAGE = b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
_FONT = b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<</Length %d%s>>stream\n" % (len(data), extra) + data + b"\nendstream"


PDF = pdf_document(_CATALOG, _PAGES, _PAGE, _stream(b"BT /F1 18 Tf 20 100 Td (Hello Teyvat) Tj ET"), _FONT)


def make_docx(paragraphs=("Photosynthesis converts light to sugar.",)):
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Cell A", "Cell B"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_pptx():
    import pptx

    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text, slide.placeholders[1].text = "Title", "Slide body text"
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def zip_with_part(name: str, size: int, base: bytes) -> bytes:
    """``base`` with ``name`` replaced by ``size`` bytes of highly compressible XML (a handful of KB on disk)."""
    source = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == name:
                head = data.split(b"<w:body>")[0] + b"<w:body>" if b"<w:body>" in data else data[: data.index(b">") + 1]
                filler = b"<w:p><w:r><w:t>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</w:t></w:r></w:p>" * (size // 60)
                data = head + filler + (b"</w:body></w:document>" if b"<w:body>" in data else b"")
            archive.writestr(item.filename, data)
    return out.getvalue()


def pdf_flate_bomb(inflated_mb: int) -> bytes:
    compressor = zlib.compressobj(9)
    chunks = [compressor.compress(b" " * (1024 * 1024)) for _ in range(inflated_mb)] + [compressor.flush()]
    # the page has a font, so every PDF library really inflates the stream instead of skipping the page
    return pdf_document(_CATALOG, _PAGES, _PAGE, _stream(b"".join(chunks), b"/Filter/FlateDecode"), _FONT)


@unittest.skipUnless(HAVE_DOCX, "python-docx not installed")
class ArchiveGuardTests(unittest.TestCase):
    def test_a_small_file_that_unpacks_to_100_mb_is_refused_instantly(self):
        bomb = zip_with_part("word/document.xml", 100_000_000, make_docx())
        self.assertLess(len(bomb), 1_000_000)  # well under any upload limit
        started = time.perf_counter()
        with self.assertRaises(doc_extract.DocumentRejected) as caught:
            doc_extract.check_archive(bomb)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertIn("far more data", str(caught.exception))
        with self.assertRaises(doc_extract.DocumentRejected):
            doc_extract.read_docx_text(bomb)  # the reader checks too, whoever calls it

    def test_many_parts_and_a_large_total_are_refused_too(self):
        many = io.BytesIO()
        with zipfile.ZipFile(many, "w") as archive:
            for i in range(doc_extract.MAX_ARCHIVE_MEMBERS + 1):
                archive.writestr(f"p{i}.txt", b"x")
        with self.assertRaises(doc_extract.DocumentRejected):
            doc_extract.check_archive(many.getvalue())
        total = io.BytesIO()
        with zipfile.ZipFile(total, "w", zipfile.ZIP_DEFLATED) as archive:
            for i in range(6):  # six 11 MB media parts: each is allowed alone, together they are not
                archive.writestr(f"media/{i}.bin", b"\0" * (11 * 1024 * 1024))
        with self.assertRaises(doc_extract.DocumentRejected):
            doc_extract.check_archive(total.getvalue())

    def test_not_a_zip_is_refused_politely(self):
        with self.assertRaises(doc_extract.DocumentRejected):
            doc_extract.check_archive(b"definitely not a zip file")

    def test_ordinary_documents_pass(self):
        doc_extract.check_archive(make_docx())
        if HAVE_PPTX:
            doc_extract.check_archive(make_pptx())


@unittest.skipUnless(HAVE_DOCX and HAVE_PPTX, "python-docx / python-pptx not installed")
class ReaderTests(unittest.TestCase):
    def test_readers_return_the_text(self):
        text = doc_extract.extract_text("docx", make_docx())
        self.assertIn("Photosynthesis", text)
        self.assertIn("Cell A | Cell B", text)
        self.assertIn("Slide body text", doc_extract.extract_text("pptx", make_pptx()))
        self.assertIn("Teyvat", doc_extract.extract_text("pdf", PDF))

    def test_output_is_capped(self):
        long_doc = make_docx(["word " * 200] * 1000)  # ~1 MB of text
        text = doc_extract.extract_text("docx", long_doc)
        self.assertLessEqual(len(text), doc_extract.MAX_TEXT_CHARS)
        self.assertGreater(len(text), 10_000)

    def test_unknown_kinds_and_oversized_input_are_refused(self):
        with self.assertRaises(doc_extract.DocumentRejected):
            doc_extract.extract_text("exe", b"x")
        with self.assertRaises(doc_extract.DocumentRejected):
            doc_extract.extract_text("pdf", b"0" * (doc_extract.MAX_INPUT_BYTES + 1))


@unittest.skipUnless(HAVE_DOCX and HAVE_PPTX, "python-docx / python-pptx not installed")
class IsolatedWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_documents_are_read_in_a_child_process(self):
        self.assertIn("Photosynthesis", await doc_extract.extract_text_isolated("docx", make_docx()))
        self.assertIn("Slide body text", await doc_extract.extract_text_isolated("pptx", make_pptx()))
        self.assertIn("Teyvat", await doc_extract.extract_text_isolated("pdf", PDF))

    async def test_the_child_never_sees_the_bots_secrets(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "super-secret", "GROQ_API_KEY": "also-secret", "PATH": os.environ.get("PATH", "")}):
            env = doc_extract._worker_env()
        self.assertNotIn("DISCORD_TOKEN", env)
        self.assertNotIn("GROQ_API_KEY", env)
        self.assertIn("PATH", env)

    async def test_the_bomb_is_refused_before_a_process_is_even_started(self):
        bomb = zip_with_part("word/document.xml", 100_000_000, make_docx())
        started = time.perf_counter()
        with patch.object(doc_extract.asyncio, "create_subprocess_exec", side_effect=AssertionError("no child needed")):
            with self.assertRaises(doc_extract.DocumentRejected):
                await doc_extract.extract_text_isolated("docx", bomb)
        self.assertLess(time.perf_counter() - started, 1.0)

    async def test_a_pdf_that_inflates_to_gigabytes_is_contained(self):
        bomb = pdf_flate_bomb(600)  # 600 MB once inflated (1.2 GB of RSS without the ceiling), ~600 KB on disk
        self.assertLess(len(bomb), 5_000_000)
        with patch.dict(os.environ, {"DOC_WORKER_MEMORY_MB": "300"}):
            started = time.perf_counter()
            try:
                text = await doc_extract.extract_text_isolated("pdf", bomb)
            except doc_extract.DocumentRejected:
                text = ""  # also fine: refused
            self.assertEqual(text.strip(), "")
            self.assertLess(time.perf_counter() - started, 30)
        # ...and the parent is untouched: it can still read a normal file right afterwards
        self.assertIn("Teyvat", await doc_extract.extract_text_isolated("pdf", PDF))

    async def test_a_child_that_takes_too_long_is_killed(self):
        started = time.perf_counter()
        with self.assertRaises(doc_extract.DocumentRejected) as caught:
            await doc_extract.extract_text_isolated("pdf", PDF, timeout=0.001)
        self.assertIn("too long", str(caught.exception))
        self.assertLess(time.perf_counter() - started, 5)

    async def test_without_a_way_to_start_a_child_it_parses_in_a_thread(self):
        with patch.object(doc_extract.asyncio, "create_subprocess_exec", side_effect=PermissionError("no fork here")):
            self.assertIn("Teyvat", await doc_extract.extract_text_isolated("pdf", PDF))

    async def test_a_crashing_child_is_reported_not_raised(self):
        async def crash(kind, data, timeout):
            raise doc_extract.DocumentRejected("That document is too heavy for me to read.")

        with patch.object(doc_extract, "_run_child", crash):
            with self.assertRaises(doc_extract.DocumentRejected):
                await doc_extract.extract_text_isolated("pdf", PDF)


if __name__ == "__main__":
    unittest.main()
