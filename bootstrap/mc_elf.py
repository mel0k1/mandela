#!/usr/bin/env python3

PAGE = 0x1000
BASE = 0x400000

EM_X86_64 = 62
EM_AARCH64 = 183

PT_LOAD = 1
PF_X, PF_W, PF_R = 1, 2, 4

def align_up(v, a):
    return (v + a - 1) // a * a

def build_elf(text_bytes, bss_size, entry_off, machine):
    text_off = 64 + 2 * 56
    text_vaddr = BASE + text_off
    rw_off = align_up(len(text_bytes) + text_off, PAGE)
    rw_vaddr = BASE + rw_off
    entry = text_vaddr + entry_off

    e = bytearray(64)
    e[0:4] = b"\x7fELF"
    e[4] = 2
    e[5] = 1
    e[6] = 1
    e[7] = 0
    e[16:18] = (2).to_bytes(2, "little")
    e[18:20] = machine.to_bytes(2, "little")
    e[20:24] = (1).to_bytes(4, "little")
    e[24:32] = entry.to_bytes(8, "little")
    e[32:40] = (64).to_bytes(8, "little")
    e[40:48] = (0).to_bytes(8, "little")
    e[48:52] = (0).to_bytes(4, "little")
    e[52:54] = (64).to_bytes(2, "little")
    e[54:56] = (56).to_bytes(2, "little")
    e[56:58] = (2).to_bytes(2, "little")
    e[58:60] = (0).to_bytes(2, "little")
    e[60:62] = (0).to_bytes(2, "little")
    e[62:64] = (0).to_bytes(2, "little")

    def phdr(vaddr, off, filesz, memsz, flags):
        p = bytearray(56)
        p[0:4] = PT_LOAD.to_bytes(4, "little")
        p[4] = flags
        p[8:16] = off.to_bytes(8, "little")
        p[16:24] = vaddr.to_bytes(8, "little")
        p[24:32] = vaddr.to_bytes(8, "little")
        p[32:40] = filesz.to_bytes(8, "little")
        p[40:48] = memsz.to_bytes(8, "little")
        p[48:56] = PAGE.to_bytes(8, "little")
        return p

    p1 = phdr(BASE, 0, len(text_bytes) + text_off, len(text_bytes) + text_off,
              PF_R | PF_X)
    p2 = phdr(rw_vaddr, rw_off, 0, bss_size, PF_R | PF_W)

    out = bytearray()
    out += e + p1 + p2
    out += b"\x00" * (text_off - len(out))
    out += text_bytes
    out += b"\x00" * (rw_off - len(out))
    return bytes(out)
