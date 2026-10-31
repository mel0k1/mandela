#!/usr/bin/env python3
"""Minimal ELF64 executable writer for the Mandela native backend (v0.6).

Produces a static ET_EXEC ELF binary with two PT_LOAD segments directly from
byte buffers — no system assembler, linker or libc is involved:

  * an R+X segment at 0x400000 holding the machine code plus read-only data
    (string literals and runtime constant pools),
  * an RW segment right above it holding mutable globals (zero-initialised,
    i.e. pure .bss: filesz 0, memsz n).

Entry point and segment addresses are fully determined here, so the encoder
fixups can be resolved against them.
"""

PAGE = 0x1000
BASE = 0x400000

EM_X86_64 = 62
EM_AARCH64 = 183

PT_LOAD = 1
PF_X, PF_W, PF_R = 1, 2, 4


def align_up(v, a):
    return (v + a - 1) // a * a


def build_elf(text_bytes, bss_size, entry_off, machine):
    """Assemble an ELF image.

    text_bytes: R+X contents (code + rodata), placed at BASE.
    bss_size:   bytes of zero-initialised RW memory (globals), page above.
    entry_off:  offset of the entry point inside text_bytes.
    machine:    EM_X86_64 or EM_AARCH64.
    """
    text_off = 64 + 2 * 56                            # ehdr + 2 phdrs
    text_vaddr = BASE + text_off
    rw_off = align_up(len(text_bytes) + text_off, PAGE)
    rw_vaddr = BASE + rw_off                          # BASE ≡ 0 (mod PAGE)
    entry = text_vaddr + entry_off

    # ---- ELF header ----
    e = bytearray(64)
    e[0:4] = b"\x7fELF"
    e[4] = 2            # ELFCLASS64
    e[5] = 1            # little-endian
    e[6] = 1            # EV_CURRENT
    e[7] = 0            # ELFOSABI_NONE
    e[16:18] = (2).to_bytes(2, "little")          # ET_EXEC
    e[18:20] = machine.to_bytes(2, "little")
    e[20:24] = (1).to_bytes(4, "little")          # e_version
    e[24:32] = entry.to_bytes(8, "little")
    e[32:40] = (64).to_bytes(8, "little")         # e_phoff
    e[40:48] = (0).to_bytes(8, "little")          # e_shoff
    e[48:52] = (0).to_bytes(4, "little")          # e_flags
    e[52:54] = (64).to_bytes(2, "little")         # e_ehsize
    e[54:56] = (56).to_bytes(2, "little")         # e_phentsize
    e[56:58] = (2).to_bytes(2, "little")          # e_phnum
    e[58:60] = (0).to_bytes(2, "little")          # e_shentsize
    e[60:62] = (0).to_bytes(2, "little")          # e_shnum
    e[62:64] = (0).to_bytes(2, "little")          # e_shstrndx

    def phdr(vaddr, off, filesz, memsz, flags):
        p = bytearray(56)
        p[0:4] = PT_LOAD.to_bytes(4, "little")
        p[4] = flags
        p[8:16] = off.to_bytes(8, "little")
        p[16:24] = vaddr.to_bytes(8, "little")
        p[24:32] = vaddr.to_bytes(8, "little")    # p_paddr
        p[32:40] = filesz.to_bytes(8, "little")
        p[40:48] = memsz.to_bytes(8, "little")
        p[48:56] = PAGE.to_bytes(8, "little")     # p_align
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
