"""Mining Signals Dev Mode — user interface.

    from devmode.ui import open_dev_mode
    open_dev_mode(parent=some_widget)

Backend selection lives in ``devmode.ui.backend`` (real ``devmode.api``,
or the demo ``FakeBackend`` when SC_DEVMODE_FAKE=1 or the real one is
missing).
"""

from .window import DevModeWindow, open_dev_mode

__all__ = ["DevModeWindow", "open_dev_mode"]
