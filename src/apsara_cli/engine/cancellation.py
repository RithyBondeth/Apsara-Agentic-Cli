"""Cooperative subprocess cancellation; finish cleanup before checkpointing."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import os
import subprocess
import threading
import time

_cancel_event: ContextVar[threading.Event | None] = ContextVar("apsara_cancel_event", default=None)


class CommandCancelled(RuntimeError):
    pass


@contextmanager
def cancellation_context():
    event = threading.Event()
    token = _cancel_event.set(event)
    try:
        yield event
    finally:
        _cancel_event.reset(token)


async def run_interruptible(function, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        event = _cancel_event.get()
        if event is not None:
            event.set()
        # The checkpoint must reflect the final stopped command, not a live thread.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise


def run_command(command, *, cwd, timeout: float, env=None, shell=False, input_text: str | None = None) -> subprocess.CompletedProcess:
    from apsara_cli.engine.processes import ProcessManager
    event = _cancel_event.get()
    if event is not None and event.is_set():
        raise CommandCancelled("Command cancelled before launch.")
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=cwd, env=env, shell=shell,
                               stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, **options)
    deadline = time.monotonic() + timeout
    first = True
    complete = False
    try:
        while True:
            if event is not None and event.is_set():
                raise CommandCancelled("Command cancelled by user.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                supplied = input_text if first else None
                first = False
                stdout, stderr = process.communicate(input=supplied, timeout=min(remaining, 0.1))
                complete = True
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if not complete or process.poll() is None:
            ProcessManager._terminate_tree(process, force=True)
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if process.stdin is not None:
            process.stdin.close()
