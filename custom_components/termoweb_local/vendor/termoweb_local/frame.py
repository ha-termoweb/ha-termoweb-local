"""Codec re-export: termoweb_local.frame *is* the tools/termoweb_frame module.

tools/termoweb_frame.py is the ground-truth codec (keystream, CRC, frame builders)
proven against the whole capture corpus
(docs/captures/2026-09-06-proof/corpus-rebuild.md). This package must not fork that
logic, so this module never redefines a single byte of it: it loads the
tools/termoweb_frame.py file by path when this package sits inside the full
project checkout (development, and every test in this suite), and falls back to
`_vendored_frame.py`, a copy kept byte-identical to that file, when the
package is installed on its own (e.g. as a Home Assistant custom component
dependency, with no `tools/termoweb_frame.py` sitting next to it).

tests/test_frame_vendor.py compares the two files byte for byte so the vendored
copy cannot silently drift from the reference; re-run `cp tools/termoweb_frame.py
termoweb_local/_vendored_frame.py` from the repo root whenever the reference
changes.
"""
import importlib.util
import pathlib

_REPO_ROOT_FRAME = pathlib.Path(__file__).resolve().parent.parent / "tools" / "termoweb_frame.py"
_VENDORED_FRAME = pathlib.Path(__file__).resolve().parent / "_vendored_frame.py"


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE_PATH = _REPO_ROOT_FRAME if _REPO_ROOT_FRAME.exists() else _VENDORED_FRAME
_impl = _load_module(SOURCE_PATH, "termoweb_local._frame_impl")

# Public codec surface, unchanged from termoweb_frame.py; see that module's own
# docstrings for what each name does.
NETWORK_ID = _impl.NETWORK_ID
CRC_INIT = _impl.CRC_INIT
CRC_POLY = _impl.CRC_POLY
HEADER_LEN = _impl.HEADER_LEN
Frame = _impl.Frame
keystream = _impl.keystream
descramble = _impl.descramble
scramble = _impl.scramble
crc16 = _impl.crc16
build_frame = _impl.build_frame
build_ack = _impl.build_ack
parse_frame = _impl.parse_frame
frame_length = _impl.frame_length
split_frames = _impl.split_frames
setpoint_payload = _impl.setpoint_payload
mode_payload = _impl.mode_payload
poll_payload = _impl.poll_payload
