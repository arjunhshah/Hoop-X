import asyncio
import json
import time
import traceback
import uuid
from queue import Queue
from .data import Ctx
from ....utils.logger import logger

try:
    from ....modules.external.websockets.sync.client import connect
    from ....modules.external.websockets.server import serve, broadcast
    from ....modules.external.websockets.legacy.server import WebSocketServer
    from ....modules.external.websockets.exceptions import ConnectionClosedOK
    from ....modules.external.websockets.sync.client import ClientConnection
    from ....modules.external.websockets.protocol import State
    from ....modules.external.websockets.exceptions import (
        ConnectionClosedError,
        ConnectionClosed,
    )
except Exception:
    from websockets.sync.client import connect
    from websockets.server import serve, broadcast
    from websockets import WebSocketServerProtocol
    from websockets.legacy.server import WebSocketServer
    from websockets.exceptions import ConnectionClosedOK
    from websockets.sync.client import ClientConnection
    from websockets.protocol import State


"""
        listen_addr = f"ws://{get_ip()}:{get_port()}/ws?clientId={SessionId['SessionId']}"
        ws = WebSocketApp(listen_addr, on_message=on_message)
        TaskManager.ws = ws
        ws.run_forever()

"""


class Server:
    _host = "127.0.0.1"
    _port = 61863
    _server: "WebSocketServer" = None
    _compatible_browser = False

    def __init__(self, port):
        self.host = self._host
        self.port = port
        self.logger = logger
        self._handlers = {}
        self._sockets: dict[str, WebSocketServerProtocol] = {}
        self._submit_tasks = {}
        self._processing_tasks = {}
        self._failed_tasks = {}
        self._succeeded_task = {}
        self._disconnect_sids = set()
        self._alive_clients = Queue()
        self.stop_event = asyncio.Event()

        self.reg_handler("_default", self._default)
        self.reg_handler("hello_client", self._hello)
        self.reg_handler("rodin_auth", self._auth)
        self.reg_handler("close_server", self._close)

        # web 接口
        self.reg_handler("web_connect", self._web_connect)
        self.reg_handler("send_model", self._send_model)
        self.reg_handler("fetch_task", self._fetch_task)
        self.reg_handler("fetch_material_config", self._fetch_material_config)
        self.reg_handler("fail_task", self._fail_task)
        self.reg_handler("ping_client_return", self._ping_client_return)

        # 本机接口
        self.reg_handler("submit_task", self._submit_task)
        self.reg_handler("skip_task", self._skip_task)
        self.reg_handler("query_sid_dead", self._query_sid_dead)
        self.reg_handler("query_task_status", self._query_task_status)
        self.reg_handler("fetch_task_result", self._fetch_task_result)
        self.reg_handler("clear_task", self._clear_task)
        self.reg_handler("any_client_connected", self._any_client_connected)

    def reg_handler(self, etype, handler):
        self._handlers[etype] = handler

    def unreg_handler(self, etype):
        del self._handlers[etype]

    def pop_task_all(self, sid):
        logger.debug(f"remove [{sid}]")
        self._submit_tasks.pop(sid, None)
        self._processing_tasks.pop(sid, None)
        self._succeeded_task.pop(sid, None)
        self._failed_tasks.pop(sid, None)

    async def call_handler(self, websocket: "WebSocketServerProtocol", message):
        try:
            event: dict = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            event = {}
        etype = event.get("type", "_default")
        handler = self._handlers.get(etype, self._default)
        try:
            await handler(websocket, event)
        except Exception as e:
            self.logger.error(f"Error in handler {handler.__name__}: {e}")
            self.logger.error(traceback.format_exc())

    async def _hello(self, websocket: "WebSocketServerProtocol", event: dict):
        self.logger.info(f"Server Received: {event}")
        event = {"type": "hello_server", "data": "Hello Client!"}
        await websocket.send(json.dumps(event))

    async def _auth(self, websocket: "WebSocketServerProtocol", event: dict):
        event = {
            "type": "rodin_auth_return",
            "data": "OK",
        }
        await websocket.send(json.dumps(event))

    async def _web_connect(self, websocket: "WebSocketServerProtocol", event: dict):
        """
        web端尝试连接blender端
        """
        event = {
            "type": "web_connect_return",
            "data": "OK",
        }
        await websocket.send(json.dumps(event))

    async def _send_model(self, websocket: "WebSocketServerProtocol", event: dict):
        """
        web端往blender端发送模型
        """
        files = event.get("data", {}).get("files", None)
        sid = event.get("data", {}).get("sid", None)
        browser = event.get("data", {}).get("browser", None)
        if sid is None:
            sid = event.get("sid", None)
        if sid:
            logger.debug(f"Received model from: {sid}")
        self.pop_task_all(sid)

        if sid:
            self.logger.info(f"Task succeded by {sid}: {websocket.remote_address}")
            self._succeeded_task[sid] = {}  # = event.get("data", None)
        else:
            self.logger.info(f"Received send model: {websocket.remote_address}")
        if not files:  # None, '', [], {} 都会被认为是 False
            fail_event = {
                "type": "send_model_return",
                "sid": None,
                "data": "Fail",
            }
            await websocket.send(json.dumps(fail_event))
            logger.debug(f"Sent send model return (fail): {fail_event}")
            if browser:
                if browser == "Firefox" or browser == "Safari":
                    Server._compatible_browser = True
                else:
                    Server._compatible_browser = False
            return
        from ..utils import RodinModelLoader
        from ....utils.timer import Timer

        Timer.put((RodinModelLoader.load_rodin_model, event))
        # 发送收到消息
        event = {
            "type": "send_model_return",
            "sid": sid,
            "data": "OK",
        }
        await websocket.send(json.dumps(event))
        logger.debug(f"Sent send model return: {event}")

    async def _fetch_task(self, websocket: "WebSocketServerProtocol", event: dict):
        """
        web端尝试获取任务
        fetch后task会从submit_task中移除，并添加到processing_task中
        """
        # sid = getattr(websocket, "sid", None) # 不再使用sid获取任务
        event = {
            "type": "fetch_task_return",
            "task": None,
        }
        sid = next((k for k, v in self._submit_tasks.items() if v is not None), None)
        if sid is not None:
            event["task"] = self._submit_tasks[sid]
            event["sid"] = sid
            self._processing_tasks[sid] = event["task"]
        self.logger.info(f"Task fetched {sid}: {websocket.remote_address}")
        await websocket.send(json.dumps(event))

    async def _fetch_material_config(
        self, websocket: "WebSocketServerProtocol", event: dict
    ):
        """
        web端尝试获取材质配置
        """
        event = {
            "type": "fetch_material_config_return",
            "config": Ctx.config,
            "condition_type": Ctx.condition_type,
        }
        await websocket.send(json.dumps(event))

    async def _ping_client_return(
        self, websocket: "WebSocketServerProtocol", event: dict
    ):
        """
        blender端尝试ping
        """
        if event.get("status") != "ok":
            return
        self._alive_clients.put(websocket)

    async def _fail_task(self, websocket: "WebSocketServerProtocol", event: dict):
        sid = event.get("sid", None)
        self.pop_task_all(sid)
        self.logger.info(f"Task failed by {sid}: {websocket.remote_address}")
        self._failed_tasks[sid] = event.get("data", None)

    async def _submit_task(self, websocket: "WebSocketServerProtocol", event: dict):
        """
        blender端尝试提交任务
        """
        # 鉴别只能由blender端提交任务
        sid = event.get("sid", None)
        if sid is None:
            return
        from ..utils import RodinModelLoader

        tree_str = RodinModelLoader.print_tree_str(event)
        self.logger.debug(f"Server Received Task:\n{tree_str}")
        self._submit_tasks[sid] = event.get("data", "{}")

    async def _skip_task(self, websocket: "WebSocketServerProtocol", event: dict):
        sid = event.get("sid", None)
        event = {
            "type": "skip_task_return",
            "data": "none",
        }
        self.pop_task_all(sid)
        event["data"] = "skipped"
        await websocket.send(json.dumps(event))

    async def _query_sid_dead(self, websocket: "WebSocketServerProtocol", event: dict):
        sid = event.get("sid", None)
        event = {
            "type": "query_sid_dead_return",
            "dead": sid in self._disconnect_sids,
        }
        await websocket.send(json.dumps(event))

    async def _query_task_status(
        self, websocket: "WebSocketServerProtocol", event: dict
    ):
        sid = event.get("sid", None)
        event = {
            "type": "query_task_status_return",
            "status": "",
        }
        if sid in self._submit_tasks:
            event["status"] = "pending"
        if sid in self._processing_tasks:
            event["status"] = "processing"
        if sid in self._failed_tasks:
            event["status"] = "failed"
        if sid in self._succeeded_task:
            event["status"] = "succeeded"
        if sid is None:
            event["status"] = "not_found"
        await websocket.send(json.dumps(event))

    async def _fetch_task_result(
        self, websocket: "WebSocketServerProtocol", event: dict
    ):
        sid = event.get("sid", None)
        event = {
            "type": "fetch_task_result_return",
            "result": None,
            "status": "not_found",
        }
        if sid in self._succeeded_task:
            event["result"] = self._succeeded_task.pop(sid, None)
            event["status"] = "succeeded"
        if sid in self._failed_tasks:
            event["result"] = self._failed_tasks.pop(sid, None)
            event["status"] = "failed"
        if sid is None:
            event["status"] = "not_found"
        await websocket.send(json.dumps(event))

    async def _any_client_connected(
        self, websocket: "WebSocketServerProtocol", event: dict
    ):
        # server 请求
        event = {
            "type": "ping_client",
        }
        # # client 返回
        # event = {
        #     "type": "ping_client_return",
        #     "status": "ok",
        # }
        # 向所有客户端发送 ping_client 消息, 如果有客户端返回 ping_client_return, 则认为客户端在线
        # 如果无客户端返回 ping_client_return, 则认为客户端离线
        ts = time.time()
        for sid, ws in self._sockets.items():
            if ws == websocket:
                continue
            try:
                await ws.send(json.dumps(event))
            except ConnectionClosedOK:
                pass
            except ConnectionClosedError:
                pass
            except Exception as e:
                self.logger.critical(f"客户端{sid}异常: {e}")
                traceback.print_exc()
        self.logger.debug(f"总查询耗时: {time.time() - ts:.2f}s")
        await asyncio.sleep(0.1)  # 等待客户端返回
        event = {
            "type": "any_client_connected_return",
            "status": None if self._alive_clients.empty() else "ok",
        }
        while not self._alive_clients.empty():
            self._alive_clients.get()
        await websocket.send(json.dumps(event))

    async def _clear_task(self, websocket: "WebSocketServerProtocol", event: dict):
        sid = event.get("sid", None)
        self.pop_task_all(sid)

    async def _close(self, websocket: "WebSocketServerProtocol", event: dict):
        self.logger.warning(f"Server Closing: {event}")
        Server._server.close()
        Server._server = None

    # def _direct_close():
    #     logger.debug("Server Closing")
    #     for ws in Server._server.websockets:
    #         logger.debug(f"Server Closing: {ws._port}")
    #         ws.close()
    #     logger.debug("Server Closing 2")
    #     Server._server.close()
    #     Server._server = None

    async def _default(self, websocket: "WebSocketServerProtocol", event: dict):
        try:
            self.logger.warning(f"默认消息: {event}")
            event = {
                "type": "default",
                "data": event,
            }
            await websocket.send(json.dumps(event))
        except ConnectionClosedOK:
            pass

    def get_sid(self, path: str) -> str:
        if not isinstance(path, str):
            return uuid.uuid4().hex
        return path.split("id=")[-1] if "id=" in path else uuid.uuid4().hex

    async def handle(self, websocket: "WebSocketServerProtocol", path: str):
        # 从请求uri中获取id, uri格式: ws://{host}:{port}/ws?id={self.id}
        try:
            sid = self.get_sid(path)
            # 连入时注册ws
            self._sockets[sid] = websocket
            websocket.sid = sid
            self.logger.debug(f"Client Connected: {websocket}")
            self.logger.debug(f"Client Connected: {sid}")

            async for message in websocket:
                await self.call_handler(websocket, message)
        except ConnectionClosed as e:
            self.logger.debug(
                f"客户端{sid}断开: {e.code} (code={e.code}, reason='{e.reason}')"
            )
        except Exception as e:
            self.logger.critical(f"客户端{sid}异常: {e}")
        finally:
            self._disconnect_sids.add(sid)

    async def main(self):
        async with serve(self.handle, self.host, self.port, max_size=None) as server:
            self.logger.warning(f"Server running on port {self.port}")
            Server._port = self.port
            Server._server = server
            await self.stop_event.wait()  # 阻塞直到设置 stop

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.main())

    def _direct_close(self):
        def _stop():
            self.stop_event.set()

        if hasattr(self, "loop"):
            self.loop.call_soon_threadsafe(_stop)


