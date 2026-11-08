import asyncio
import logging
from aiohttp import web, web_response
from uscmdrelay.exceptions import RelayError
from uscmdrelay.libraries.connection_limiter import ConnectionLimiter
from uscmdrelay.listeners import READ_TIMEOUT, WRITE_TIMEOUT
from uscmdrelay.models.listener import ListenerModel
from uscmdrelay.models.response import ResponseCode, ResponseModel
from uscmdrelay.services.relay import RelayService

__all__ = ['HttpListener']

# aiohttp has no option for this, and it also sends the header on replies to malformed requests
web_response.SERVER_SOFTWARE = 'uscmdrelay'

_HTTP_STATUS = {
    ResponseCode.OK: 200,
    ResponseCode.AUTH_FAILED: 401,
    ResponseCode.UNKNOWN_CLUSTER: 404,
    ResponseCode.UNKNOWN_COMMAND: 404,
    ResponseCode.INVALID_FIELD: 422,
    ResponseCode.WRONG_FORMAT: 400,
    ResponseCode.BAD_MESSAGE: 400,
    ResponseCode.COMMAND_FAILED: 500,
    ResponseCode.COMMAND_TIMEOUT: 500,
    ResponseCode.BUSY: 503,
    ResponseCode.SERVER_ERROR: 500,
}

class _LimitedProtocol(asyncio.Protocol):
    """ Applies the connection limits, which aiohttp has no option for. """

    def __init__(self, handler_factory: web.Server, limiter: ConnectionLimiter) -> None:
        self._handler_factory: web.Server = handler_factory
        self._limiter: ConnectionLimiter = limiter
        self._handler: asyncio.Protocol | None = None
        self._peer_ip: str = ''

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._peer_ip = transport.get_extra_info('peername')[0]

        if not self._limiter.acquire(self._peer_ip):
            transport.abort()
            return

        self._handler = self._handler_factory()
        self._handler.connection_made(transport)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._handler is None:
            return

        self._limiter.release(self._peer_ip)
        self._handler.connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        self._handler.data_received(data)

    def eof_received(self) -> bool | None:
        return self._handler.eof_received()

    def pause_writing(self) -> None:
        self._handler.pause_writing()

    def resume_writing(self) -> None:
        self._handler.resume_writing()

class HttpListener:
    def __init__(self, model: ListenerModel, relay: RelayService, limiter: ConnectionLimiter, *, max_message_size: int, logger: logging.Logger) -> None:
        self._model: ListenerModel = model
        self._relay: RelayService = relay
        self._limiter: ConnectionLimiter = limiter
        self._max_message_size: int = max_message_size
        self._logger: logging.Logger = logger
        self._runner: web.AppRunner | None = None
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=self._max_message_size, middlewares=[self._error_middleware])
        app.router.add_post('/run/{cluster}/{command}', self._handle_run)

        # the keepalive timeout also limits connections that never send a request
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=5, keepalive_timeout=READ_TIMEOUT)
        await self._runner.setup()

        self._server = await asyncio.get_running_loop().create_server(
            lambda: _LimitedProtocol(self._runner.server, self._limiter),
            self._model.host,
            self._model.port,
        )

        self._logger.info(f'Listening for HTTP clients on {self._model.host}:{self._model.port}')

    async def stop_accepting(self) -> None:
        self._server.close()

    async def wait_closed(self) -> None:
        await self._runner.cleanup()

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException as e:
            return web.json_response(ResponseModel.error(e.reason, ResponseCode.BAD_MESSAGE).to_dict(), status=e.status)

    async def _handle_run(self, request: web.Request) -> web.StreamResponse:
        peer_ip = request.remote or ''
        token = self._get_bearer_token(request)

        # with a bearer token, nothing from an unauthenticated client is read or parsed
        try:
            key = self._relay.authenticate(token, peer_ip) if token is not None else None
        except RelayError as e:
            return await self._respond(request, ResponseModel.error(str(e), e.code))

        try:
            body = await asyncio.wait_for(request.read(), READ_TIMEOUT)
        except asyncio.TimeoutError:
            return await self._respond(request, ResponseModel.error('Timed out waiting for the message', ResponseCode.BAD_MESSAGE))

        target = (request.match_info['cluster'], request.match_info['command'])

        return await self._respond(request, await self._relay.handle(body, peer_ip, key=key, target=target, headers=request.headers.items()))

    def _get_bearer_token(self, request: web.Request) -> str | None:
        scheme, _, token = request.headers.get('Authorization', '').partition(' ')

        return token.strip() if scheme.lower() == 'bearer' else None

    async def _respond(self, request: web.Request, response: ResponseModel) -> web.StreamResponse:
        status = 202 if response.status == 'accepted' else _HTTP_STATUS[ResponseCode(response.code)]
        reply = web.json_response(response.to_dict(), status=status)

        try:
            await asyncio.wait_for(self._send(request, reply), WRITE_TIMEOUT)
        except ConnectionError as e:
            self._logger.debug(f'Client {request.remote} went away: {e}')
        except asyncio.TimeoutError:
            self._logger.warning(f'Client {request.remote} stopped reading its response')

            if request.transport is not None:
                request.transport.abort()

        return reply

    async def _send(self, request: web.Request, reply: web.Response) -> None:
        await reply.prepare(request)
        await reply.write_eof()
