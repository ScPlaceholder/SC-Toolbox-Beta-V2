"""gguf_stitch.py - build a character model from a stock base GGUF + a small per-character tensor delta (stdlib only).

WHY THIS EXISTS (measured 2026-09-23 against the local Ollama 0.34.2):
    Modelfile `ADAPTER x.lora.gguf`, the CLI and POST /api/create {"adapters": {...}} ALL answer
        400 {"error":"LoRA adapters are no longer supported"}
    before looking at the base, the blob or the name. So a LoRA GGUF cannot be shipped to this Ollama at all.

What ships instead is a DELTA: the LoRA pre-merged into exactly the tensors it touches (q/k/v/o projection weights
of all 28 layers, Q8_0), built once at build time by build_character_delta.py. At provision time this module splices
those tensors into the base GGUF Ollama already pulled (qwen2.5:1.5b) and streams the result into Ollama as a blob.
GGUF allows a different quant type per tensor, so Q4_K_M base tensors + Q8_0 attention tensors is a valid file.

The stitched file is never written to disk: `Stitch.chunks()` generates it deterministically, so provisioning makes
one pass to hash it (the blob digest must be known before upload) and a second pass to upload it. Zero temp disk.

    s = Stitch(base_gguf_path, delta_gguf_path)
    s.size                      # exact byte length of the stitched GGUF
    s.sha256(progress)          # "sha256:<hex>"   (pass 1)
    for chunk in s.chunks(): ...                  (pass 2, same bytes)

Validation is strict: every delta tensor must exist in the base with identical dims, the delta must declare the same
architecture and base fingerprint (tensor-name/shape digest) it was built against. A base that drifted upstream
fails loudly instead of producing a subtly wrong character.
"""
from __future__ import annotations

import hashlib
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional

MAGIC = b"GGUF"
CHUNK = 4 << 20

# value types
T_U8, T_I8, T_U16, T_I16, T_U32, T_I32, T_F32, T_BOOL, T_STR, T_ARR, T_U64, T_I64, T_F64 = range(13)
_SCALAR = {T_U8: "<B", T_I8: "<b", T_U16: "<H", T_I16: "<h", T_U32: "<I", T_I32: "<i", T_F32: "<f",
           T_BOOL: "<?", T_U64: "<Q", T_I64: "<q", T_F64: "<d"}

# ggml type -> (block elements, bytes per block)
GGML_TYPES = {
    0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24), 8: (32, 34), 9: (32, 36),
    10: (256, 84), 11: (256, 110), 12: (256, 144), 13: (256, 176), 14: (256, 210), 15: (256, 292),
    16: (256, 66), 17: (256, 74), 18: (256, 98), 19: (256, 50), 20: (32, 18), 21: (256, 110), 22: (256, 82),
    23: (256, 136), 24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8), 29: (256, 56), 30: (1, 2),
}
TYPE_NAMES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 10: "Q2_K", 11: "Q3_K",
              12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 30: "BF16"}


class GGUFError(ValueError):
    pass


@dataclass
class TensorInfo:
    name: str
    dims: tuple
    ggml_type: int
    offset: int            # relative to the data section

    @property
    def nbytes(self) -> int:
        if self.ggml_type not in GGML_TYPES:
            raise GGUFError(f"{self.name}: unknown ggml type {self.ggml_type}")
        blk, size = GGML_TYPES[self.ggml_type]
        n = 1
        for d in self.dims:
            n *= d
        if n % blk:
            raise GGUFError(f"{self.name}: {n} elements not a multiple of block {blk}")
        return n // blk * size


class _Reader:
    def __init__(self, f: BinaryIO):
        self.f = f

    def take(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise GGUFError("truncated GGUF")
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def string(self) -> str:
        return self.take(self.u64()).decode("utf-8", "replace")

    def value(self, t: int, keep: bool):
        if t in _SCALAR:
            fmt = _SCALAR[t]
            return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]
        if t == T_STR:
            return self.string()
        if t == T_ARR:
            et, n = self.u32(), self.u64()
            if et in _SCALAR and not keep:          # skip big numeric arrays without decoding them
                self.take(struct.calcsize(_SCALAR[et]) * n)
                return None
            out = [self.value(et, keep) for _ in range(n)]
            return out if keep else None
        raise GGUFError(f"unknown GGUF value type {t}")


