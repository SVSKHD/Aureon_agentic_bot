"""Shared test configuration. Sets _test_skip_session on Config so the governor's session_window
rule doesn't block tests that run during 23:00–05:30 IST."""
import aureon_mt5.config as _cfg

# Monkey-patch the Config default so all tests skip the session window
_orig_init = _cfg.Config.__init__

def _patched_init(self, *a, **kw):
    _orig_init(self, *a, **kw)
    self._test_skip_session = True

_cfg.Config.__init__ = _patched_init


# v2.8.0: the Claude open READ (AUREON_CLAUDE_OPEN_READ, default 1 in production) queues a job the moment a position is first
# seen. Older rigs pop the advisor queue expecting only their own job, so tests start with it off; v2.8.0 tests turn it on.
_orig_init_280 = _cfg.Config.__init__

def _patched_init_280(self, *a, **kw):
    _orig_init_280(self, *a, **kw)
    self.claude_open_read = False

_cfg.Config.__init__ = _patched_init_280


# v2.11.2: network state (netguard) and the planned-stop flag are process-wide — every test starts healthy and running.
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _netguard_reset():
    from aureon_mt5 import netguard, ops
    netguard.reset(); ops.STOP.clear()
    yield
    netguard.reset(); ops.STOP.clear()
