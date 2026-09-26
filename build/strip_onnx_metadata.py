"""Strip PII-leaking metadata from every ONNX model under <root>.

PyTorch's default ``torch.onnx.export`` records the full python file path
of every layer in ``pkg.torch.onnx.stack_trace`` per-node — which embeds
the build-machine username into the shipped model. This script clears
the doc_string and metadata_props on the model, graph, and every node,
then re-saves preserving the original ``.onnx`` + ``.onnx.data``
external-data layout so on-disk size stays close to the original.

Usage:
    python strip_onnx_metadata.py <root_dir>
"""
from __future__ import annotations

import os
import re
import sys

import onnx


def strip(path: str) -> None:
    m = onnx.load(path)
    m.doc_string = ""
    del m.metadata_props[:]
    m.graph.doc_string = ""
    del m.graph.metadata_props[:]
    for n in m.graph.node:
        n.doc_string = ""
        del n.metadata_props[:]
    for io in list(m.graph.input) + list(m.graph.output):
        io.doc_string = ""
    for init in m.graph.initializer:
        init.doc_string = ""
    onnx.save(
        m, path,
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location=os.path.basename(path) + ".data",
        size_threshold=1024,
        convert_attribute=False,
    )


def main(root: str) -> int:
    if not os.path.isdir(root):
        print(f"[!] strip_onnx_metadata: root does not exist: {root}", file=sys.stderr)
        return 1
    # Backup folders are deleted by sanitize_staging.py (same pattern as its BACKUP_DIR_RE), so
    # stripping them is wasted work. A stale one (_bak_pretrain: an external-data .onnx whose .data
    # onnx.load cannot read) made every 2.4.0 build print "ONNX metadata strip failed - installer
    # may leak username", a false alarm: NTFS lists _bak_* AFTER letter/digit folders (it compares
    # uppercased, and '_' > 'Z'), so the live models had already been stripped (checked: 0 of 24
    # shipped .onnx carry torch metadata). A warning that always fires trains everyone to ignore it.
    # Now: skip backups, report EVERY failure (not just the first), fail only if a real one fails.
    backup = re.compile(r"(^_bak_|^models_bak_|^_bak$)", re.I)
    count, failed = 0, []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not backup.search(d)]
        for fn in files:
            if fn.endswith(".onnx"):
                p = os.path.join(dirpath, fn)
                try:
                    strip(p)
                    count += 1
                except Exception as e:
                    print(f"[!] strip failed for {p}: {e}", file=sys.stderr)
                    failed.append(p)
    if failed:
        print(f"[!] stripped {count}, FAILED {len(failed)} ONNX file(s) under {root}", file=sys.stderr)
        return 2
    print(f"[OK] stripped metadata from {count} ONNX file(s) under {root}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
