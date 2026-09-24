import pathlib
import sys
import types

# Make `app` importable when pytest is run from anywhere (e.g. `pytest
# dashboard/tests` from the repo root, not just from inside `dashboard/`).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# app/control.py does `import pwd` (POSIX-only - it's for reading the
# zoombot UID on the real VPS). The tests here never actually call
# zoombot_uid(), so a minimal stand-in is enough to let the module import
# cleanly on a non-POSIX dev machine (e.g. Windows) too. This never runs
# on the real deployment target, where the real stdlib `pwd` is used.
if "pwd" not in sys.modules:
    try:
        import pwd  # noqa: F401
    except ImportError:
        fake_pwd = types.ModuleType("pwd")

        class _FakePasswdEntry:
            pw_uid = 1000

        fake_pwd.getpwnam = lambda name: _FakePasswdEntry()
        sys.modules["pwd"] = fake_pwd

if "grp" not in sys.modules:
    try:
        import grp  # noqa: F401
    except ImportError:
        fake_grp = types.ModuleType("grp")

        class _FakeGroupEntry:
            gr_gid = 1000

        fake_grp.getgrnam = lambda name: _FakeGroupEntry()
        sys.modules["grp"] = fake_grp

if "pyDes" not in sys.modules:
    try:
        import pyDes  # noqa: F401
    except ImportError:
        fake_pydes = types.ModuleType("pyDes")
        fake_pydes.des = lambda key, *a, **k: types.SimpleNamespace(encrypt=lambda d: d, decrypt=lambda d: d)
        fake_pydes.ECB = 0
        fake_pydes.PAD_NORMAL = 0
        sys.modules["pyDes"] = fake_pydes
