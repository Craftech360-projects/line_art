import unittest

from starlette.websockets import WebSocketDisconnect

from app.main import send_json, websocket_endpoint
from app.models import ProgressMessage


class ClosedWebSocket:
    async def send_text(self, _text):
        raise WebSocketDisconnect(code=1000)


class SendJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_json_reports_closed_websocket_without_raising(self):
        sent = await send_json(
            ClosedWebSocket(),
            ProgressMessage(stage="generating", message="Generating line art..."),
        )

        self.assertFalse(sent)


class DisconnectingReceiveWebSocket:
    async def accept(self):
        return None

    async def receive(self):
        raise RuntimeError('Cannot call "receive" once a disconnect message has been received.')


class WebSocketEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_exits_cleanly_when_receive_reports_closed_socket(self):
        await websocket_endpoint(DisconnectingReceiveWebSocket())
