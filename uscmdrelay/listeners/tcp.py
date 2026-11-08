import asyncio
import json
import logging
from uscmdrelay.libraries.connection_limiter import ConnectionLimiter
from uscmdrelay.listeners import READ_TIMEOUT, WRITE_TIMEOUT
from uscmdrelay.models.listener import ListenerModel
from uscmdrelay.models.response import ResponseCode, ResponseModel
from uscmdrelay.services.relay import RelayService

__all__ = ['TcpListener']

class TcpListener:
    def __init__(self, model: ListenerModel, relay: RelayService, limiter: ConnectionLimiter, *, max_message_size: int, logger: logging.Logger) -> None:
        self._model: ListenerModel = model
        self._relay: RelayService = relay
        self._limiter: ConnectionLimiter = limiter
        self._max_message_size: int = max_message_size
        self._logger: logging.Logger = logger
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self._model.host, self._model.port, limit=self._max_message_size + 1)
        self._logger.info(f'Listening for TCP clients on {self._model.host}:{self._model.port}')

    async def stop_accepting(self) -> None:
        self._server.close()

    async def wait_closed(self) -> None:
        if self._clients:
            _, pending = await asyncio.wait(self._clients, timeout=5)

            for task in pending:
                task.cancel()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

        peer_ip = writer.get_extra_info('peername')[0]

        if not self._limiter.acquire(peer_ip):
            writer.transport.abort()
            return

        try:
            await self._serve_client(reader, writer, peer_ip)
        finally:
            self._limiter.release(peer_ip)

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer_ip: str) -> None:
        try:
            response = await self._process(reader, peer_ip)

            writer.write((json.dumps(response.to_dict()) + '\n').encode('utf-8'))
            await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
        except ConnectionError as e:
            self._logger.debug(f'Client {peer_ip} went away: {e}')
        except asyncio.TimeoutError:
            self._logger.warning(f'Client {peer_ip} stopped reading its response')
            writer.transport.abort()
        finally:
            await self._close(writer)

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        writer.close()

        try:
            await asyncio.wait_for(writer.wait_closed(), WRITE_TIMEOUT)
        except ConnectionError:
            pass
        except asyncio.TimeoutError:
            writer.transport.abort()

    async def _process(self, reader: asyncio.StreamReader, peer_ip: str) -> ResponseModel:
        try:
            raw = await asyncio.wait_for(self._read_message(reader), READ_TIMEOUT)
        except asyncio.TimeoutError:
            return ResponseModel.error('Timed out waiting for the message', ResponseCode.BAD_MESSAGE)
        except ValueError:
            return ResponseModel.error('Message is too large', ResponseCode.BAD_MESSAGE)

        return await self._relay.handle(raw, peer_ip)

    async def _read_message(self, reader: asyncio.StreamReader) -> bytes:
        """ Read one message: a single JSON line, or KEY=VALUE lines up to a blank line. """
        data = bytearray()

        while line := await reader.readline():
            data += line

            if len(data) > self._max_message_size:
                raise ValueError('message too large')

            started = data.strip()

            if started.startswith(b'{') or (started and not line.strip()):
                break

        return bytes(data)
