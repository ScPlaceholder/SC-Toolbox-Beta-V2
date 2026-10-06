"""A pretend reference site for tests/test_eyes_reference.py: http.server on 127.0.0.1 that counts requests.

It behaves like the real host in the two ways that have already cost something:
  * it answers 403 to Python's default "Python-urllib/x.y" agent, so every fetch test fails if the store
    stops naming itself;
  * with `front_page` set it answers a MISSING address with that page and status 200, as the real host does,
    so "it arrived" is never mistaken for "it is there".
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRONT_PAGE = b"<!DOCTYPE html>\n<html><head><title>ship fingerprints</title></head><body>front page</body></html>\n"

SHIPS_V1 = (("alpha", "Alpha", "alphas"), ("beta", "Beta", "alphas"))
SHIPS_V2 = (("gamma", "Gamma", "gammas"),)


def blob(tag: str, n: int = 6000) -> bytes:
    """Made-up file content, different for every tag, the same every run."""
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(("%s/%d" % (tag, i)).encode()).digest()
        i += 1
    return out[:n]


def ref(path: str, data: bytes) -> dict:
    return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def table_files(tid: str, rows: int = 4, folder: str = "fingerprint_table") -> dict:
    """{site path: bytes} for one table: the array (made-up bytes) and its row labels (real JSON)."""
    labels = [{"ship": "alpha", "view": "ext", "room": "exterior", "room_src": "eye", "src": "vid%d" % i, "t": 2.0 * i}
              for i in range(rows)]
    return {"%s/%s.f16.npy" % (folder, tid): blob(tid),
            "%s/%s.rows.json" % (folder, tid): json.dumps(labels).encode("utf-8")}


def table_entry(tid: str, files: dict, since: int, batch: str, rows: int = 4, folder: str = "fingerprint_table",
                space: str = "m", ships=("alpha",)) -> dict:
    a, r = "%s/%s.f16.npy" % (folder, tid), "%s/%s.rows.json" % (folder, tid)
    return {"id": tid, "since": since, "batch": batch, "model": "m", "built_from": "emb/m", "space": space,
            "output": "pooled", "centred": False, "rows": rows, "width": 8,
            "rows_per_ship": {ships[0]: rows}, "files": {"f16_npy": ref(a, files[a]), "rows": ref(r, files[r])}}


def make_site(version: int = 1, extra_unknown_keys: bool = False) -> dict:
    """{site path: bytes} for a whole pretend site at index version 1 or 2. Version 2 is version 1 plus one
    batch: every version-1 file and entry is there unchanged, which is the rule the real site keeps."""
    files: dict = {}
    files.update(table_files("t_one"))
    files.update(table_files("t_two"))
    files["emb/m/vid0.npy"] = blob("emb/vid0", 900)
    man1 = {"files": [dict(ref(p, d), contents="made up") for p, d in sorted(files.items())]}
    files["manifest.json"] = json.dumps(man1).encode("utf-8")
    index = {"format": 1, "version": 1, "date": "2026-10-06", "limits": "cannot name a ship from one frame",
             "models": {"m": {"name": "pretend model"}},
             "batches": [{"id": "b0001", "since": 1, "manifest": dict(ref("manifest.json", files["manifest.json"]),
                                                                      files=len(man1["files"])),
                          "ships": [s[0] for s in SHIPS_V1]}],
             "tables": [table_entry("t_one", files, 1, "b0001"), table_entry("t_two", files, 1, "b0001", space="n")],
             "ships": [{"key": k, "name": n, "family": f, "since": 1,
                        "frames": {"gallery": {"exterior": 2, "interior": 2, "cockpit": 1}},
                        "tables": {"t_one": 4}, "videos": [{"id": "vid0", "role": "gallery", "batch": "b0001"}]}
                       for k, n, f in SHIPS_V1],
             "history": [{"version": 1}]}
    if version >= 2:
        new = table_files("b0002_m", folder="fingerprint_table/b0002")
        files.update(new)
        man2 = {"files": [ref(p, d) for p, d in sorted(new.items())]}
        files["batches/b0002/manifest.json"] = json.dumps(man2).encode("utf-8")
        index["version"] = 2
        index["batches"].append({"id": "b0002", "since": 2, "ships": [s[0] for s in SHIPS_V2],
                                 "manifest": dict(ref("batches/b0002/manifest.json",
                                                      files["batches/b0002/manifest.json"]), files=2)})
        index["tables"].append(table_entry("b0002_m", files, 2, "b0002", folder="fingerprint_table/b0002",
                                           ships=("gamma",)))
        index["ships"] += [{"key": k, "name": n, "family": f, "since": 2, "frames": {}, "tables": {"b0002_m": 4},
                            "videos": [{"id": "vid9", "role": "gallery", "batch": "b0002"}]} for k, n, f in SHIPS_V2]
        index["history"].append({"version": 2})
        if extra_unknown_keys:
            index["something_a_later_version_added"] = {"any": "thing"}
            index["tables"][-1]["a_new_field"] = 7
    files["index.json"] = json.dumps(index, indent=1).encode("utf-8")
    return files


class PretendSite:
    """files: {site path: bytes}, or {site path: ("redirect", url)}. hits: every path asked for, in order.
    agents: the User-Agent of every request."""

    def __init__(self, files: dict, front_page: bytes = b""):
        self.files, self.hits, self.agents, self.front_page = dict(files), [], [], front_page
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.lstrip("/")
                outer.hits.append(path)
                outer.agents.append(self.headers.get("User-Agent", ""))
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                    self.send_error(403)
                    return
                body = outer.files.get(path)
                if body is None:
                    if not outer.front_page:
                        self.send_error(404)
                        return
                    body = outer.front_page
                if isinstance(body, tuple):
                    self.send_response(302)
                    self.send_header("Location", body[1])
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def asked(self, suffix: str = "") -> list:
        return [h for h in self.hits if h.endswith(suffix)]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
