#!/usr/bin/env python3
import struct
import sys

sys.path.insert(0, "bootstrap")
import mc_rt

M = (1 << 64) - 1


def extract(name_a, name_b):
    rt = mc_rt.gen_runtime()[0]
    i0 = next(i for i, op in enumerate(rt) if op == ("label", name_a))
    i1 = next(i for i, op in enumerate(rt) if op == ("label", name_b))
    return rt[i0:i1]


def run(ops, N=None, args=None, calls=None):
    mem = {}
    if N is not None:
        mem[16] = N & M
        mem[24] = (N >> 64) & M
        mem[32] = (N >> 128) & M
    R = dict(args or {})
    SP = {}
    F = dict(args.get("__F", {}))
    F = {}
    def val(x):
        return R.get(x, x) if isinstance(x, str) else x
    pc = 0
    while pc < len(ops):
        op = ops[pc]
        k = op[0]
        if k == "label":
            pass
        elif k == "jmp":
            pc = next(j for j, o in enumerate(ops) if o == ("label", op[1])); continue
        elif k == "mov":
            R[op[1]] = val(op[2])
        elif k in ("set", "set64"):
            R[op[1]] = op[2]
        elif k == "addr":
            R[op[1]] = 0
        elif k in ("subsp", "addsp"):
            pass
        elif k == "spaddr":
            R[op[1]] = 1000 + op[2]
        elif k == "load":
            b = val(op[2])
            if isinstance(b, str) and b == "SP":
                R[op[1]] = SP.get(op[3], 0)
            elif isinstance(b, int) and b >= 1000:
                R[op[1]] = SP.get(b - 1000 + op[3], 0)
            else:
                R[op[1]] = mem.get(b + op[3], 0)
        elif k == "load8":
            b = val(op[2])
            if isinstance(b, int) and b >= 1000:
                R[op[1]] = SP.get(b - 1000 + op[3], 0) & 0xFF
            else:
                R[op[1]] = mem.get(b + op[3], 0) & 0xFF
        elif k == "store":
            b = val(op[2])
            if isinstance(b, str) and b == "SP":
                SP[op[3]] = R[op[1]]
            elif isinstance(b, int) and b >= 1000:
                SP[b - 1000 + op[3]] = R[op[1]]
            else:
                mem[b + op[3]] = R[op[1]]
        elif k == "store8":
            b = val(op[2])
            v = R[op[1]] & 0xFF
            if isinstance(b, int) and b >= 1000:
                SP[b - 1000 + op[3]] = v
            else:
                mem[b + op[3]] = v
        elif k == "shl":
            R[op[1]] = (val(op[2]) << op[3]) & M
        elif k == "shr":
            R[op[1]] = (val(op[2]) >> op[3]) & M
        elif k == "shlv":
            R[op[1]] = (val(op[2]) << val(op[3])) & M
        elif k == "shrv":
            R[op[1]] = (val(op[2]) >> val(op[3])) & M
        elif k == "or":
            R[op[1]] = (val(op[2]) | val(op[3])) & M
        elif k == "and":
            R[op[1]] = (val(op[2]) & val(op[3])) & M
        elif k == "add":
            R[op[1]] = (val(op[2]) + val(op[3])) & M
        elif k == "sub":
            R[op[1]] = (val(op[2]) - val(op[3])) & M
        elif k == "mul":
            R[op[1]] = (val(op[2]) * val(op[3])) & M
        elif k == "neg":
            R[op[1]] = (-val(op[2])) & M
        elif k == "udivmod":
            num, den = val(op[3]), val(op[4])
            R[op[1]] = num // den
            R[op[2]] = num % den
        elif k == "msb":
            v = val(op[2])
            R[op[1]] = v.bit_length() - 1 if v else 0
        elif k == "clz":
            v = val(op[2])
            R[op[1]] = 64 - v.bit_length() if v else 64
        elif k == "cji":
            a, b = val(op[1]), op[2]
            t = {"ne": a != b, "e": a == b, "l": a < b, "le": a <= b,
                 "g": a > b, "ge": a >= b}[op[3]]
            if t:
                pc = next(j for j, o in enumerate(ops) if o == ("label", op[4])); continue
        elif k == "cj":
            a, b = val(op[1]), val(op[2])
            t = {"ne": a != b, "e": a == b, "l": a < b, "le": a <= b, "g": a > b,
                 "ge": a >= b, "b": a < b, "be": a <= b, "a": a > b,
                 "ae": a >= b}[op[3]]
            if t:
                pc = next(j for j, o in enumerate(ops) if o == ("label", op[4])); continue
        elif k == "fgpr":
            R[op[1]] = F.get(op[2], 0)
        elif k == "gprf":
            F[op[1]] = R.get(op[2], 0) & M
        elif k == "fjisnan":
            pass
        elif k == "call":
            if calls and op[1] in calls:
                calls[op[1]](R, mem)
            else:
                raise Exception("sim: unhandled call %s" % op[1])
        elif k == "ret":
            return R.get("RET", 0), R, mem
        else:
            raise Exception("sim: unhandled op %r" % (op,))
        pc += 1
    raise Exception("sim: fell off end")


def shl192(R, mem):
    v = mem.get(16, 0) | (mem.get(24, 0) << 64) | (mem.get(32, 0) << 128)
    v = (v << R["A0"])
    mem[16], mem[24], mem[32] = v & M, (v >> 64) & M, (v >> 128) & M


CALLS = {"rt_shl192": shl192}


def sim_u192_to_f64(N, rshift, sticky):
    ops = extract("rt_u192_to_f64", "rt_f64_from_decimal")
    ret, R, mem = run(ops, N=N, args={"A0": rshift, "A1": sticky}, calls=CALLS)
    return ret


if __name__ == "__main__":
    import math
    for N, rs in [(1, 0), (0x8000000000000000, 63), (0x14000000000000, 1073),
                  (123, 0), (10, 1)]:
        got = sim_u192_to_f64(N, rs, 0)
        exp = struct.unpack("<q", struct.pack("<d", math.ldexp(float(N), -rs)))[0]
        exp &= (1 << 64) - 1
        print("N=%x rs=%d sim=%s exp=%s %s"
              % (N, rs, got, exp, "OK" if got == exp else "BAD"))