class BlenderClient:
    """
    用于blender端内部使用
    """

    _id = uuid.uuid4().hex

    def __init__(self):
        self.logger = logger
        self.websocket: ClientConnection = None

    def is_connected(self):
        """
        server关闭后websocket.state为 CLOSED
        """
        return self.websocket and self.websocket.state == State.OPEN

    def ensure_connect(self) -> bool:
        if not self.is_connected():
            self.try_connect()
        if self.websocket is None:
            self.logger.error("Server not connected")
            return False
        return True

    def try_connect(self):
        if self.is_connected():
            return True
        self.websocket = None
        try:
            websocket = connect(self.uri)
            self.websocket = websocket
        except Exception:
            return False

    @property
    def host(self):
        return Server._host

    @property
    def port(self):
        return Server._port

    @property
    def uri(self):
        return f"ws://{self.host}:{self.port}/ws?id={self._id}"

    def submit_task(self, data: dict, sid=None):
        if not self.ensure_connect():
            raise Exception("Server not connected")
        event = {
            "type": "submit_task",
            "sid": sid or uuid.uuid4().hex,
            "data": data,
        }

        self.websocket.send(json.dumps(event))

    def query_sid_dead(self, sid):
        if not self.ensure_connect():
            raise Exception("Server not connected")
        event = {
            "type": "query_sid_dead",
            "sid": sid,
        }
        self.websocket.send(json.dumps(event))
        resp = self.websocket.recv()
        return json.loads(resp).get("dead", False)

    def query_task_status(self, sid) -> str:
        if not self.ensure_connect():
            raise Exception("Server not connected")

        event = {
            "type": "query_task_status",
            "sid": sid,
        }
        self.websocket.send(json.dumps(event))
        resp = self.websocket.recv()
        res = json.loads(resp).get("status", "error")
        return res

    def fetch_task_result(self, sid):
        if not self.ensure_connect():
            raise Exception("Server not connected")

        event = {
            "type": "fetch_task_result",
            "sid": sid,
        }
        self.websocket.send(json.dumps(event))
        resp = self.websocket.recv()
        return resp

    def skip_task(self, sid):
        if not self.ensure_connect():
            raise Exception("Server not connected")

        event = {
            "type": "skip_task",
            "sid": sid,
        }
        logger.debug(f"任务[{sid}] -> skip_task")
        self.websocket.send(json.dumps(event))

    def clear_task(self, sid):
        if not self.ensure_connect():
            raise Exception("Server not connected")

        event = {
            "type": "clear_task",
            "sid": sid,
        }
        self.websocket.send(json.dumps(event))

    def any_client_connected(self):
        if not self.ensure_connect():
            raise Exception("Server not connected")

        event = {
            "type": "any_client_connected",
        }
        self.websocket.send(json.dumps(event))
        try:
            resp = self.websocket.recv()
            res = json.loads(resp).get("status", "error")
            return res == "ok"
        except Exception as e:
            self.logger.error(f"any_client_connected error: {e}")
            self.logger.error(traceback.format_exc())
            return False


