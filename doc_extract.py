"""Reading untrusted documents (.docx, .pptx, .pdf) without letting them take the bot down.

A 400 KB ``.docx`` can unpack to hundreds of megabytes of XML (a "zip bomb"), and a PDF stream can inflate just as
far; the parsers then spend minutes and gigabytes on it. Two layers of defence:

1. ``check_archive`` looks at the zip directory *before* any parser sees the bytes and refuses files whose declared
   size is out of proportion. Python's zip reader never yields more than the declared size, so this holds.
2. ``extract_text_isolated`` runs the parser in a child process with a memory ceiling, a CPU ceiling and a wall-clock
   timeout, and without the bot's secrets in its environment. Whatever the checks miss (a PDF flate bomb, a parser
   bug, a pathological page) costs the child, never the bot.

The module doubles as the child's entry point: ``python doc_extract.py <pdf|pptx|docx>`` reads the file from
stdin and prints one JSON line.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import zipfile
from pathlib import Path

log = logging.getLogger("scaramouche.docs")

MiB = 1024 * 1024
KINDS = ("pdf", "pptx", "docx")

MAX_INPUT_BYTES = 16 * MiB            # the bot already refuses uploads above 12 MB
MAX_TEXT_CHARS = 60_000               # more than any caller uses; stops a huge document from becoming a huge reply
MAX_PDF_PAGES = 60
MAX_PPTX_SLIDES = 200
MAX_ARCHIVE_MEMBERS = 2000
MAX_ARCHIVE_UNCOMPRESSED = 64 * MiB   # sum of the declared sizes of every part
MAX_XML_PART = 12 * MiB               # one XML part (document.xml, a slide ...); lxml needs ~10x that in memory
MAX_REPLY_BYTES = 1 * MiB             # what the parent is willing to read back from the child

DEFAULT_MEMORY_MB = 1024
DEFAULT_CPU_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 40.0


class DocumentRejected(Exception):
    """The document is unsafe, too heavy or unreadable. The message is fit to show to the person who sent it."""


# ── layer 1: look before parsing ─────────────────────────────────────────────
def check_archive(data: bytes) -> None:
    """Refuse OOXML (zip) files that would unpack to an unreasonable amount of data."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, EOFError, ValueError, OSError, NotImplementedError) as exc:
        raise DocumentRejected("I couldn't read that file. It may be damaged or password protected.") from exc
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise DocumentRejected("That document contains far too many parts.")
    total = 0
    for info in infos:
        total += info.file_size
        if info.filename.lower().endswith((".xml", ".rels")) and info.file_size > MAX_XML_PART:
            raise DocumentRejected("That document unpacks into far more data than I'm willing to read.")
    if total > MAX_ARCHIVE_UNCOMPRESSED:
        raise DocumentRejected("That document unpacks into far more data than I'm willing to read.")


# ── the readers (blocking; they run in the child, or in an executor as a fallback) ──
def _budget_join(parts, limit: int = MAX_TEXT_CHARS) -> str:
    out: list[str] = []
    used = 0
    for part in parts:
        if not part:
            continue
        out.append(part)
        used += len(part) + 1
        if used >= limit:
            break
    return "\n".join(out)[:limit]


