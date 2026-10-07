"""UPDATE NOW must run the setup .exe, never unzip it.

update_checker prefers the release's SC_Toolbox_Setup_<ver>.exe asset. The launcher
used to pass that file to apply_zip, which raised "File is not a zip file" every time.
"""

import os
import pathlib
import sys
import threading

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))

import pytest  # noqa: E402
from shared import auto_updater as AU  # noqa: E402


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r/releases/download/v2.3.1/SC_Toolbox_Setup_2.3.1.exe", True),
    ("https://github.com/o/r/releases/download/v2.3.1/SC_Toolbox_Setup_2.3.1.EXE?x=1", True),
    ("https://api.github.com/repos/o/r/zipball/v2.3.1", False),
    ("https://example.com/update.zip", False),
])
def test_is_installer(url, expected):
    assert AU.is_installer(url) is expected


def test_download_keeps_exe_suffix(tmp_path):
    src = tmp_path / "SC_Toolbox_Setup_9.9.9.exe"
    src.write_bytes(b"MZ" + b"\x00" * 1000)
    out = AU.download(pathlib.Path(src).as_uri(), lambda d, t: None, threading.Event(), suffix=".exe")
    try:
        assert out.endswith(".exe")
        assert open(out, "rb").read() == src.read_bytes()
    finally:
        os.unlink(out)


def test_apply_zip_rejects_an_installer(tmp_path):
    exe = tmp_path / "SC_Toolbox_Setup.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 1000)
    with pytest.raises(Exception, match="not a zip"):
        AU.apply_zip(str(exe), root=str(tmp_path / "root"))


def test_the_installer_is_not_started_inside_the_install_folder(monkeypatch, tmp_path):
    """The Toolbox's working folder is the install folder. An installer that inherits it cannot
    rename that folder, and every in-app update from 3.0.0 failed with "Failed to remove existing
    application directory". The installer must be started somewhere else."""
    import subprocess
    import tempfile
    seen = {}

    def fake_popen(args, **kw):
        seen["args"], seen["kw"] = args, kw

    install = tmp_path / "SC_Toolbox" / "current"
    install.mkdir(parents=True)
    monkeypatch.chdir(install)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    AU.run_installer(str(tmp_path / "setup.exe"))
    cwd = seen["kw"].get("cwd")
    assert cwd, "no working folder given: the installer would inherit the install folder"
    assert os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    assert not os.path.normcase(os.path.abspath(cwd)).startswith(os.path.normcase(str(install.parent)))

