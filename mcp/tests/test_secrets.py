import importlib
import sys

import pytest


def test_non_windows_imports_do_not_load_dpapi(monkeypatch):
    import cmx_mcp.secrets as secrets

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        secrets.ctypes,
        "WinDLL",
        lambda *_: (_ for _ in ()).throw(AssertionError("DPAPI loaded during import")),
        raising=False,
    )
    imported = importlib.reload(secrets)
    assert imported._crypt32 is None
    assert imported._kernel32 is None
    assert importlib.import_module("cmx_mcp.server") is not None


def test_non_windows_dpapi_calls_fail_closed(monkeypatch, tmp_path):
    import cmx_mcp.secrets as secrets

    monkeypatch.setattr(sys, "platform", "linux")
    expected = "Windows DPAPI is not supported on this platform"
    with pytest.raises(RuntimeError, match=expected):
        secrets.protect_for_current_user("x" * 20)
    with pytest.raises(RuntimeError, match=expected):
        secrets.unprotect_for_current_user(b"encrypted")
    with pytest.raises(RuntimeError, match=expected):
        secrets.write_secret(tmp_path / "token.dpapi", "x" * 20)
    with pytest.raises(RuntimeError, match=expected):
        secrets.read_secret(tmp_path / "token.dpapi")
    assert not (tmp_path / "token.dpapi").exists()
