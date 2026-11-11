"""Reading an uploaded file, away from the server's own work (ADR-070).

A file is hostile until read: a 65 KB Word file can take 45 seconds and two
gigabytes to parse, and a 150 KB one never finishes. Read on the event loop,
that froze every other request; read in a thread, it would still share the
process's memory. So each file is read in a child process that is given a
time limit and, where the platform enforces one, a memory limit — and is
killed at either. The server only waits.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import weakref
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.utils import import_worker
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: "process" in production; "inline" reads in a thread of this process (tests, and a host that cannot spawn).
ISOLATION: str = os.getenv("IMPORT_PARSER_ISOLATION", "process")
TIMEOUT_SECONDS: float = float(os.getenv("IMPORT_PARSER_TIMEOUT_SECONDS", "30"))
#: 0 leaves the child's memory unbounded. Enforced on Linux; set it once measured on the host.
MEMORY_MB: int = int(os.getenv("IMPORT_PARSER_MEMORY_MB", "0"))
CONCURRENCY: int = max(1, int(os.getenv("IMPORT_PARSER_CONCURRENCY", "2")))

IMPORT_TOO_COMPLEX = "import_too_complex"
IMPORT_UNREADABLE = "import_unreadable"

_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


class ImportTooComplex(Exception):
    """The file could not be read within the bounds a file is given."""


@dataclass
class ParsedImport:
    pages: List[Dict[str, Any]] = field(default_factory=list)
    pictures: List[Dict[str, Any]] = field(default_factory=list)
    words: int = 0
    content_bytes: int = 0
    #: Why there are no pages, as a stable code; None when there are.
    refusal: str | None = None


def _gate_for_loop() -> asyncio.Semaphore:
    """At most `CONCURRENCY` files are read at once; a semaphore belongs to the loop that made it."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Semaphore(CONCURRENCY)
    return gate


def _read_in_child(filename: str, file_content: bytes) -> Dict[str, Any]:
    """Blocking: one child, one file, killed at the time limit. Runs in a worker thread."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    memory = MEMORY_MB * 1024 * 1024 if MEMORY_MB > 0 else None
    child = context.Process(target=import_worker.run, args=(sender, memory, filename, file_content), daemon=True)
    child.start()
    sender.close()
    try:
        if not receiver.poll(TIMEOUT_SECONDS):
            logger.warning("Import of %r stopped: not read within %ss", filename, TIMEOUT_SECONDS)
            return {"too_complex": True}
        return receiver.recv()
    except (EOFError, OSError):
        # The child died without an answer: out of memory, or a reader crashed the interpreter.
        logger.warning("Import of %r stopped: the reader exited with %s", filename, child.exitcode)
        return {"too_complex": True}
    finally:
        receiver.close()
        if child.is_alive():
            child.kill()
        child.join(5)


async def parse(filename: str, file_content: bytes) -> ParsedImport:
    """The file's pages, pictures, words and stored size. Raises `ImportTooComplex`."""
    read = import_worker.read_file if ISOLATION == "inline" else _read_in_child
    async with _gate_for_loop():
        answer = await asyncio.to_thread(read, filename, file_content)
    if answer.get("too_complex"):
        raise ImportTooComplex(filename)
    if "error" in answer:
        logger.error("Import of %r failed in the reader: %s", filename, answer["error"])
        return ParsedImport(refusal=IMPORT_UNREADABLE)
    return ParsedImport(answer["pages"], answer["pictures"], answer["words"], answer["content_bytes"], answer.get("refusal"))


async def to_lexical(html_content: str) -> str:
    """The stored JSON of a document, converted off the event loop."""
    return await asyncio.to_thread(import_worker.to_lexical, html_content)
