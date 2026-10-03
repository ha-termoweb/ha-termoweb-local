"""Codec surface: round-trip every worked frame from verify_scrambler.py through
termoweb_local.frame, plus the two explicit checks docs/90-phase3-plan.md P2's
verification line asks for (a built command frame matching a worked example, and a
captured E5 frame decoding to the field values PROTOCOL.md section 5.4 already
states)."""
import pytest

from termoweb_local import frame as tf

# Worked on-air frames, each CRC-valid and rebuilt byte for byte.
WORKED = [
    ("F1 setpoint 25.5 gateway to node 4", "F19C8858B3A1CD20575E4B9CBAEBD9F6E5"),
    ("F2 mode off", "F29C8858B3A1CD20575E4B9CBAEDF895"),
    ("F2 mode heat", "F29C8858B3A1CD20575E4B9CBAEB9853"),
    ("F2 poll", "F29C8858B3A1CD20575E4B9C59BCF7A0"),
    ("F2 heater to gateway BB BC", "F29C885DB6A1C825565F4A9CBBBC6252"),
    ("E5 report master 25.5", "E59C885DB6A1C825565F4A9C5850E47E05BCB4E185AE81C3E7BFE380F1"),
    ("E5 report master 24.0", "E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28C3E7BEE3933D"),
    ("E5 report bedroom 21.0 off", "E59C885AB6A1CF25565F4A9C5850E47A05BAB4C19CAEE7F1E686E38829"),
    ("E5 report living 22.0", "E59C885BB6A1CE25565F4A9C5850E47E05BCB4C19AF92AE5E787E3CAD2"),
    ("FA ack heater to gateway", "FA9C885DB621FBD1"),
    ("F1 heater to gateway B0 F7", "F19C885DB6A1C825565F4A9CB0F73694B6"),
]


@pytest.mark.parametrize("name,hexstr", WORKED)
def test_worked_frame_round_trips(name, hexstr):
    air = bytes.fromhex(hexstr)
    parsed = tf.parse_frame(air)
    assert parsed.length_ok, name
    assert parsed.crc_ok, name

    if parsed.logical[0] == 0x05:
        rebuilt = tf.build_ack(parsed.src, parsed.dst)
    else:
        rebuilt = tf.build_frame(
            parsed.src, parsed.dst, parsed.payload, parsed.flags, tuple(parsed.logical[8:11])
        )
    assert rebuilt == air, name


def test_setpoint_command_matches_worked_example():
    """docs/PROTOCOL.md section 5.1: gateway (01) to master bedroom heater (04), 25.5C."""
    expected_air = "F19C8858B3A1CD20575E4B9CBAEBD9F6E5"
    air = tf.build_frame(0x01, 0x04, tf.setpoint_payload(25.5))
    assert air.hex().upper() == expected_air


def test_mode_heat_and_off_match_worked_examples():
    """docs/PROTOCOL.md section 5.2 worked frames, gateway (01) to master bedroom (04)."""
    heat_air = tf.build_frame(0x01, 0x04, tf.mode_payload("heat"))
    assert heat_air.hex().upper() == "F29C8858B3A1CD20575E4B9CBAEB9853"

    off_air = tf.build_frame(0x01, 0x04, tf.mode_payload("off"))
    assert off_air.hex().upper() == "F29C8858B3A1CD20575E4B9CBAEDF895"


def test_decode_captured_e5_matches_protocol_md_section_5_4():
    """PROTOCOL.md section 5.4's worked example: master bedroom heater (04) reporting
    25.5C to the gateway (01) while heating (byte 17 = 02, byte 20 = 0x33)."""
    air = bytes.fromhex(
        "E59C885DB6A1C825565F4A9C5850E47E05BCB4E185AE81C3E7BFE380F1"
    )
    parsed = tf.parse_frame(air)
    assert parsed.crc_ok
    assert parsed.logical[17] == 0x02  # heating output active
    assert parsed.logical[20] == 0x33  # 25.5C in half degrees


def _pn9_from_scratch(count, msb_first):
    """PN9 per TI DN509 (SWRA322): 9-bit LFSR, x^9 + x^5 + 1, seeded all ones, one output
    byte per eight shifts. msb_first packs the first bit shifted out into bit 7 of the byte,
    which is what this link does; the LSB-first packing is DN509's own tabulated order."""
    register = 0x1FF
    out = bytearray()
    for _ in range(count):
        byte = 0
        for index in range(8):
            bit = register & 1
            register = (register >> 1) | (((bit ^ ((register >> 5) & 1)) & 1) << 8)
            byte |= bit << (7 - index) if msb_first else bit << index
        out.append(byte)
    return bytes(out)


def test_keystream_is_pn9_msb_first():
    """The codec's 8-bit-register keystream is the PN9 whitener with bytes assembled
    MSB-first. Pinned over the generator's full 511-byte period so the two formulations
    cannot drift, and pinned against the LSB-first order too so the bit-order distinction
    that makes this link differ from a stock CC1101 cannot regress silently."""
    period = 511
    ours = bytes(tf.keystream(period))

    assert ours == _pn9_from_scratch(period, msb_first=True)
    assert ours != _pn9_from_scratch(period, msb_first=False)

    # DN509 tabulates the LSB-first order; this link's bytes are its bit-reverse.
    dn509 = _pn9_from_scratch(period, msb_first=False)
    assert dn509[:8].hex() == "ffe11d9aed853324"
    assert ours[:8].hex() == "ff87b859b7a1cc24"
    assert bytes(int(f"{b:08b}"[::-1], 2) for b in dn509) == ours


