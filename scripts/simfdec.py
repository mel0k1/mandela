#!/usr/bin/env python3
import struct
import sys

sys.path.insert(0, "bootstrap")
import mc_rt
from sim192 import extract, run, M


def shl192(R, mem):
    v = mem.get(16, 0) | (mem.get(24, 0) << 64) | (mem.get(32, 0) << 128)
    v = (v << R["A0"])
    mem[16], mem[24], mem[32] = v & M, (v >> 64) & M, (v >> 128) & M


def shr192s(R, mem):
    k = R["A0"]
    v = mem.get(16, 0) | (mem.get(24, 0) << 64) | (mem.get(32, 0) << 128)
    dropped = v & ((1 << k) - 1)
    v >>= k
    R["RET"] = 1 if dropped else 0
    mem[16], mem[24], mem[32] = v & M, (v >> 64) & M, (v >> 128) & M


def mul192(R, mem):
    m = R["A0"]
    v = mem.get(16, 0) | (mem.get(24, 0) << 64) | (mem.get(32, 0) << 128)
    v = (v * m)
    mem[16], mem[24], mem[32] = v & M, (v >> 64) & M, (v >> 128) & M


def divmod192(R, mem):
    d = R["A0"]
    v = mem.get(16, 0) | (mem.get(24, 0) << 64) | (mem.get(32, 0) << 128)
    R["RET"] = v % d
    v //= d
    mem[16], mem[24], mem[32] = v & M, (v >> 64) & M, (v >> 128) & M


def u192_to_f64(R, mem):
    v = mem.get(16, 0) | (mem.get(24, 0) << 64) | (mem.get(32, 0) << 128)
    val = v / 2.0 ** R["A0"] + (R["A1"] * 2.0 ** -1100)
    R["RET"] = struct.unpack("<Q", struct.pack("<d", val))[0]


CALLS = {"rt_shl192": shl192, "rt_shr192s": shr192s, "rt_mul192": mul192,
         "rt_divmod192": divmod192, "rt_u192_to_f64": u192_to_f64}


def sim_fdec(D, q):
    ops = extract("rt_f64_from_decimal", "rt_str_from_f64")
    qq = q & 0xFFFFFFFFFFFFFFFF if q < 0 else q
    ret, R, mem = run(ops, args={"A0": D, "A1": qq}, calls=CALLS)
    return ret


if __name__ == "__main__":
    for D, q, name in [(1, 0, "1.0"), (25, -1, "2.5"), (5, -1, "0.5"),
                       (314159, -5, "3.14159"), (1, -3, "0.001"),
                       (1000000, 4, "1.0e10"), (123456789, -2, "1234567.89")]:
        got = sim_fdec(D, q)
        exp = struct.unpack("<Q", struct.pack("<d", float(name)))[0]
        print("OK " if got == exp else "BAD",
              "fdec(%d,%d) sim=%s exp=%s (%s)" % (D, q, got, exp, name))
