"""Where the bot's MetaTrader5 API comes from (docs/LINUX_SETUP.md).

AUREON_MT5_BACKEND=native    (default) `import MetaTrader5` — Windows, or Windows Python under Wine.
AUREON_MT5_BACKEND=mt5linux  the bot runs on native Linux Python; MetaTrader5 lives in Windows Python
                             under Wine behind the mt5linux server (an rpyc classic server):
                                 wine python.exe -m mt5linux --host 127.0.0.1 -p 18812
                             AUREON_MT5LINUX_HOST (default 127.0.0.1), AUREON_MT5LINUX_PORT (default 18812).

The mt5linux PyPI client is not used: it sends arguments as Python source text (a numpy-2 float
becomes `np.float64(…)`, which the Wine side cannot evaluate) and returns live remote references
(every attribute read is a round trip, and they cannot be JSON-dumped into state files). Here the
arguments go as JSON (numpy scalars converted, datetimes tagged), and results are turned into plain
data inside Wine and rebuilt as local named tuples with the same type name and fields; numpy arrays
(copy_rates_*, copy_ticks_*) come back as real arrays.

There is no automatic retry: a dropped connection raises ConnectionError from that call and the
next call reconnects, so an order_send can never be sent twice by this layer.
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import os
import threading

_lock = threading.Lock()
_api = None

# Runs once inside the Wine Python, on every (re)connect.
_SERVER_HELPER = r'''
import datetime as _aureon_dt, json as _aureon_json
import MetaTrader5 as mt5

def _aureon_hook(d):
    if "__dt__" in d and len(d) == 1:
        return _aureon_dt.datetime.fromisoformat(d["__dt__"])
    return d

def _aureon_plain(x):
    if x is None or isinstance(x, (bool, int, float, str, bytes)):
        return x
    if hasattr(x, "_asdict"):
        return ("__nt__", type(x).__name__, {k: _aureon_plain(v) for k, v in x._asdict().items()})
    if isinstance(x, (tuple, list)):
        items = [_aureon_plain(v) for v in x]
        return ("__t__", items) if isinstance(x, tuple) else items
    if isinstance(x, dict):
        return {k: _aureon_plain(v) for k, v in x.items()}
    return x  # numpy arrays and scalars pickle as they are

def _aureon_call(name, payload):
    args, kwargs = _aureon_json.loads(payload, object_hook=_aureon_hook)
    return _aureon_plain(getattr(mt5, name)(*args, **kwargs))

def _aureon_constants():
    return {k: v for k, v in vars(mt5).items() if k.isupper() and isinstance(v, (int, float, str))}
'''


def _jsonable(x):
    """Local args → JSON-safe: numpy scalars to Python, datetimes tagged, tuples to lists."""
    if isinstance(x, _dt.datetime):
        return {"__dt__": x.isoformat()}
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    item = getattr(x, "item", None)          # numpy scalar (np.float64, np.int64, np.bool_)
    if callable(item) and not isinstance(x, (str, bytes)) and getattr(x, "shape", None) == ():
        return item()
    return x


_types: dict[tuple, type] = {}


def _rebuild(x):
    if isinstance(x, tuple) and x and x[0] == "__nt__":
        _, name, fields = x
        key = (name, tuple(fields))
        cls = _types.get(key)
        if cls is None:
            cls = _types[key] = collections.namedtuple(name, list(fields))
        return cls(**{k: _rebuild(v) for k, v in fields.items()})
    if isinstance(x, tuple) and x and x[0] == "__t__":
        return tuple(_rebuild(v) for v in x[1])
    if isinstance(x, list):
        return [_rebuild(v) for v in x]
    if isinstance(x, dict):
        return {k: _rebuild(v) for k, v in x.items()}
    return x


class Mt5Remote:
    """The MetaTrader5 module's surface (functions + UPPER_CASE constants) over mt5linux."""

    def __init__(self, host: str = "127.0.0.1", port: int = 18812, *, connect=None):
        import rpyc  # only needed for this backend

        self._rpyc = rpyc
        self._host, self._port = host, int(port)
        self._factory = connect or rpyc.classic.connect
        self._conn = None
        self._call = None
        self._clock = threading.Lock()
        self._open()
        consts = rpyc.classic.obtain(self._conn.eval("_aureon_constants()"))
        self.__dict__.update(consts)

    def _open(self):
        conn = self._factory(self._host, self._port)
        conn._config["sync_request_timeout"] = 300
        conn.execute(_SERVER_HELPER)
        self._conn, self._call = conn, conn.namespace["_aureon_call"]

    def _invoke(self, name: str, args, kwargs):
        payload = json.dumps([_jsonable(list(args)), _jsonable(kwargs)])
        with self._clock:
            if self._conn is None:
                try:
                    self._open()
                except OSError as e:
                    raise ConnectionError(f"mt5linux server {self._host}:{self._port} unreachable: {e}") from e
            try:
                out = self._rpyc.classic.obtain(self._call(name, payload))
            except (EOFError, OSError) as e:      # server restarted / socket gone: reconnect next call
                self._conn = self._call = None
                raise ConnectionError(f"mt5linux connection lost during {name}(): {e}") from e
        return _rebuild(out)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            return self._invoke(name, args, kwargs)

        call.__name__ = name
        return call


def backend() -> str:
    return (os.environ.get("AUREON_MT5_BACKEND", "native") or "native").strip().lower()


def load():
    """The MetaTrader5 module (native) or an Mt5Remote (mt5linux). One instance per process."""
    global _api
    with _lock:
        if _api is None:
            b = backend()
            if b == "native":
                import MetaTrader5 as m  # type: ignore

                _api = m
            elif b == "mt5linux":
                _api = Mt5Remote(
                    os.environ.get("AUREON_MT5LINUX_HOST", "127.0.0.1").strip() or "127.0.0.1",
                    int(os.environ.get("AUREON_MT5LINUX_PORT", "18812") or 18812),
                )
            else:
                raise ValueError(f"AUREON_MT5_BACKEND={b!r}: use 'native' or 'mt5linux'")
        return _api


def reset() -> None:
    """Forget the instance (tests; or after a failed connect so the next load() tries again)."""
    global _api
    with _lock:
        _api = None
