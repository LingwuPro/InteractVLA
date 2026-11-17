"""OpenPI transport with an inference timeout that accommodates first-call compilation."""

import inspect

from openpi_client import msgpack_numpy
from websockets.sync.client import connect


class PolicyClient:
    def __init__(self, host: str, port: int, timeout: float = 600.0):
        options = {"compression": None, "max_size": None, "open_timeout": 30}
        if "ping_interval" in inspect.signature(connect).parameters:
            options["ping_interval"] = None
        self.socket = connect(f"ws://{host}:{port}", **options)
        self.timeout = timeout
        self.metadata = msgpack_numpy.unpackb(self.socket.recv(timeout=30))
        self.packer = msgpack_numpy.Packer()

    def infer(self, observation: dict) -> dict:
        self.socket.send(self.packer.pack(observation))
        response = self.socket.recv(timeout=self.timeout)
        if isinstance(response, str):
            raise RuntimeError(response)
        return msgpack_numpy.unpackb(response)

    def close(self) -> None:
        self.socket.close()