# --- build_frame's `path` argument, logical bytes 6-10 ---
#
# docs/captures/2026-09-05-gateway-dump/analysis8.md section 7: bytes 6-10 are the
# end-to-end mesh routing path (byte 6 the originator, bytes 7-10 the hops towards the
# final destination), while bytes 3-4 are this hop's sender and next hop. The builder
# used to write bytes 6-7 as the frame's own src and dst, which made a relayed frame
# inexpressible. `path` takes the five bytes whole; `hops` stays as the older name for
# bytes 8-10 alone.

# The three relayed frames of analysis8.md section 4, the only frames in the whole
# capture corpus whose path is not the frame's own (src, dst) one-hop path. Named
# regression cases: each is a real capture, on air, from
# docs/captures/2026-09-06-proof/.
RELAY_FRAMES = [
    (
        "heater 04 forwarding our own F1 one hop onward",
        "F19C885DB6A1CD20565F4A9CBAEBD926DA",
        dict(src=0x04, dst=0x01, payload=bytes.fromhex("B40233"),
             path=(0x01, 0x04, 0x01, 0x01, 0x01), tag=0x00),
    ),
    (
        "heater 02 relaying 04's F4 route probe",
        "F49C885BB6A1C826565F4A9AB57C",
        dict(src=0x02, dst=0x01, payload=b"",
             path=(0x04, 0x02, 0x01, 0x01, 0x01), tag=0x06),
    ),
    (
        "heater 03 relaying 04's F4 route probe",
        "F49C885AB6A1C827565F4A9AF40E",
        dict(src=0x03, dst=0x01, payload=b"",
             path=(0x04, 0x03, 0x01, 0x01, 0x01), tag=0x06),
    ),
]


@pytest.mark.parametrize("name,hexstr,kwargs", RELAY_FRAMES)
def test_relay_frame_rebuilds_byte_exactly_from_its_own_path(name, hexstr, kwargs):
    air = bytes.fromhex(hexstr)
    parsed = tf.parse_frame(air)
    assert parsed.crc_ok, name

    # The frames really are relayed: the path's originator is not this hop's sender.
    assert parsed.logical[6] != parsed.src, name
    assert tuple(parsed.logical[6:11]) == kwargs["path"], name

    assert tf.build_frame(**kwargs) == air, name


@pytest.mark.parametrize("name,hexstr,kwargs", RELAY_FRAMES)
def test_relay_frame_is_inexpressible_without_path(name, hexstr, kwargs):
    """The old `hops`-only surface cannot reach these frames at all: with bytes 6-7
    forced to src and dst there is no argument that produces the captured bytes. Pinned
    so a revert to the old derivation fails loudly rather than silently losing three
    frames from the corpus rebuild again."""
    without_path = dict(kwargs)
    without_path.pop("path")
    rebuilt = tf.build_frame(hops=kwargs["path"][2:], **without_path)
    assert rebuilt != bytes.fromhex(hexstr), name


def test_path_defaults_to_the_one_hop_path_and_folds_in_hops():
    """The default must leave every existing caller byte-identical: no path at all is
    (src, dst, 0, 0, 0), and `hops` still names bytes 8-10 alone."""
    assert tf.build_frame(0x01, 0x04, tf.setpoint_payload(25.5)) == tf.build_frame(
        0x01, 0x04, tf.setpoint_payload(25.5), path=(0x01, 0x04, 0x00, 0x00, 0x00)
    )
    assert tf.build_frame(
        0x04, 0x01, bytes([0xB5, 0x55]), hops=(1, 1, 1)
    ) == tf.build_frame(
        0x04, 0x01, bytes([0xB5, 0x55]), path=(0x04, 0x01, 0x01, 0x01, 0x01)
    )

    logical = tf.descramble(tf.build_frame(0x01, 0x04, tf.setpoint_payload(25.5)))
    assert tuple(logical[6:11]) == (0x01, 0x04, 0x00, 0x00, 0x00)


def test_path_accepts_bytes_and_bytearray_as_well_as_a_tuple():
    """corpus_check.py round-trips a bytes slice straight off the captured frame."""
    air = tf.build_frame(0x02, 0x01, b"", path=(0x04, 0x02, 0x01, 0x01, 0x01), tag=0x06)
    assert tf.build_frame(0x02, 0x01, b"", path=bytes([4, 2, 1, 1, 1]), tag=0x06) == air
    assert tf.build_frame(0x02, 0x01, b"", path=bytearray([4, 2, 1, 1, 1]), tag=0x06) == air


def test_path_and_hops_together_are_rejected():
    with pytest.raises(ValueError, match="not both"):
        tf.build_frame(0x01, 0x04, b"\xb4", path=(1, 4, 0, 0, 0), hops=(0, 0, 0))


@pytest.mark.parametrize("bad", [(), (1,), (1, 4, 0, 0), (1, 4, 0, 0, 0, 0), b"\x01\x04"])
def test_path_must_be_exactly_five_bytes(bad):
    with pytest.raises(ValueError, match="five bytes"):
        tf.build_frame(0x01, 0x04, b"\xb4", path=bad)


@pytest.mark.parametrize("bad_hops", [(), (1, 1), (1, 1, 1, 1)])
def test_hops_of_the_wrong_length_is_rejected_through_the_path_check(bad_hops):
    """`hops` had no length check before, so a wrong-length tuple silently shifted every
    byte after it. Folding it into `path` gives it one for free."""
    with pytest.raises(ValueError, match="five bytes"):
        tf.build_frame(0x01, 0x04, b"\xb4", hops=bad_hops)
