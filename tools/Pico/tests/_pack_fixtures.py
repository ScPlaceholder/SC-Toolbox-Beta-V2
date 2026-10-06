"""Small outfit packs and a pretend pack server, for the pack tests. Not a test file.

The packs are built here in the real format (pico/packs.py: a .tar.xz of index.json, <loop>.webp and
<loop>.anchors.json) from made-up loop files a few kilobytes long, so the tests need no art on the PC and
finish in seconds. The server is http.server on 127.0.0.1, on a port the system picks, and it writes down
every request, so a test can say "nothing was asked for" and mean it.
"""
import hashlib
import io
import json
import lzma
import socket
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# One loop per mood is enough for sprites.LoopChooser to accept the outfit.
LOOPS = ("idle_look_default", "determined_focused", "sad_sad", "happy_happy", "startled_surprised",
         "annoyed_angry", "confused_confused", "weapon_draw_focused_held")
OUTFITS = {"o02": "anvil", "o05": "banu", "o08": "drake", "o16": "origin"}


def noise(tag: str, n: int) -> bytes:
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(("%s/%d" % (tag, i)).encode()).digest()
        i += 1
    return out[:n]


def make_pack(code: str, outfit: str, salt: str = "", loops=LOOPS, extra=(), size: int = 600) -> bytes:
    """A pack's bytes. `salt` changes the art (so the checksum); `extra` adds raw (name, bytes) members."""
    files = []
    for name in loops:
        files.append((name + ".webp", b"RIFF" + noise("%s/%s/%s" % (code, name, salt), size)))
        files.append((name + ".anchors.json", json.dumps({"size": [10, 10], "belly_w": 4, "frames": [{}]}).encode()))
    index = {"format": 1, "code": code, "outfit": outfit, "name": outfit.title(),
             "loops": [{"loop": n[:-5], "file": n, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(),
                        "anchors": True} for n, b in files if n.endswith(".webp")]}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, b in [("index.json", json.dumps(index).encode())] + files + list(extra):
            ti = tarfile.TarInfo(name)
            ti.size = len(b)
            tar.addfile(ti, io.BytesIO(b))
    return lzma.compress(raw.getvalue(), format=lzma.FORMAT_XZ, preset=1)


def make_manifest(blobs: dict, default: str = "o08", version: str = "t1") -> dict:
    """packs.json for {code: pack bytes}, in the shape elah-audio/_pico_pals_notice/build_packs.py writes."""
    rows = []
    for code in sorted(blobs):
        b = blobs[code]
        rows.append({"code": code, "name": OUTFITS[code].title(), "outfit": OUTFITS[code], "pack": code + ".tar.xz",
                     "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(), "loops": len(LOOPS),
                     "unpacked_bytes": len(LOOPS) * 700})
    return {"name": "Pico Pals", "format": 1, "version": version, "default": default, "outfits": rows}


def site(blobs: dict, **kw) -> dict:
    """What the server holds: every pack under its file name, and packs.json."""
    files = {code + ".tar.xz": b for code, b in blobs.items()}
    files["packs.json"] = json.dumps(make_manifest(blobs, **kw)).encode()
    return files


class PretendServer:
    """files: {name: bytes}, or {name: ("redirect", url)}. hits: every path asked for, in order."""

    def __init__(self, files: dict, delay: float = 0.0, piece: int = 2048):
        self.files, self.hits, self.delay, self.piece = dict(files), [], delay, piece
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                # The real host (Cloudflare) answers 403 to Python's default "Python-urllib/x.y" agent. Found
                # 2026-10-06 on the first real upload; this server now refuses it too, so every fetch test
                # fails if the store stops naming itself.
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                    self.send_error(403)
                    return
                body = outer.files.get(self.path.rsplit("/", 1)[-1])
                if not self.path.startswith("/packs/") or body is None:
                    self.send_error(404)
                    return
                if isinstance(body, tuple):
                    self.send_response(302)
                    self.send_header("Location", body[1])
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for i in range(0, len(body), outer.piece):
                        self.wfile.write(body[i:i + outer.piece])
                        self.wfile.flush()
                        if outer.delay:
                            time.sleep(outer.delay)
                except OSError:
                    pass                          # the client hung up (a cancelled download)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d/packs" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def packs_asked(self) -> list:
        return [h.rsplit("/", 1)[-1] for h in self.hits]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_url() -> str:
    """An address on this PC that nothing is listening on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d/packs" % port