class GGUFFile:
    """Parsed header of a GGUF v2/v3 file. Keeps the raw KV bytes so a writer can copy them verbatim."""

    def __init__(self, path: Path | str, keep_arrays: bool = False):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            r = _Reader(f)
            if r.take(4) != MAGIC:
                raise GGUFError(f"{self.path.name}: not a GGUF file")
            self.version = r.u32()
            if self.version not in (2, 3):
                raise GGUFError(f"{self.path.name}: GGUF version {self.version} unsupported")
            n_tensors, n_kv = r.u64(), r.u64()
            kv_start = f.tell()
            self.kv: dict = {}
            for _ in range(n_kv):
                key = r.string()
                t = r.u32()
                self.kv[key] = r.value(t, keep_arrays)
            kv_end = f.tell()
            self.n_kv = n_kv
            self.tensors: list[TensorInfo] = []
            for _ in range(n_tensors):
                name = r.string()
                nd = r.u32()
                dims = tuple(r.u64() for _ in range(nd))
                self.tensors.append(TensorInfo(name, dims, r.u32(), r.u64()))
            self.alignment = int(self.kv.get("general.alignment", 32))
            self.data_start = _align(f.tell(), self.alignment)
            f.seek(kv_start)
            self.kv_raw = f.read(kv_end - kv_start)
        self.by_name = {t.name: t for t in self.tensors}

    def fingerprint(self) -> str:
        """Digest of (name, dims) for every tensor, in order. Identifies the base LAYOUT, not its weights."""
        h = hashlib.sha256()
        for t in self.tensors:
            h.update(f"{t.name}:{','.join(map(str, t.dims))};".encode())
        return h.hexdigest()


def _align(n: int, a: int) -> int:
    return n + (-n % a)


def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _enc_kv_str(key: str, val: str) -> bytes:
    return _enc_str(key) + struct.pack("<I", T_STR) + _enc_str(val)


def file_sha256(path: Path | str, progress: Optional[Callable[[int, int], None]] = None) -> str:
    p = Path(path)
    total, done, h = p.stat().st_size, 0, hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, total)
    return "sha256:" + h.hexdigest()


