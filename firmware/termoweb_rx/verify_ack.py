"""Reimplements the AVR C build_ack()/crc16() from firmware/termoweb_rx/main.c
in Python, byte for byte, and checks it against termoweb_frame.build_ack.
This is the "compute the AVR CRC logic in Python from your C" option from
the task (no AVR host build target exists in this repo's Makefile, so a
`make test` was not added; this script is the verification instead)."""
import sys
import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "custom_components", "termoweb_local", "vendor", "termoweb_local"))
import _vendored_frame as tf

KEYSTREAM = [0xFF, 0x87, 0xB8, 0x59, 0xB7, 0xA1, 0xCC, 0x24]
NETWORK_ID = [0x1B, 0x30]


def crc16_c(data):
    crc = 0x1D0F
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def build_ack_c(acker_id, src_id):
    logical = [0x05, NETWORK_ID[0], NETWORK_ID[1], acker_id, src_id, 0x80, 0, 0]
    crc = crc16_c(logical[:6])
    logical[6] = (crc >> 8) & 0xFF
    logical[7] = crc & 0xFF
    return bytes(b ^ KEYSTREAM[i] for i, b in enumerate(logical))


fails = 0

c_ack = build_ack_c(0x01, 0x04)
py_ack = tf.build_ack(0x01, 0x04)
print(f"C-model  build_ack(01,04) = {c_ack.hex(' ').upper()}")
print(f"termoweb_frame.build_ack(01,04) = {py_ack.hex(' ').upper()}")
if c_ack != py_ack:
    print("MISMATCH")
    fails += 1

worked = bytes.fromhex("FA9C885DB621FBD1")
c_ack2 = build_ack_c(0x04, 0x01)  # heater 04 acking gateway 01, per PROTOCOL.md 5.5
print(f"C-model  build_ack(04,01) = {c_ack2.hex(' ').upper()} (worked example: {worked.hex(' ').upper()})")
if c_ack2 != worked:
    print("MISMATCH vs worked example")
    fails += 1

if fails:
    print(f"{fails} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
