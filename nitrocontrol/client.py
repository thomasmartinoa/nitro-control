"""Tiny client for the nitro-controld socket (newline-delimited JSON)."""

import json
import os
import socket

from . import SOCKET_PATH


class NitroError(Exception):
    pass


class DaemonUnavailable(NitroError):
    pass


class Client:
    def __init__(self, path=None, timeout=3.0):
        self.path = path or os.environ.get("NITRO_SOCKET", SOCKET_PATH)
        self.timeout = timeout

    def call(self, cmd, **args):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                s.sendall((json.dumps({"cmd": cmd, "args": args}) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise DaemonUnavailable("nitro-controld is not running (%s)" % e)
        except OSError as e:
            raise DaemonUnavailable("cannot talk to nitro-controld: %s" % e)
        try:
            reply = json.loads(buf)
        except ValueError:
            raise NitroError("malformed reply from daemon")
        if not reply.get("ok"):
            raise NitroError(reply.get("error", "request failed"))
        return reply.get("data")

    def available(self):
        try:
            self.call("ping")
            return True
        except NitroError:
            return False