class Stitch:
    """base GGUF + delta GGUF -> stitched GGUF, as a deterministic byte stream."""

    DELTA_KEYS = ("suitmk2.delta.speaker", "suitmk2.delta.base_fingerprint")

    def __init__(self, base: Path | str, delta: Path | str, extra_kv: Optional[dict] = None):
        self.base, self.delta = GGUFFile(base), GGUFFile(delta)
        missing = [k for k in self.DELTA_KEYS if k not in self.delta.kv]
        if missing:
            raise GGUFError(f"{self.delta.path.name}: not a SuitMk2 delta (missing {missing})")
        self.speaker = str(self.delta.kv["suitmk2.delta.speaker"])
        want_fp, have_fp = self.delta.kv["suitmk2.delta.base_fingerprint"], self.base.fingerprint()
        if want_fp != have_fp:
            raise GGUFError(f"base layout mismatch: delta built for {want_fp[:12]}, base is {have_fp[:12]}")
        arch_d, arch_b = self.delta.kv.get("suitmk2.delta.arch"), self.base.kv.get("general.architecture")
        if arch_d and arch_d != arch_b:
            raise GGUFError(f"architecture mismatch: delta {arch_d}, base {arch_b}")
        for k, v in self.delta.kv.items():          # suitmk2.require.<base key> = value the base must carry
            if k.startswith("suitmk2.require."):
                have = self.base.kv.get(k[len("suitmk2.require."):])
                if str(have) != str(v):
                    raise GGUFError(f"base {k[len('suitmk2.require.'):]} is {have!r}, delta requires {v!r}")
        for t in self.delta.tensors:
            b = self.base.by_name.get(t.name)
            if b is None:
                raise GGUFError(f"delta tensor {t.name} not in base")
            if b.dims != t.dims:
                raise GGUFError(f"{t.name}: dims {t.dims} vs base {b.dims}")
        self.replaced = {t.name for t in self.delta.tensors}
        kv = {"suitmk2.speaker": self.speaker}
        kv.update({k: str(v) for k, v in (extra_kv or {}).items()})
        self._n_extra = len(kv)
        self._extra = b"".join(_enc_kv_str(k, v) for k, v in sorted(kv.items()))
        self._plan()

    def _plan(self) -> None:
        a = self.base.alignment
        self.layout = []                     # (TensorInfo out, source GGUFFile, source TensorInfo)
        off = 0
        for t in self.base.tensors:
            src_file = self.delta if t.name in self.replaced else self.base
            src = src_file.by_name[t.name]
            out = TensorInfo(t.name, t.dims, src.ggml_type, off)
            self.layout.append((out, src_file, src))
            off = _align(off + src.nbytes, a)
        infos = b"".join(_enc_str(o.name) + struct.pack("<I", len(o.dims))
                         + b"".join(struct.pack("<Q", d) for d in o.dims)
                         + struct.pack("<IQ", o.ggml_type, o.offset) for o, _, _ in self.layout)
        head = MAGIC + struct.pack("<I", 3) + struct.pack("<QQ", len(self.layout), self.base.n_kv + self._n_extra)
        self._header = head + self.base.kv_raw + self._extra + infos
        self._header += b"\0" * (-len(self._header) % a)
        last_out, _, last_src = self.layout[-1]          # the LAST tensor is not padded after its data
        self.size = len(self._header) + last_out.offset + last_src.nbytes

    def chunks(self, chunk: int = CHUNK) -> Iterator[bytes]:
        yield self._header
        handles: dict = {}
        try:
            pos = 0
            for out, src_file, src in self.layout:
                if out.offset > pos:
                    yield b"\0" * (out.offset - pos)
                    pos = out.offset
                f = handles.get(src_file.path)
                if f is None:
                    f = handles[src_file.path] = open(src_file.path, "rb")
                f.seek(src_file.data_start + src.offset)
                left = src.nbytes
                while left:
                    b = f.read(min(chunk, left))
                    if not b:
                        raise GGUFError(f"{src_file.path.name}: truncated in {src.name}")
                    left -= len(b)
                    pos += len(b)
                    yield b
        finally:
            for f in handles.values():
                f.close()

    def sha256(self, progress: Optional[Callable[[int, int], None]] = None) -> str:
        h, done = hashlib.sha256(), 0
        for b in self.chunks():
            h.update(b)
            done += len(b)
            if progress:
                progress(done, self.size)
        if done != self.size:
            raise GGUFError(f"stitch produced {done} bytes, planned {self.size}")
        return "sha256:" + h.hexdigest()

    def summary(self) -> dict:
        types: dict = {}
        for out, _, _ in self.layout:
            n = TYPE_NAMES.get(out.ggml_type, str(out.ggml_type))
            types[n] = types.get(n, 0) + 1
        return {"speaker": self.speaker, "tensors": len(self.layout), "replaced": len(self.replaced),
                "size": self.size, "types": types}


# ---- selftest: synthetic GGUFs, no model files needed -----------------------------------------------------------
def write_test_gguf(path: Path, kv: dict, tensors: list, alignment: int = 32) -> None:
    """Tiny GGUF writer for tests: kv values are str or int (u32); tensors are (name, dims, type, bytes)."""
    body_kv = b""
    for k, v in kv.items():
        body_kv += _enc_str(k) + (struct.pack("<I", T_STR) + _enc_str(v) if isinstance(v, str)
                                  else struct.pack("<II", T_U32, v))
    infos, data, off = b"", b"", 0
    for name, dims, t, raw in tensors:
        infos += _enc_str(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims)
        infos += struct.pack("<IQ", t, off)
        data += raw + b"\0" * (-len(raw) % alignment)
        off = _align(off + len(raw), alignment)
    head = MAGIC + struct.pack("<IQQ", 3, len(tensors), len(kv)) + body_kv + infos
    head += b"\0" * (-len(head) % alignment)
    last = tensors[-1][3]
    pad = -len(last) % alignment
    path.write_bytes(head + (data[:-pad] if pad else data))


