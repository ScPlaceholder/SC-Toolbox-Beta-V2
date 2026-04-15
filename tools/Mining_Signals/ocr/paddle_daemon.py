"""PaddleOCR sidecar daemon — runs under a separate Python 3.13 embed.

This script is NOT invoked by the main Mining Signals app directly.
It is spawned as a subprocess by ``paddle_client.py``, which runs
under the main Python 3.14 interpreter. The main app cannot import
``paddlepaddle`` itself because paddlepaddle has no Python 3.14
wheels as of April 2026, so we isolate it in a Python 3.13 embed.

## Protocol

Binary line-framed JSON over stdin/stdout:

    Request:   <4-byte big-endian length> <PNG bytes>
    Response:  <4-byte big-endian length> <UTF-8 JSON>

Response JSON shape::

    {
      "ok": true,
      "texts": [
        {"text": "8261", "conf": 0.99, "y_mid": 138},
        {"text": "31%",  "conf": 0.98, "y_mid": 175},
        ...
      ],
      "elapsed_ms": 3450
    }

Or on failure::

    {"ok": false, "error": "message"}

The daemon loads PaddleOCR once at startup (~5-20 s) and then
services requests in a tight loop. The first request is slow
(~3-5 s for the first inference); subsequent requests should
be <1 s on CPU.

## Stderr

All stderr output is forwarded to the client for debugging but
MUST NOT contain anything the client expects on stdout. PaddleOCR
itself emits some informational messages on stderr during model
initialization — those are fine, they flow through without being
interpreted.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import time
import warnings

# Cap CPU thread count BEFORE importing any numeric libs. These env
# vars control OpenMP / MKL / OpenBLAS thread pools used by
# paddlepaddle internals. Without caps, a single inference can use
# all available CPU cores — this was pegging user machines at 90%
# CPU across all cores during scanning. 2 threads per inference is
# plenty for tiny digit crops. (Also set by the parent paddle_client
# in the subprocess env, but duplicated here in case env var
# propagation was blocked or stripped.)
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("PADDLE_NUM_THREADS", "2")
os.environ.setdefault("FLAGS_use_mkldnn", "false")

# Silence noisy warnings that pollute stderr during normal operation.
warnings.filterwarnings("ignore")
os.environ.setdefault("GLOG_minloglevel", "2")


def _log(msg: str) -> None:
    """Send a log line to stderr (visible to client, not stdin/stdout)."""
    sys.stderr.write(f"[paddle_daemon] {msg}\n")
    sys.stderr.flush()


# Maximum request size: 50 MB. A full-screen 4K PNG is ~25 MB worst case;
# anything larger is either a bug or malformed input.
_MAX_REQUEST_BYTES = 50 * 1024 * 1024


def _read_request() -> bytes | None:
    """Read one framed request from stdin. Returns raw PNG bytes, or None at EOF."""
    length_bytes = sys.stdin.buffer.read(4)
    if not length_bytes or len(length_bytes) < 4:
        return None
    (length,) = struct.unpack(">I", length_bytes)
    if length == 0:
        return b""
    if length > _MAX_REQUEST_BYTES:
        _log(f"request too large ({length} bytes > {_MAX_REQUEST_BYTES}), rejecting")
        return None
    # Read exactly `length` bytes — handle partial reads
    buf = bytearray()
    while len(buf) < length:
        chunk = sys.stdin.buffer.read(length - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _write_response(payload: dict) -> None:
    """Write one framed JSON response to stdout."""
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack(">I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _init_ocr():
    """Load PaddleOCR. Expensive (~5-20 s)."""
    _log("loading PaddleOCR...")
    t0 = time.time()
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        lang="en",
    )
    _log(f"ready in {time.time() - t0:.1f} s")
    return ocr


def _serve_one(ocr, png_bytes: bytes) -> dict:
    """Run OCR on one PNG blob and return a JSON-serializable dict."""
    try:
        from PIL import Image
        import numpy as np

        t0 = time.time()
        img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
        results = ocr.predict(img)
        elapsed_ms = int((time.time() - t0) * 1000)

        if not results:
            return {"ok": True, "texts": [], "elapsed_ms": elapsed_ms}

        page = results[0]
        texts_attr = getattr(page, "rec_texts", None) or page.get("rec_texts", [])
        boxes_attr = getattr(page, "rec_boxes", None)
        if boxes_attr is None:
            boxes_attr = page.get("rec_boxes", [])
        scores_attr = getattr(page, "rec_scores", None) or page.get("rec_scores", [])

        out_texts = []
        for i, text in enumerate(texts_attr):
            conf = float(scores_attr[i]) if i < len(scores_attr) else 0.0
            x_mid = -1
            y_mid = -1
            try:
                box = boxes_attr[i]
                if hasattr(box[0], "__iter__"):
                    xs = [float(p[0]) for p in box]
                    ys = [float(p[1]) for p in box]
                else:
                    xs = [float(box[0]), float(box[2])]
                    ys = [float(box[1]), float(box[3])]
                x_mid = int(sum(xs) / len(xs))
                y_mid = int(sum(ys) / len(ys))
            except Exception:
                pass
            out_texts.append({"text": str(text), "conf": conf, "x_mid": x_mid, "y_mid": y_mid})

        return {"ok": True, "texts": out_texts, "elapsed_ms": elapsed_ms}
    except Exception as exc:
        _log(f"request failed: {exc}")
        return {"ok": False, "error": str(exc)}


def main() -> None:
    ocr = _init_ocr()
    # Ready signal: tell the client we're accepting requests
    _write_response({"ok": True, "status": "ready"})
    _log("awaiting requests on stdin")

    while True:
        png = _read_request()
        if png is None:
            _log("stdin closed, exiting")
            return
        response = _serve_one(ocr, png)
        _write_response(response)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("interrupted")
    except Exception as exc:
        _log(f"fatal: {exc}")
        try:
            _write_response({"ok": False, "error": f"daemon fatal: {exc}"})
        except Exception:
            pass
        sys.exit(1)
