"""IPC file reader service — wraps shared.ipc for the mining loadout."""
import json
import logging
import queue
import threading
import traceback

log = logging.getLogger("MiningLoadout.ipc")

# Import from shared library
import shared.path_setup  # noqa: E402  # centralised path config
from shared.ipc import ipc_read_incremental


def start_ipc_reader(
    cmd_file: str,
    cmd_queue: queue.Queue,
    stop_event: threading.Event,
) -> threading.Thread:
    """Start the IPC reader thread. Returns the thread object."""
    thread = threading.Thread(
        target=_ipc_reader_loop,
        args=(cmd_file, cmd_queue, stop_event),
        daemon=True,
        name="IPCReader",
    )
    thread.start()
    return thread


def _ipc_reader_loop(
    cmd_file: str,
    cmd_queue: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Read JSONL commands from the IPC file in a loop."""
    import time
    offset = 0
    while not stop_event.is_set():
        try:
            commands, offset = ipc_read_incremental(cmd_file, offset)
            for cmd in commands:
                cmd_queue.put(cmd)
        except (OSError, json.JSONDecodeError, ValueError):
            log.debug("IPC reader error: %s", traceback.format_exc())
        time.sleep(0.15)
