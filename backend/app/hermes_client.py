"""Hermes ACP client: a pool of agent processes keyed by app session.

There are no user accounts in MAI-Agent: the unit of context is the app
session id (a UUID string). Each app session maps to one Hermes session
(``hermes_session_id``); the conversation transcript and agent memory live
in Hermes' own session store (``state.db``). A stored session is restored
with ACP ``session/load``, which per the ACP spec replays the prior
transcript back to the client as ``session_update`` notifications — the
backend captures that replay as its history source instead of duplicating
the transcript in its own database.
"""

import asyncio
import logging
import os
import shlex
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from acp import PROTOCOL_VERSION, Client, RequestError, connect_to_agent
from acp.schema import (
    AgentMessageChunk,
    ClientCapabilities,
    Implementation,
    HttpMcpServer,
    RequestPermissionResponse,
    ImageContentBlock,
    TextContentBlock,
    UserMessageChunk,
)

from app.config import get_settings
from app.safe_logging import log_event

logger = logging.getLogger(__name__)


class HermesUnavailableError(RuntimeError):
    """Raised when the configured Hermes ACP process cannot be used."""


class HermesSessionLoadError(HermesUnavailableError):
    """The agent explicitly rejected loading a stored session."""


#: Instruction prepended to the ACP prompt when an image is attached.
#: Without it, the model may try to locate tooling for opening the image
#: instead of analyzing the inline pixels it already received.
IMAGE_INLINE_INSTRUCTION = (
    "К сообщению прикреплено изображение; оно передано тебе inline — пиксели доступны. "
    "Проанализируй изображение непосредственно, не вызывая инструменты и не читая файлы. "
    "Ответь по его содержимому.\n\n"
)


def with_image_instruction(text: str) -> str:
    """Return the prompt text with the inline-image instruction prepended."""
    return IMAGE_INLINE_INSTRUCTION + text


