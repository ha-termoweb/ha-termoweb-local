#!/usr/bin/env python3
"""Termoweb gateway-to-heater frame codec: one PN9 keystream, one CRC, no per-class tables.

Ground truth is docs/captures/2026-09-05-gateway-dump/scrambler-crc-verification.md and its
verify_scrambler.py: every on-air frame is `logical XOR keystream` from byte 0, where the
keystream is the standard PN9 data whitener, and every frame carries one CRC-16/CCITT (poly 0x1021,
init 0x1D0F, final XOR 0xFFFF) over its logical bytes from 0 to len-3. Logical byte 0 is the
length of the bytes that follow, excluding the 2 CRC bytes, so `air[0] ^ 0xFF` (the keystream's
first byte is always 0xFF) recovers that length straight from the wire without knowing anything
else about the frame, which is what lets nano_rx.py drop its old per-first-byte CLASS_TABLE.

The whitener is not bespoke and provides no obfuscation: it is the same PN9 generator the TI
CC1101 family uses for PKTCTRL0.WHITE_DATA, documented in TI Design Note DN509 (SWRA322).
One reproduction detail matters. DN509 tabulates the eight LSBs of every eighth register state,
giving FF E1 1D 9A ..., while this link assembles each keystream byte MSB-first from the
generator's serial output, giving FF 87 B8 59 ..., the bit-reverse of DN509's table byte for
byte. So a stock CC1101 with WHITE_DATA set would not produce these on-air bytes, and rtl_433
and Universal Radio Hacker, which de-whiten in DN509's byte order, do not work here unmodified.
"""
import dataclasses

NETWORK_ID = bytes.fromhex("1B30")
CRC_INIT = 0x1D0F
CRC_POLY = 0x1021
HEADER_LEN = 12  # logical bytes 0..11 before the payload


def keystream(n):
    """First n bytes of the link's PN9 whitening keystream (x^9 + x^5 + 1, seed all ones,
    one byte per eight shifts, bytes assembled MSB-first).

    This is an 8-bit-register formulation of that 9-bit generator, kept because it is the
    form the corpus was originally verified against and it emits the identical sequence.
    tests/test_frame.py::test_keystream_is_pn9_msb_first pins the equivalence against a
    from-scratch PN9 generator over the full 511-byte period, in both bit orders, so the
    two formulations cannot drift apart unnoticed.
    """
    reg, fb, out = 0xFF, 1, []
    for _ in range(n):
        out.append(reg)
        for _ in range(8):
            fb_old = fb
            fb = ((reg ^ ((reg << 5) & 0xFF)) >> 7) & 1
            reg = ((reg << 1) | fb_old) & 0xFF
    return out


def descramble(data):
    """XOR data with the keystream from byte 0. Scrambling and descrambling are the same
    operation, so this also builds on-air bytes from logical ones."""
    return bytes(b ^ k for b, k in zip(data, keystream(len(data))))


scramble = descramble


def crc16(data, init=CRC_INIT, poly=CRC_POLY):
    """CRC-16/CCITT, MSB first, given init, final XOR 0xFFFF, over data as given (caller
    picks the logical byte range: 0 to len-3)."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def build_frame(src, dst, payload, flags=0x00, hops=None, tag=0x00,
                network_id=NETWORK_ID, path=None):
    """Build the on-air bytes for a data frame: length byte, network id, src (this hop's
    sender), dst (the next hop), flags, the five-byte routing path, the byte-11 tag, the
    payload, then the CRC, all scrambled.

    `path` is logical bytes 6-10, the end-to-end mesh routing path: byte 6 the originator,
    bytes 7-10 the successive hops towards the final destination, zero-padded. That is what
    the gateway firmware's own path builder writes (`FUN_400ffa7c`: `0x400ffac9` stores this
    station's id at buffer index 0, `0x400ffad4` lays the walked next-hop chain down forwards
    behind it, `0x400ffaf8` zero-pads the rest), and `FUN_400ff728` `0x400ff780` then reads
    buffer index 1, logical byte 7, back out as the link-layer destination in byte 4. See
    docs/captures/2026-09-05-gateway-dump/analysis8.md sections 2 and 7.

    It defaults to `(src, dst) + hops`, this station originating a frame whose next hop is
    also its destination, which is what every frame this project sends and all but three
    frames in the whole capture corpus carry, and what `FUN_400ffa7c` itself produces on a
    one-hop path. Pass it explicitly to build a relayed frame, where src and dst are this
    hop's link addresses and the path is not derivable from them, as in the heater-forwarded
    F1 and the two F4 route advertisements of analysis8.md section 4.

    `hops` remains accepted as the older name for logical bytes 8-10 alone (00 00 00 from the
    gateway, 01 01 01 from a heater) and is folded into the default path; passing both is an
    error rather than a silent precedence rule.

    The tag is the gateway routing layer's own per-call-site constant (`FUN_400ff728`'s
    param_2, `0x400ff752`): 00 on every F1/F2/F3/E5 command frame, 04 on the pairing id
    assignment, 06 on a heater's F4 route probe. The one F4 addressed to this station carries
    84 rather than that 06, so the high bit is carried by something the firmware reading does
    not account for; the corpus is round-tripped on the observed byte.

    network_id defaults to this link's own NETWORK_ID, which every joined node uses. It
    is a parameter only because a heater that has not joined yet announces itself with a
    zero network id (the E7 at 2026-09-06 17:12:17.345), so a codec that cannot express
    that cannot rebuild the whole corpus."""
    if path is None:
        path = (src, dst) + tuple(hops if hops is not None else (0x00, 0x00, 0x00))
    elif hops is not None:
        raise ValueError("pass either path (bytes 6-10) or hops (bytes 8-10), not both")
    path = bytes(path)
    if len(path) != 5:
        raise ValueError("path must be logical bytes 6-10, five bytes")
    rest = (bytes(network_id) + bytes([src, dst, flags]) + path +
            bytes([tag]) + bytes(payload))
    logical = bytes([len(rest)]) + rest
    crc = crc16(logical)
    logical += bytes([crc >> 8, crc & 0xFF])
    return scramble(logical)


@dataclasses.dataclass
class Frame:
    length_ok: bool
    crc_ok: bool
    src: int
    dst: int
    flags: int
    payload: bytes
    logical: bytes


def parse_frame(air):
    """Descramble on-air bytes and report length/CRC validity plus the logical fields.
    Safe on any length, including a piece too short to hold a full header: src/dst/flags/
    payload just come back as whatever the (possibly out-of-range) slice yields."""
    logical = descramble(air)
    if not logical:
        return Frame(False, False, None, None, None, b"", logical)
    length = logical[0]
    length_ok = length == len(air) - 3
    body_end = length + 1  # index just past the last body byte, before the CRC trailer
    if len(logical) >= body_end + 2:
        crc_ok = crc16(logical[:body_end]) == int.from_bytes(logical[body_end:body_end + 2], "big")
    else:
        crc_ok = False
    src = logical[3] if len(logical) > 3 else None
    dst = logical[4] if len(logical) > 4 else None
    flags = logical[5] if len(logical) > 5 else None
    payload = logical[HEADER_LEN:body_end] if len(logical) >= HEADER_LEN else b""
    return Frame(length_ok, crc_ok, src, dst, flags, payload, logical)


def frame_length(air_first_byte):
    """Total on-air frame length (header + payload + 2 CRC bytes) implied by one air byte,
    with no descrambling needed: the keystream's own byte 0 is always 0xFF."""
    return (air_first_byte ^ 0xFF) + 3


