from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import Any


MAX_JSONRPC_MESSAGE_BYTES = 8 * 1024 * 1024
_MISSING = object()
_CURRENT_INBOUND_REQUEST: ContextVar[tuple[object, str | int] | None] = (
    ContextVar("agentserver_jsonrpc_inbound_request", default=None)
)


class JsonRpcPeerError(RuntimeError):
    pass


class JsonRpcProtocolError(JsonRpcPeerError):
    pass


class JsonRpcTransportError(JsonRpcPeerError):
    pass


class JsonRpcRequestTimeout(JsonRpcPeerError):
    def __init__(self, method: str) -> None:
        super().__init__(f"JSON-RPC request timed out: {method}")
        self.method = method


class JsonRpcRemoteError(JsonRpcPeerError):
    def __init__(
        self,
        *,
        code: int,
        message: str,
        method: str,
        request_id: str,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.remote_message = message
        self.method = method
        self.request_id = request_id
        self.data = data


class JsonRpcRequestError(JsonRpcPeerError):
    """A local inbound-handler error that is safe to return to the peer."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.safe_message = str(message)
        self.data = data


RequestHandler = Callable[[Any], Awaitable[Any]]
NotificationHandler = Callable[[Any], Awaitable[None]]
DefaultNotificationHandler = Callable[[str, Any], Awaitable[None]]
DiagnosticHandler = Callable[[str], None]


def _valid_id(value: object) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _object(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


class AsyncJsonRpcPeer:
    """Bidirectional JSON-RPC peer over newline-delimited JSON streams.

    Codex omits the ``jsonrpc`` member on the wire. Incoming peers that include
    it are tolerated, but this class never emits it.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_message_bytes: int = MAX_JSONRPC_MESSAGE_BYTES,
        max_inbound_requests: int = 64,
        diagnostic: DiagnosticHandler | None = None,
    ) -> None:
        if max_message_bytes < 1024:
            raise ValueError("max_message_bytes must be at least 1024")
        if max_inbound_requests < 1:
            raise ValueError("max_inbound_requests must be positive")
        self.reader = reader
        self.writer = writer
        self.max_message_bytes = int(max_message_bytes)
        self.max_inbound_requests = int(max_inbound_requests)
        self.diagnostic = diagnostic
        self._request_handlers: dict[str, RequestHandler] = {}
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._default_notification_handler: DefaultNotificationHandler | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        self._next_request_id = 1
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._inbound_tasks_by_id: dict[
            tuple[str, str | int], asyncio.Task[None]
        ] = {}
        self._closed = False
        self._closed_event = asyncio.Event()
        self._terminal_error: JsonRpcPeerError | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def terminal_error(self) -> JsonRpcPeerError | None:
        return self._terminal_error

    def handle_request(self, method: str, handler: RequestHandler) -> None:
        if self._reader_task is not None:
            raise RuntimeError("request handlers must be registered before start")
        self._request_handlers[method] = handler

    def handle_notification(self, method: str, handler: NotificationHandler) -> None:
        if self._reader_task is not None:
            raise RuntimeError("notification handlers must be registered before start")
        self._notification_handlers.setdefault(method, []).append(handler)

    def handle_unknown_notification(self, handler: DefaultNotificationHandler) -> None:
        if self._reader_task is not None:
            raise RuntimeError("notification handlers must be registered before start")
        self._default_notification_handler = handler

    async def start(self) -> None:
        if self._reader_task is not None:
            return
        if self._closed:
            raise JsonRpcTransportError("JSON-RPC peer is closed")
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="agentserver-jsonrpc-reader"
        )

    async def request(
        self,
        method: str,
        params: Any = _MISSING,
        *,
        timeout: float | None = None,
    ) -> Any:
        self._require_open()
        loop = asyncio.get_running_loop()
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = loop.create_future()
        key = str(request_id)
        self._pending[key] = (method, future)
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not _MISSING:
            message["params"] = params
        try:
            await self._send(message)
        except BaseException:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()
            raise
        try:
            if timeout is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout=float(timeout))
            except asyncio.TimeoutError as error:
                raise JsonRpcRequestTimeout(method) from error
        finally:
            self._pending.pop(key, None)

    async def notify(self, method: str, params: Any = _MISSING) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not _MISSING:
            message["params"] = params
        await self._send(message)

    async def wait_closed(self) -> JsonRpcPeerError | None:
        await self._closed_event.wait()
        return self._terminal_error

    async def wait_inbound(self, timeout: float | None = None) -> None:
        tasks = tuple(self._inbound_tasks)
        if not tasks:
            return
        waiter = asyncio.gather(*tasks, return_exceptions=True)
        if timeout is None:
            await waiter
            return
        try:
            await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            return

    def current_inbound_request_id(self) -> str | int | None:
        """Return the wire id for the request executing in this task context."""

        current = _CURRENT_INBOUND_REQUEST.get()
        if current is None or current[0] is not self:
            return None
        return current[1]

    async def wait_inbound_response(self, request_id: str | int) -> None:
        """Wait until an inbound request's response has completed ``drain()``.

        Completed tasks are removed from the live index, so a missing task is
        already past the response boundary. Shielding prevents cancellation of
        an API caller from cancelling the protocol handler that owns the reply.
        """

        task = self._inbound_tasks_by_id.get(self._inbound_request_key(request_id))
        if task is None:
            return
        if task is asyncio.current_task():
            raise RuntimeError("an inbound request cannot wait for its own response")
        await asyncio.shield(task)

    async def close(self) -> None:
        await self._terminate(
            JsonRpcTransportError("JSON-RPC peer closed"), unexpected=False
        )

    def _require_open(self) -> None:
        if self._closed:
            raise self._terminal_error or JsonRpcTransportError(
                "JSON-RPC peer is closed"
            )
        if self._reader_task is None:
            raise JsonRpcTransportError("JSON-RPC peer is not started")

    async def _send(self, message: Mapping[str, Any]) -> None:
        self._require_open()
        try:
            encoded = json.dumps(
                dict(message),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as error:
            raise JsonRpcProtocolError("JSON-RPC message is not JSON encodable") from error
        if len(encoded) > self.max_message_bytes:
            raise JsonRpcProtocolError("JSON-RPC message exceeds the wire limit")
        try:
            async with self._write_lock:
                self._require_open()
                self.writer.write(encoded)
                await self.writer.drain()
        except JsonRpcPeerError:
            raise
        except (ConnectionError, OSError, RuntimeError) as error:
            failure = JsonRpcTransportError("failed to write JSON-RPC message")
            await self._terminate(failure, unexpected=True)
            raise failure from error

    async def _read_loop(self) -> None:
        failure: JsonRpcPeerError | None = None
        try:
            while not self._closed:
                try:
                    encoded = await self.reader.readline()
                except (ValueError, asyncio.LimitOverrunError) as error:
                    raise JsonRpcProtocolError(
                        "JSON-RPC line exceeds the wire limit"
                    ) from error
                except (ConnectionError, OSError) as error:
                    raise JsonRpcTransportError(
                        "failed to read JSON-RPC input"
                    ) from error
                if not encoded:
                    raise JsonRpcTransportError("JSON-RPC input stream ended")
                if len(encoded) > self.max_message_bytes:
                    raise JsonRpcProtocolError(
                        "JSON-RPC line exceeds the wire limit"
                    )
                if not encoded.strip():
                    continue
                try:
                    message = json.loads(encoded)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise JsonRpcProtocolError("invalid JSON-RPC wire message") from error
                if not isinstance(message, Mapping):
                    raise JsonRpcProtocolError("JSON-RPC wire message must be an object")
                await self._route(message)
        except asyncio.CancelledError:
            return
        except JsonRpcPeerError as error:
            failure = error
        except BaseException as error:
            failure = JsonRpcTransportError("JSON-RPC reader failed")
            failure.__cause__ = error
        finally:
            if failure is not None:
                await self._terminate(failure, unexpected=True)

    async def _route(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        has_id = "id" in message
        if isinstance(method, str) and method:
            if has_id:
                if not _valid_id(message.get("id")):
                    raise JsonRpcProtocolError("JSON-RPC request id is invalid")
                await self._start_inbound_request(
                    message["id"], method, message.get("params")
                )
                return
            await self._dispatch_notification(method, message.get("params"))
            return
        if has_id and _valid_id(message.get("id")):
            await self._handle_response(message)
            return
        raise JsonRpcProtocolError("unroutable JSON-RPC wire message")

    async def _handle_response(self, message: Mapping[str, Any]) -> None:
        request_id = str(message["id"])
        pending = self._pending.pop(request_id, None)
        if pending is None:
            self._diagnose("late_or_unknown_response")
            return
        method, future = pending
        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            error = JsonRpcProtocolError("JSON-RPC response must have result or error")
            if not future.done():
                future.set_exception(error)
            raise error
        if has_error:
            body = _object(message.get("error"))
            code = body.get("code") if body else None
            remote_message = body.get("message") if body else None
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or not isinstance(remote_message, str)
            ):
                error = JsonRpcProtocolError("JSON-RPC response error is invalid")
                if not future.done():
                    future.set_exception(error)
                raise error
            error = JsonRpcRemoteError(
                code=code,
                message=remote_message,
                method=method,
                request_id=request_id,
                data=body.get("data") if body else None,
            )
            if not future.done():
                future.set_exception(error)
            return
        if not future.done():
            future.set_result(message.get("result"))

    async def _dispatch_notification(self, method: str, params: Any) -> None:
        handlers = self._notification_handlers.get(method, ())
        if not handlers and self._default_notification_handler is not None:
            try:
                await self._default_notification_handler(method, params)
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._diagnose("notification_handler_failed")
            return
        for handler in handlers:
            try:
                await handler(params)
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._diagnose("notification_handler_failed")

    async def _start_inbound_request(
        self, request_id: str | int, method: str, params: Any
    ) -> None:
        if len(self._inbound_tasks) >= self.max_inbound_requests:
            await self._send_error(
                request_id, -32001, "Server overloaded; retry later."
            )
            return
        task = asyncio.create_task(
            self._dispatch_request(request_id, method, params),
            name=f"agentserver-jsonrpc-inbound:{method}",
        )
        request_key = self._inbound_request_key(request_id)
        self._inbound_tasks.add(task)
        self._inbound_tasks_by_id[request_key] = task
        task.add_done_callback(
            lambda completed: self._finish_inbound_request(
                request_key, completed
            )
        )

    async def _dispatch_request(
        self, request_id: str | int, method: str, params: Any
    ) -> None:
        handler = self._request_handlers.get(method)
        if handler is None:
            await self._send_error(request_id, -32601, "Method not found")
            return
        try:
            token = _CURRENT_INBOUND_REQUEST.set((self, request_id))
            try:
                result = await handler(params)
            finally:
                _CURRENT_INBOUND_REQUEST.reset(token)
        except asyncio.CancelledError:
            raise
        except JsonRpcRequestError as error:
            await self._send_error(
                request_id, error.code, error.safe_message, error.data
            )
            return
        except BaseException:
            await self._send_error(request_id, -32603, "Request handler failed")
            return
        try:
            await self._send({"id": request_id, "result": result})
        except JsonRpcPeerError:
            if not self._closed:
                raise

    @staticmethod
    def _inbound_request_key(request_id: str | int) -> tuple[str, str | int]:
        if not _valid_id(request_id):
            raise ValueError("request_id must be a JSON-RPC string or integer id")
        return ("string" if isinstance(request_id, str) else "integer", request_id)

    def _finish_inbound_request(
        self,
        request_key: tuple[str, str | int],
        task: asyncio.Task[None],
    ) -> None:
        self._inbound_tasks.discard(task)
        if self._inbound_tasks_by_id.get(request_key) is task:
            self._inbound_tasks_by_id.pop(request_key, None)

    async def _send_error(
        self, request_id: str | int, code: int, message: str, data: Any = _MISSING
    ) -> None:
        error: dict[str, Any] = {"code": int(code), "message": str(message)}
        if data is not _MISSING:
            error["data"] = data
        try:
            await self._send({"id": request_id, "error": error})
        except JsonRpcPeerError:
            if not self._closed:
                raise

    async def _terminate(
        self, error: JsonRpcPeerError, *, unexpected: bool
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if unexpected:
            self._terminal_error = error
        else:
            self._terminal_error = self._terminal_error or error
        pending = tuple(self._pending.values())
        self._pending.clear()
        for _method, future in pending:
            if not future.done():
                future.set_exception(error)
        current = asyncio.current_task()
        for task in tuple(self._inbound_tasks):
            if task is not current:
                task.cancel()
        reader_task = self._reader_task
        if reader_task is not None and reader_task is not current:
            reader_task.cancel()
        try:
            self.writer.close()
            wait_closed = getattr(self.writer, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass
        self._closed_event.set()

    def _diagnose(self, code: str) -> None:
        if self.diagnostic is not None:
            self.diagnostic(code)
