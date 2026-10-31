#!/usr/bin/env python3
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bootstrap"))
import mc_rt
import mc_x64
import mc_a64
import mc_isa
import mc_elf
import native


def build(arch, ops, ro, out):
    if arch == "x86-64":
        a = mc_x64.Asm()
        emit = mc_isa.emit_x64
        machine = mc_elf.EM_X86_64
    else:
        a = mc_a64.Asm()
        emit = mc_isa.emit_a64
        machine = mc_elf.EM_AARCH64
    for op in ops:
        emit(a, op)
    while len(a.buf) % 8:
        a.byte(0)
    for lbl in sorted(ro):
        a.labels[lbl] = len(a.buf)
        a.buf.extend(ro[lbl])
    text_base = mc_elf.BASE + 64 + 2 * 56
    rw_vaddr = mc_elf.BASE + ((len(a.buf) + 64 + 2 * 56 + 4095) // 4096) * 4096
    a.bss_abs = {"rt_data": rw_vaddr, "mnd_g": rw_vaddr + 80}
    a.text_base = text_base
    code = native.resolve_with_bss(a, arch)
    elf = mc_elf.build_elf(code, 80, a.labels["_start"], machine)
    with open(out, "wb") as fh:
        fh.write(elf)
    os.chmod(out, 0o755)


def run(arch, ops):
    rt_ops, rt_ro = mc_rt.gen_runtime()
    out = "/tmp/isa_test_bin"
    build(arch, rt_ops + [("label", "_start"), ("call", "rt_init")] + ops,
          rt_ro, out)
    r = subprocess.run([out], capture_output=True, timeout=10)
    return r.stdout.decode("utf-8", "replace")


B_NLO, B_NMID, B_NHI = 16, 24, 32


def save(*pairs):
    o = []
    for i, (reg, slot) in enumerate(pairs):
        o.append(("spaddr", "R8", slot))
        o.append(("store", reg, "R8", 0))
    return o


def prints(*slots):
    o = []
    for slot in slots:
        o.append(("spaddr", "R8", slot))
        o.append(("load", "A1", "R8", 0))
        o.append(("set", "A0", 1))
        o.append(("call", "rt_print_int"))
    return o


def fin():
    return [("set", "A0", 0), ("call", "rt_exit")]


def set_n(lo, mid=0, hi=0):
    o = [("addr", "R1", "rt_data")]
    o.append(("set64" if lo > 0x7FFFFFFF else "set", "R0", lo))
    o.append(("store", "R0", "R1", B_NLO))
    o.append(("set", "R0", mid))
    o.append(("store", "R0", "R1", B_NMID))
    o.append(("set", "R0", hi))
    o.append(("store", "R0", "R1", B_NHI))
    return o


def rtshow(off):
    return [("addr", "R1", "rt_data"), ("load", "A1", "R1", off),
            ("set", "A0", 1), ("call", "rt_print_int")]


CASES = []
CASES.append(("str_from_int", [
    ("set", "A1", 12345), ("set", "A0", 1), ("call", "rt_print_int")],
    "12345\n"))

CASES.append(("udivmod 100/7", [
    ("set", "R7", 100), ("set", "R2", 7),
    ("udivmod", "R0", "R1", "R7", "R2"),
    ("subsp", 32)] + save(("R0", 0), ("R1", 8)) + prints(0, 8) +
    [("addsp", 32)] + fin(), "14\n2\n"))

CASES.append(("mulu128 lo", [
    ("set64", "R5", 0x123456789), ("set", "R6", 10),
    ("mulu128", "R7", "R2", "R5", "R6"),
    ("subsp", 32)] + save(("R7", 0), ("R2", 8)) + prints(0, 8) +
    [("addsp", 32)] + fin(), "0\n48867183450\n"))

CASES.append(("mulu128 hi", [
    ("set64", "R5", 0xFFFFFFFFFFFFFFFF), ("set64", "R6", 0xFFFFFFFFFFFFFFFF),
    ("mulu128", "R7", "R2", "R5", "R6"),
    ("subsp", 32)] + save(("R7", 0), ("R2", 8)) + prints(0, 8) +
    [("addsp", 32)] + fin(), "-2\n1\n"))

CASES.append(("shlv", [
    ("set", "R3", 1), ("set", "R4", 40), ("shlv", "R2", "R3", "R4"),
    ("subsp", 32)] + save(("R2", 0)) + prints(0) + [("addsp", 32)] + fin(),
    "1099511627776\n"))

CASES.append(("shrv", [
    ("set64", "R3", 0x8000000000000000), ("set", "R4", 60),
    ("shrv", "R2", "R3", "R4"),
    ("subsp", 32)] + save(("R2", 0)) + prints(0) + [("addsp", 32)] + fin(),
    "8\n"))

CASES.append(("msb", [
    ("set64", "R3", 0x100), ("msb", "R2", "R3"),
    ("subsp", 32)] + save(("R2", 0)) + prints(0) + [("addsp", 32)] + fin(),
    "8\n"))

CASES.append(("rt_shl192 1<<4", set_n(1) + [
    ("set", "A0", 4), ("call", "rt_shl192")] + rtshow(B_NLO), "16\n"))

CASES.append(("rt_divmod192 37/10", set_n(37) + [
    ("set", "A0", 10), ("call", "rt_divmod192"),
    ("subsp", 32),
    ("addr", "R1", "rt_data"),
    ("load", "R5", "R1", B_NLO),
    ("store", "RET", "R1", 0), ("store", "R5", "R1", 8)] +
    [("spaddr", "R8", 0), ("load", "A1", "R8", 0), ("set", "A0", 1),
     ("call", "rt_print_int"),
     ("spaddr", "R8", 8), ("load", "A1", "R8", 0), ("set", "A0", 1),
     ("call", "rt_print_int"),
     ("addsp", 32)] + fin(), "7\n3\n"))


def main():
    arch = sys.argv[1] if len(sys.argv) > 1 else "x86-64"
    only = sys.argv[2] if len(sys.argv) > 2 else None
    bad = 0
    for name, ops, exp in CASES:
        if only and only not in name:
            continue
        got = run(arch, ops)
        ok = "OK " if got == exp else "BAD"
        if got != exp:
            bad += 1
        print("%s %-20s exp=%-24r got=%r" % (ok, name, exp, got))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