class TestClient:
    def __init__(self, port=61863):
        self.host = Server._host
        self.port = port
        self.logger = logger
        self.id = uuid.uuid4().hex
        self.uri = f"ws://{self.host}:{self.port}/ws?id={self.id}"
        self.logger.info("TestClient try to running")

    def _run(self):
        try:
            self._hello()
            self._auth()
            self._close()
            self._hello()
        except ConnectionRefusedError:
            self.logger.error(f"连接被拒绝 port: {self.port}")
        except Exception as e:
            self.logger.error(f"TestClient error: {e}")
            self.logger.error(traceback.format_exc())
        finally:
            self.logger.info("TestClient end")

    def _hello(self):
        with connect(self.uri) as websocket:
            event = {
                "type": "hello_client",
                "data": "Hello Server!",
            }
            websocket.send(json.dumps(event))
            message = websocket.recv()
            event: dict = json.loads(message)
            if event.get("type", "") == "hello_server":
                self.logger.info(f"Client Received: {message}")

    def _auth(self):
        with connect(self.uri) as websocket:
            event = {
                "type": "rodin_auth",
                "data": "Auth Server!",
            }
            websocket.send(json.dumps(event))
            message = websocket.recv()
            event: dict = json.loads(message)
            if event.get("type", "") == "rodin_auth_return":
                self.logger.info(f"验证成功: {message}")

    def _close(self):
        with connect(self.uri) as websocket:
            event = {
                "type": "close_server",
                "data": "Close Server!",
            }
            websocket.send(json.dumps(event))

    @classmethod
    def run_test(cls, port=None):
        if port is None:
            port = Server._port
        client = cls(port)
        client._run()


server = None


def run(port_range):
    global server
    for p in range(*port_range):
        try:
            server = Server(p)
            server.run()
            break
        except OSError:
            logger.warning(f"Port {p} is in use")
        except Exception:
            traceback.print_exc()


def closeserver():
    global server
    server._direct_close()