class HermesCallbacks(Client):
    """ACP client callbacks.

    During a prompt turn, assistant text chunks of the current turn are
    collected into ``messages``. While a stored session is being loaded
    (``replaying`` flag), the spec-mandated history replay is collected into
    ``replay`` as merged user/assistant messages — that is the backend's
    history source (Hermes is the single owner of the transcript).
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.replay: list[dict[str, str]] = []
        self.replaying = False

    def _append_replay(self, role: str, delta: str) -> None:
        if self.replay and self.replay[-1]["role"] == role:
            self.replay[-1]["content"] += delta
        else:
            self.replay.append({"role": role, "content": delta})

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self.replaying:
            if isinstance(update, UserMessageChunk) and isinstance(update.content, TextContentBlock):
                self._append_replay("user", update.content.text)
            elif isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
                self._append_replay("assistant", update.content.text)
            return
        if isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
            self.messages.append(update.content.text)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any, **kwargs: Any):
        return RequestPermissionResponse(outcome={"outcome": "cancelled"})

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)


@dataclass
class HermesProcess:
    process: asyncio.subprocess.Process
    connection: Any
    callbacks: HermesCallbacks
    session_id: str
    last_used: float = field(default_factory=monotonic)
    #: Transcript for UI history: seeded from the session/load replay and
    #: appended to after each prompt turn.
    history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SessionLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class HermesClientManager:
    def __init__(self) -> None:
        self._processes: dict[str, HermesProcess] = {}
        self._locks: dict[str, SessionLock] = {}
        self._starting = 0

    @asynccontextmanager
    async def _session(self, session_id: str):
        entry = self._locks.setdefault(session_id, SessionLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                del self._locks[session_id]

    async def _stop_process(self, process: asyncio.subprocess.Process, connection: Any) -> None:
        if connection is not None:
            with suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=2)
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
    async def _start(self, session_id: str, hermes_session_id: str | None) -> HermesProcess:
        started_at = monotonic()
        settings = get_settings()
        if settings.acp_transport != "stdio":
            log_event(
                logger,
                logging.ERROR,
                "acp_process_start",
                channel="acp",
                outcome="rejected",
                reason_code="unsupported_transport",
                session_id=session_id,
            )
            raise HermesUnavailableError("Only ACP stdio transport is supported")
        try:
            command = shlex.split(settings.hermes_command)
        except ValueError as exc:
            log_event(
                logger,
                logging.ERROR,
                "acp_process_start",
                channel="acp",
                outcome="rejected",
                reason_code="invalid_command",
                session_id=session_id,
            )
            raise HermesUnavailableError("Invalid HERMES_COMMAND") from exc
        if not command:
            log_event(
                logger,
                logging.ERROR,
                "acp_process_start",
                channel="acp",
                outcome="rejected",
                reason_code="empty_command",
                session_id=session_id,
            )
            raise HermesUnavailableError("HERMES_COMMAND is empty")
        cwd = Path(settings.hermes_cwd).resolve()
        if not cwd.is_dir():
            log_event(
                logger,
                logging.ERROR,
                "acp_process_start",
                channel="acp",
                outcome="rejected",
                reason_code="missing_cwd",
                session_id=session_id,
            )
            raise HermesUnavailableError(f"Hermes working directory does not exist: {cwd}")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=50 * 1024 * 1024,
                env=os.environ.copy(),
            )
        except OSError as exc:
            log_event(
                logger,
                logging.ERROR,
                "acp_process_start",
                channel="acp",
                outcome="error",
                reason_code="spawn_os_error",
                session_id=session_id,
            )
            raise HermesUnavailableError("Unable to start Hermes ACP") from exc

        connection = None
        try:
            if process.stdin is None or process.stdout is None:
                raise HermesUnavailableError("Hermes ACP process did not expose stdio pipes")
            callbacks = HermesCallbacks()
            connection = connect_to_agent(
                callbacks, process.stdin, process.stdout, use_unstable_protocol=True
            )
            timeout = settings.acp_startup_timeout_seconds
            await asyncio.wait_for(
                connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="mai-agent-backend", version="0.1.0"),
                ),
                timeout=timeout,
            )
            mcp_server = HttpMcpServer(
                name="mai-agent",
                url=settings.mcp_public_url,
                headers=[],
                type="http",
            )
            if hermes_session_id is None:
                session = await asyncio.wait_for(
                    connection.new_session(cwd=str(cwd), mcp_servers=[mcp_server]),
                    timeout=timeout,
                )
                hermes_session_id = session.session_id
                history: list[dict[str, str]] = []
            else:
                try:
                    callbacks.replaying = True
                    try:
                        loaded = await asyncio.wait_for(
                            connection.load_session(
                                cwd=str(cwd),
                                session_id=hermes_session_id,
                                mcp_servers=[mcp_server],
                            ),
                            timeout=timeout,
                        )
                    finally:
                        callbacks.replaying = False
                except RequestError as exc:
                    log_event(
                        logger,
                        logging.INFO,
                        "acp_session_load",
                        channel="acp",
                        outcome="rejected",
                        reason_code="session_load_rejected",
                        session_id=session_id,
                        hermes_session_id=hermes_session_id,
                    )
                    raise HermesSessionLoadError("Hermes rejected the stored session") from exc
                if loaded is None or not loaded.model_fields_set:
                    log_event(
                        logger,
                        logging.INFO,
                        "acp_session_load",
                        channel="acp",
                        outcome="rejected",
                        reason_code="session_not_found",
                        session_id=session_id,
                        hermes_session_id=hermes_session_id,
                    )
                    raise HermesSessionLoadError("Hermes no longer has the stored session")
                history = list(callbacks.replay)
            log_event(
                logger,
                logging.INFO,
                "acp_process_start",
                channel="acp",
                outcome="success",
                session_id=session_id,
                hermes_session_id=hermes_session_id,
                duration_ms=int((monotonic() - started_at) * 1000),
            )
            return HermesProcess(
                process, connection, callbacks, hermes_session_id, history=history
            )
        except HermesSessionLoadError:
            await self._stop_process(process, connection)
            raise
        except (HermesUnavailableError, asyncio.CancelledError):
            await self._stop_process(process, connection)
            raise
        except Exception as exc:
            await self._stop_process(process, connection)
            reason_code = "startup_timeout" if isinstance(exc, TimeoutError) else "startup_failed"
            detail = (
                "Hermes ACP startup timed out"
                if isinstance(exc, TimeoutError) else "Hermes ACP startup failed"
            )
            log_event(
                logger,
                logging.ERROR,
                "acp_process_start",
                channel="acp",
                outcome="error",
                reason_code=reason_code,
                session_id=session_id,
                duration_ms=int((monotonic() - started_at) * 1000),
            )
            raise HermesUnavailableError(detail) from exc

    async def _open(self, session_id: str, previous_id: str | None) -> HermesProcess:
        entry = self._processes.get(session_id)
        if entry is not None and entry.process.returncode is None:
            return entry
        await self._close_unlocked(session_id)
        await self.close_idle()
        if len(self._processes) + self._starting >= get_settings().hermes_max_processes:
            log_event(
                logger,
                logging.WARNING,
                "acp_session_open",
                channel="acp",
                outcome="rejected",
                reason_code="max_processes_reached",
                session_id=session_id,
            )
            raise HermesUnavailableError("Hermes is busy. Please retry.")
        self._starting += 1
        try:
            try:
                entry = await self._start(session_id, previous_id)
            except HermesSessionLoadError:
                log_event(
                    logger,
                    logging.INFO,
                    "acp_session_fallback",
                    channel="acp",
                    outcome="success",
                    reason_code="fresh_session_opened",
                    session_id=session_id,
                )
                entry = await self._start(session_id, None)
            self._processes[session_id] = entry
            return entry
        finally:
            self._starting -= 1
    async def start_session(self, session_id: str) -> str:
        async with self._session(session_id):
            return (await self._open(session_id, None)).session_id

    async def reset_session(self, session_id: str) -> str:
        """Create a brand-new Hermes session for the app session.

        Used by the Telegram ``/new`` command: the previous live process is
        terminated and a fresh ``new_session`` is opened, so the chat
        continues with a clean context. The old Hermes session itself is not
        deleted — it stays in Hermes' own store (``state.db``); only the
        live binding changes.
        """
        async with self._session(session_id):
            await self._close_unlocked(session_id)
            return (await self._open(session_id, None)).session_id

    async def prompt(
        self,
        session_id: str,
        hermes_session_id: str,
        text: str,
        content_blocks: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        started_at = monotonic()
        has_image = content_blocks is not None
        async with self._session(session_id):
            entry = await self._open(session_id, hermes_session_id)
            entry.callbacks.messages.clear()
            prompt_blocks: list[Any] = [TextContentBlock(type="text", text=text)]
            if content_blocks is not None:
                prompt_blocks.append(
                    ImageContentBlock(
                        type="image",
                        data=content_blocks["data"],
                        mime_type=content_blocks["mime_type"],
                    )
                )
            try:
                response = await asyncio.wait_for(
                    entry.connection.prompt(session_id=entry.session_id, prompt=prompt_blocks),
                    timeout=get_settings().acp_prompt_timeout_seconds,
                )
            except asyncio.CancelledError:
                await self._close_unlocked(session_id)
                raise
            except Exception as exc:
                await self._close_unlocked(session_id)
                reason_code = "prompt_timeout" if isinstance(exc, TimeoutError) else "prompt_failed"
                detail = (
                    "Hermes ACP prompt timed out"
                    if isinstance(exc, TimeoutError) else "Hermes ACP prompt failed"
                )
                log_event(
                    logger,
                    logging.ERROR,
                    "acp_prompt",
                    channel="acp",
                    outcome="error",
                    reason_code=reason_code,
                    session_id=session_id,
                    hermes_session_id=entry.session_id,
                    has_text=bool(text),
                    has_image=has_image,
                    text_length=len(text),
                    duration_ms=int((monotonic() - started_at) * 1000),
                )
                raise HermesUnavailableError(detail) from exc
            finally:
                entry.last_used = monotonic()
            if response.stop_reason == "cancelled":
                log_event(
                    logger,
                    logging.WARNING,
                    "acp_prompt",
                    channel="acp",
                    outcome="rejected",
                    reason_code="prompt_cancelled",
                    session_id=session_id,
                    hermes_session_id=entry.session_id,
                    stop_reason=response.stop_reason,
                    duration_ms=int((monotonic() - started_at) * 1000),
                )
                raise HermesUnavailableError("Hermes ACP prompt was cancelled")
            if response.stop_reason not in ("end_turn", "max_tokens"):
                log_event(
                    logger,
                    logging.WARNING,
                    "acp_prompt_stop",
                    channel="acp",
                    outcome="rejected",
                    reason_code="unexpected_stop_reason",
                    session_id=session_id,
                    hermes_session_id=entry.session_id,
                    stop_reason=response.stop_reason,
                )
            answer = "".join(entry.callbacks.messages).strip()
            if answer:
                entry.history.append({"role": "user", "content": text})
                entry.history.append({"role": "assistant", "content": answer})
            log_event(
                logger,
                logging.INFO,
                "acp_prompt",
                channel="acp",
                outcome="success",
                session_id=session_id,
                hermes_session_id=entry.session_id,
                has_text=bool(text),
                has_image=has_image,
                text_length=len(text),
                stop_reason=response.stop_reason,
                duration_ms=int((monotonic() - started_at) * 1000),
            )
            return answer, entry.session_id

    async def history(self, session_id: str, hermes_session_id: str | None) -> list[dict[str, str]]:
        """Return the session transcript as captured from Hermes.

        Served from the warm process cache when one exists; otherwise the
        stored session is loaded (ACP replay) and the captured transcript
        returned.
        """
        if hermes_session_id is None:
            return []
        async with self._session(session_id):
            entry = await self._open(session_id, hermes_session_id)
            return [dict(item) for item in entry.history]

    async def close_session(self, session_id: str) -> None:
        """Terminate the live Hermes process for a session (if any)."""
        async with self._session(session_id):
            await self._close_unlocked(session_id)

    async def _close_unlocked(self, session_id: str) -> None:
        entry = self._processes.pop(session_id, None)
        if entry is not None:
            await self._stop_process(entry.process, entry.connection)

    async def close_idle(self) -> None:
        deadline = monotonic() - get_settings().hermes_idle_timeout_seconds
        for session_id, entry in list(self._processes.items()):
            if session_id in self._locks or self._processes.get(session_id) is not entry:
                continue
            if entry.process.returncode is not None or entry.last_used <= deadline:
                async with self._session(session_id):
                    await self._close_unlocked(session_id)

    async def reap_idle(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.close_idle()

    async def close_all(self) -> None:
        for session_id in list(self._processes):
            async with self._session(session_id):
                await self._close_unlocked(session_id)


hermes_manager = HermesClientManager()