def _selftest() -> int:
    import tempfile
    results = []

    def case(name, ok):
        results.append((name, bool(ok)))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a = bytes(range(256)) * 2                     # 512 B = 128 F32
        ffn = bytes(reversed(a))
        q4 = b"\x11" * 144 * 2                         # Q4_K, 512 elems
        write_test_gguf(td / "base.gguf", {"general.architecture": "qwen2", "general.alignment": 32},
                        [("tok.weight", (128,), 0, a), ("blk.0.attn_q.weight", (256, 2), 12, q4),
                         ("blk.0.ffn.weight", (128,), 0, ffn)])
        base = GGUFFile(td / "base.gguf")
        case("parse base: 3 tensors, arch", len(base.tensors) == 3 and base.kv["general.architecture"] == "qwen2")
        q8 = b"\x22" * 34 * 16                         # Q8_0, 512 elems
        write_test_gguf(td / "delta.gguf", {"suitmk2.delta.speaker": "elah", "suitmk2.delta.arch": "qwen2",
                                            "suitmk2.delta.base_fingerprint": base.fingerprint(),
                                            "suitmk2.require.general.architecture": "qwen2"},
                        [("blk.0.attn_q.weight", (256, 2), 8, q8)])
        write_test_gguf(td / "req.gguf", {"suitmk2.delta.speaker": "elah",
                                          "suitmk2.delta.base_fingerprint": base.fingerprint(),
                                          "suitmk2.require.general.finetune": "Instruct"},
                        [("blk.0.attn_q.weight", (256, 2), 8, q8)])
        try:
            Stitch(td / "base.gguf", td / "req.gguf")
            case("required base KV missing -> refused", False)
        except GGUFError as e:
            case("required base KV missing -> refused", "finetune" in str(e))
        s = Stitch(td / "base.gguf", td / "delta.gguf", {"suitmk2.delta.sha256": "sha256:ab"})
        blob = b"".join(s.chunks(chunk=7))
        case("planned size == generated size", len(blob) == s.size)
        (td / "out.gguf").write_bytes(blob)
        out = GGUFFile(td / "out.gguf")
        case("stitched: replaced tensor is Q8_0", out.by_name["blk.0.attn_q.weight"].ggml_type == 8)
        case("stitched: other tensors keep their type", out.by_name["blk.0.ffn.weight"].ggml_type == 0)
        case("stitched: base KV kept + suitmk2 KVs added", out.kv["general.architecture"] == "qwen2"
             and out.kv["suitmk2.speaker"] == "elah" and out.kv["suitmk2.delta.sha256"] == "sha256:ab"
             and out.n_kv == base.n_kv + 2)

        def read(g, n):
            t = g.by_name[n]
            with open(g.path, "rb") as f:
                f.seek(g.data_start + t.offset)
                return f.read(t.nbytes)
        case("stitched: replaced data is the delta's", read(out, "blk.0.attn_q.weight") == q8)
        case("stitched: untouched data is the base's", read(out, "blk.0.ffn.weight") == ffn
             and read(out, "tok.weight") == a)
        case("sha256 deterministic and equals hash of bytes",
             s.sha256() == "sha256:" + hashlib.sha256(blob).hexdigest() == Stitch(
                 td / "base.gguf", td / "delta.gguf", {"suitmk2.delta.sha256": "sha256:ab"}).sha256())
        write_test_gguf(td / "bad.gguf", {"suitmk2.delta.speaker": "elah",
                                          "suitmk2.delta.base_fingerprint": "0" * 64},
                        [("blk.0.attn_q.weight", (256, 2), 8, q8)])
        for label, delta in (("fingerprint mismatch refused", "bad.gguf"), ("non-delta refused", "base.gguf")):
            try:
                Stitch(td / "base.gguf", td / delta)
                case(label, False)
            except GGUFError:
                case(label, True)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"gguf_stitch selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if len(sys.argv) >= 3 and sys.argv[1] == "info":
        g = GGUFFile(sys.argv[2])
        types: dict = {}
        for t in g.tensors:
            n = TYPE_NAMES.get(t.ggml_type, t.ggml_type)
            types[n] = types.get(n, 0) + 1
        print(g.path.name, "v", g.version, "tensors", len(g.tensors), "kv", g.n_kv, "align", g.alignment, types)
        print("fingerprint", g.fingerprint())
        pat = sys.argv[3] if len(sys.argv) > 3 else "blk.0."
        for t in g.tensors:
            if pat in t.name or not t.name.startswith("blk."):
                print(f"  {t.name:32s} {TYPE_NAMES.get(t.ggml_type, t.ggml_type):5s} {t.dims}")
        sys.exit(0)
    print(__doc__)