def split_frames(air):
    """Split a raw multi-frame read (e.g. the firmware's fixed 64-byte buffer) into
    on-air frame chunks using each chunk's own length byte, no class table. Returns
    (chunks, leftover): leftover is whatever remained too short to hold a next declared
    length. Does not itself resync on a bad length; a caller that finds a chunk's CRC bad
    still needs a bit-level sync search (as nano_rx.py does) to find the next real frame."""
    chunks = []
    rest = bytes(air)
    while rest:
        total = frame_length(rest[0])
        if total < 3 or total > len(rest):
            break
        chunks.append(rest[:total])
        rest = rest[total:]
    return chunks, rest


def build_ack(src, dst):
    """Build the on-air bytes for an 8-byte ack: length 0x05, network id, the
    acker's own id, the acked frame's original sender id, flags 0x80, then the
    CRC directly at bytes 6-7 (no bytes 8-11, no payload; PROTOCOL.md 5.5)."""
    logical = bytes([0x05]) + NETWORK_ID + bytes([src, dst, 0x80])
    crc = crc16(logical)
    logical += bytes([crc >> 8, crc & 0xFF])
    return scramble(logical)


def setpoint_payload(celsius):
    return bytes([0xB4, 0x02, int(round(celsius * 2))])


def mode_payload(mode):
    return bytes([0xB4, {"heat": 0x02, "off": 0x04}[mode]])


def poll_payload():
    return bytes([0x57, 0x55])


if __name__ == "__main__":
    import importlib.util
    import os
    import sys

    verify_path = os.path.join(os.path.dirname(__file__), "..", "docs", "captures",
                                "2026-09-05-gateway-dump", "verify_scrambler.py")
    spec = importlib.util.spec_from_file_location("verify_scrambler", verify_path)
    verify_scrambler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify_scrambler)

    failures = 0
    for name, hx in verify_scrambler.WORKED:
        air = bytes.fromhex(hx)
        f = parse_frame(air)
        if not (f.length_ok and f.crc_ok):
            print(f"PARSE FAIL {name}: length_ok={f.length_ok} crc_ok={f.crc_ok}")
            failures += 1
            continue
        if f.logical[0] == 0x05:
            rebuilt = build_ack(f.src, f.dst)
        else:
            rebuilt = build_frame(f.src, f.dst, f.payload, f.flags,
                                   tuple(f.logical[8:11]), f.logical[11])
        if rebuilt != air:
            print(f"ROUND TRIP FAIL {name}")
            print(f"    want {air.hex(' ').upper()}")
            print(f"    got  {rebuilt.hex(' ').upper()}")
            failures += 1

    setpoint_air = build_frame(0x01, 0x04, setpoint_payload(25.5))
    expected_setpoint = "F19C8858B3A1CD20575E4B9CBAEBD9F6E5"
    if setpoint_air.hex().upper() != expected_setpoint:
        print(f"SETPOINT HELPER FAIL: got {setpoint_air.hex(' ').upper()}")
        failures += 1

    if failures:
        print(f"{failures} check(s) failed")
        sys.exit(1)
    print(f"{len(verify_scrambler.WORKED)} worked frames + helper round trips: all OK")