def read_pdf_text(data: bytes) -> str:
    """Text of a PDF (pdfplumber first, pypdf / PyPDF2 as a fallback)."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = _budget_join((page.extract_text() or "") for page in pdf.pages[:MAX_PDF_PAGES])
        if text.strip():
            return text
    except Exception as exc:  # noqa: BLE001 - fall back to the next reader
        log.debug("pdfplumber failed: %s", exc)
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name)
            reader = module.PdfReader(io.BytesIO(data))
            text = _budget_join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
            if text.strip():
                return text
        except Exception as exc:  # noqa: BLE001
            log.debug("%s failed: %s", module_name, exc)
    return ""


def read_pptx_text(data: bytes) -> str:
    check_archive(data)
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))

    def slides():
        for index, slide in enumerate(presentation.slides):
            if index >= MAX_PPTX_SLIDES:
                break
            texts = [shape.text.strip() for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
            if texts:
                yield f"[Slide {index + 1}]\n" + "\n".join(texts)

    return _budget_join(slides())


def read_docx_text(data: bytes) -> str:
    check_archive(data)
    import docx

    document = docx.Document(io.BytesIO(data))

    def blocks():
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if text:
                yield text
        for table in document.tables:  # cheat sheets often live in tables
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    yield row_text

    return _budget_join(blocks())


_READERS = {"pdf": read_pdf_text, "pptx": read_pptx_text, "docx": read_docx_text}


def extract_text(kind: str, data: bytes) -> str:
    """Parse ``data`` in this process (use ``extract_text_isolated`` for anything that came from a user)."""
    reader = _READERS.get(kind)
    if reader is None:
        raise DocumentRejected(f"I can't read {kind!r} files.")
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentRejected("That file is too big.")
    return reader(data)


# ── layer 2: parse in a child process with ceilings ──────────────────────────
_ENV_PASSTHROUGH = (
    "PATH", "PYTHONPATH", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR",
)


def _worker_env() -> dict[str, str]:
    """Only the variables Python needs to start; tokens and API keys stay out of the process that opens the file."""
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _env_number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _apply_limits() -> None:
    """Called inside the child before it touches the file."""
    try:
        import resource
    except ImportError:  # Windows: only the wall-clock timeout applies
        return
    memory = int(_env_number("DOC_WORKER_MEMORY_MB", DEFAULT_MEMORY_MB)) * MiB
    cpu = int(_env_number("DOC_WORKER_CPU_SECONDS", DEFAULT_CPU_SECONDS))
    for name, value in (("RLIMIT_AS", memory), ("RLIMIT_CPU", cpu), ("RLIMIT_CORE", 0)):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass


async def _run_child(kind: str, data: bytes, timeout: float) -> str:
    if not sys.executable:
        raise OSError("no Python interpreter path to start the document worker with")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), kind,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=_worker_env(),
    )

    async def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child already gave up; its exit status says why
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    async def collect() -> bytes:
        assert proc.stdout is not None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REPLY_BYTES:
                raise DocumentRejected("That document produced far too much text.")
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        _, raw = await asyncio.wait_for(asyncio.gather(feed(), collect()), timeout)
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError as exc:
        raise DocumentRejected("That document took too long to read.") from exc
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    if proc.returncode != 0 and not raw:
        # killed by a signal (the memory / CPU ceiling) or crashed
        log.warning("document worker for %s exited with status %s", kind, proc.returncode)
        raise DocumentRejected("That document is too heavy for me to read.")
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DocumentRejected("I couldn't read that file. It may be damaged or password protected.") from exc
    if not reply.get("ok"):
        raise DocumentRejected(str(reply.get("error") or "I couldn't read that file."))
    return str(reply.get("text") or "")


async def extract_text_isolated(kind: str, data: bytes, *, timeout: float | None = None) -> str:
    """Parse an uploaded document in a resource-limited child process. Raises ``DocumentRejected``."""
    if kind not in _READERS:
        raise DocumentRejected(f"I can't read {kind!r} files.")
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentRejected("That file is too big.")
    if kind in ("pptx", "docx"):
        check_archive(data)  # cheap, and spares a process start for the obvious bombs
    wait = timeout or _env_number("DOC_WORKER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    try:
        return await _run_child(kind, data, wait)
    except (OSError, NotImplementedError) as exc:
        # No way to start a child here (restricted host): parse in a thread, with the archive checks above.
        log.warning("document worker unavailable (%s: %s); parsing in-process", type(exc).__name__, exc)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(None, extract_text, kind, data), wait)


# ── the child's entry point ──────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    _apply_limits()
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        text = extract_text(kind, data)
        reply = {"ok": True, "text": text}
    except DocumentRejected as exc:
        reply = {"ok": False, "error": str(exc)}
    except MemoryError:
        reply = {"ok": False, "error": "That document is too heavy for me to read."}
    except Exception as exc:  # noqa: BLE001 - whatever the parser choked on, report it instead of a traceback
        reply = {"ok": False, "error": f"I couldn't read that file ({type(exc).__name__}). It may be damaged or password protected."}
    sys.stdout.write(json.dumps(reply, ensure_ascii=True))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
