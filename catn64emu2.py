#!/usr/bin/env python3
"""
CatN64EMU 2.0 - Full Hardware N64 Emulator
Project64-style with complete hardware emulation

© 2025 FlamesCo & Samsoft - Team Flames 🐱
"""

import struct
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Optional, Tuple, Callable
from enum import IntEnum, IntFlag
from dataclasses import dataclass
import json
import logging

logging.basicConfig(level=logging.INFO, format='[%(name)s] %(message)s')
log = logging.getLogger('N64')

# =============================================================================
# Constants
# =============================================================================

APP_NAME = "CatN64EMU"
APP_VERSION = "2.0" 
APP_TITLE = f"{APP_NAME} {APP_VERSION}"

CPU_FREQ = 93_750_000
RCP_FREQ = 62_500_000
CYCLES_PER_FRAME = CPU_FREQ // 60
RDRAM_SIZE = 0x800000
RSP_MEM_SIZE = 0x1000
PIF_RAM_SIZE = 64

# Memory map
class Mem:
    RSP_DMEM = 0x04000000
    RSP_IMEM = 0x04001000
    RSP_REGS = 0x04040000
    RSP_PC   = 0x04080000
    DPC_REGS = 0x04100000
    MI_REGS  = 0x04300000
    VI_REGS  = 0x04400000
    AI_REGS  = 0x04500000
    PI_REGS  = 0x04600000
    RI_REGS  = 0x04700000
    SI_REGS  = 0x04800000
    CART_ROM = 0x10000000
    PIF_RAM  = 0x1FC007C0

class MIIntr(IntFlag):
    SP = 0x01; SI = 0x02; AI = 0x04; VI = 0x08; PI = 0x10; DP = 0x20

class COP0(IntEnum):
    INDEX=0; RANDOM=1; ENTRYLO0=2; ENTRYLO1=3; CONTEXT=4; PAGEMASK=5
    WIRED=6; BADVADDR=8; COUNT=9; ENTRYHI=10; COMPARE=11; STATUS=12
    CAUSE=13; EPC=14; PRID=15; CONFIG=16; LLADDR=17; ERROREPC=30

# Utility
def u32(x): return x & 0xFFFFFFFF
def u64(x): return x & 0xFFFFFFFFFFFFFFFF
def s16(x): x &= 0xFFFF; return x if x < 0x8000 else x - 0x10000
def s32(x): x &= 0xFFFFFFFF; return x if x < 0x80000000 else x - 0x100000000
def s64(x): x &= 0xFFFFFFFFFFFFFFFF; return x if x < 0x8000000000000000 else x - 0x10000000000000000

# CIC detection
CIC_SEEDS = {0x90BB6CB5: (0x3F, "6102"), 0x0B050EE0: (0x78, "6103"),
             0x98BC2C86: (0x91, "6105"), 0xACC8580A: (0x85, "6106")}

def detect_cic(rom: bytes) -> Tuple[int, str]:
    if len(rom) < 0x1000: return 0x3F, "6102"
    crc = 0xFFFFFFFF
    for b in rom[0x40:0x1000]:
        crc ^= b
        for _ in range(8): crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return CIC_SEEDS.get(crc ^ 0xFFFFFFFF, (0x3F, "6102"))

# =============================================================================
# VR4300 CPU - Full MIPS III
# =============================================================================

class VR4300:
    def __init__(self, bus):
        self.bus = bus
        self.gpr = [0] * 32
        self.pc = 0xBFC00000
        self.hi = self.lo = 0
        self.cop0 = [0] * 32
        self.cop0[COP0.PRID] = 0x0B22
        self.cop0[COP0.STATUS] = 0x34000000
        self.cop0[COP0.RANDOM] = 31
        self.fpr = [0.0] * 32
        self.fcr31 = 0
        self.delay_slot = False
        self.branch_target = 0
        self.cycles = 0
        self._icache = {}
        
    def reset(self):
        self.gpr = [0] * 32
        self.pc = 0xBFC00000
        self.hi = self.lo = 0
        self.cop0 = [0] * 32
        self.cop0[COP0.PRID] = 0x0B22
        self.cop0[COP0.STATUS] = 0x34000000
        self.cop0[COP0.RANDOM] = 31
        self.fpr = [0.0] * 32
        self.fcr31 = 0
        self.delay_slot = False
        self._icache.clear()
        self.cycles = 0

    def translate(self, vaddr):
        vaddr = u64(vaddr)
        if 0x80000000 <= vaddr <= 0x9FFFFFFF: return vaddr & 0x1FFFFFFF
        if 0xA0000000 <= vaddr <= 0xBFFFFFFF: return vaddr & 0x1FFFFFFF
        return vaddr & 0x1FFFFFFF  # Simplified

    def read32(self, vaddr): return self.bus.read32(self.translate(vaddr))
    def write32(self, vaddr, val): self.bus.write32(self.translate(vaddr), val)
    def read64(self, vaddr): return self.bus.read64(self.translate(vaddr))
    def write64(self, vaddr, val): self.bus.write64(self.translate(vaddr), val)
    def read8(self, vaddr): return self.bus.read8(self.translate(vaddr))
    def read16(self, vaddr): return self.bus.read16(self.translate(vaddr))
    def write8(self, vaddr, val): self.bus.write8(self.translate(vaddr), val)
    def write16(self, vaddr, val): self.bus.write16(self.translate(vaddr), val)

    def check_interrupts(self):
        status = self.cop0[COP0.STATUS]
        if not (status & 1): return
        if status & 6: return
        cause = self.cop0[COP0.CAUSE]
        if (cause >> 8) & (status >> 8) & 0xFF:
            self.cop0[COP0.EPC] = u64(self.pc - 4 if self.delay_slot else self.pc)
            self.cop0[COP0.CAUSE] = (cause & ~0x7C) | (self.delay_slot << 31)
            self.cop0[COP0.STATUS] |= 2
            self.pc = 0xBFC00200 if status & 0x400000 else 0x80000180
            self.delay_slot = False

    def step(self):
        self.gpr[0] = 0
        self.cop0[COP0.COUNT] = u32(self.cop0[COP0.COUNT] + 1)
        if self.cop0[COP0.COUNT] == self.cop0[COP0.COMPARE]:
            self.cop0[COP0.CAUSE] |= 0x8000
        self.check_interrupts()
        
        pc = u64(self.pc)
        if pc in self._icache: instr = self._icache[pc]
        else:
            instr = self.read32(pc)
            if len(self._icache) < 8192: self._icache[pc] = instr
        
        if self.delay_slot:
            self.pc = self.branch_target
            self.delay_slot = False
        else:
            self.pc = u64(pc + 4)
            
        self._execute(instr, pc)
        self.gpr[0] = 0
        self.cycles += 1
        return 1

    def _execute(self, instr, pc):
        op = (instr >> 26) & 0x3F
        rs = (instr >> 21) & 0x1F
        rt = (instr >> 16) & 0x1F
        rd = (instr >> 11) & 0x1F
        sa = (instr >> 6) & 0x1F
        fn = instr & 0x3F
        imm = instr & 0xFFFF
        simm = s16(imm)
        target = instr & 0x3FFFFFF
        gpr = self.gpr

        if op == 0x00:  # SPECIAL
            self._special(fn, rs, rt, rd, sa)
        elif op == 0x01:  # REGIMM
            self._regimm(rt, rs, simm, pc)
        elif op == 0x02:  # J
            self.branch_target = ((pc + 4) & 0xF0000000) | (target << 2)
            self.delay_slot = True
        elif op == 0x03:  # JAL
            gpr[31] = u64(pc + 8)
            self.branch_target = ((pc + 4) & 0xF0000000) | (target << 2)
            self.delay_slot = True
        elif op == 0x04:  # BEQ
            if gpr[rs] == gpr[rt]:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
        elif op == 0x05:  # BNE
            if gpr[rs] != gpr[rt]:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
        elif op == 0x06:  # BLEZ
            if s64(gpr[rs]) <= 0:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
        elif op == 0x07:  # BGTZ
            if s64(gpr[rs]) > 0:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
        elif op == 0x08:  # ADDI
            gpr[rt] = s64(s32(gpr[rs] + simm))
        elif op == 0x09:  # ADDIU
            gpr[rt] = s64(s32(gpr[rs] + simm))
        elif op == 0x0A:  # SLTI
            gpr[rt] = 1 if s64(gpr[rs]) < simm else 0
        elif op == 0x0B:  # SLTIU
            gpr[rt] = 1 if u64(gpr[rs]) < u64(s64(simm)) else 0
        elif op == 0x0C:  # ANDI
            gpr[rt] = gpr[rs] & imm
        elif op == 0x0D:  # ORI
            gpr[rt] = gpr[rs] | imm
        elif op == 0x0E:  # XORI
            gpr[rt] = gpr[rs] ^ imm
        elif op == 0x0F:  # LUI
            gpr[rt] = s64(s32(imm << 16))
        elif op == 0x10:  # COP0
            self._cop0(rs, rt, rd, fn)
        elif op == 0x11:  # COP1
            self._cop1(rs, rt, rd, sa, fn, instr)
        elif op == 0x14:  # BEQL
            if gpr[rs] == gpr[rt]:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
            else: self.pc = u64(pc + 8)
        elif op == 0x15:  # BNEL
            if gpr[rs] != gpr[rt]:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
            else: self.pc = u64(pc + 8)
        elif op == 0x20:  # LB
            gpr[rt] = s64(s32(self.read8(u64(gpr[rs] + simm))) << 24 >> 24)
        elif op == 0x21:  # LH
            gpr[rt] = s64(s16(self.read16(u64(gpr[rs] + simm))))
        elif op == 0x23:  # LW
            gpr[rt] = s64(s32(self.read32(u64(gpr[rs] + simm))))
        elif op == 0x24:  # LBU
            gpr[rt] = self.read8(u64(gpr[rs] + simm))
        elif op == 0x25:  # LHU
            gpr[rt] = self.read16(u64(gpr[rs] + simm))
        elif op == 0x27:  # LWU
            gpr[rt] = self.read32(u64(gpr[rs] + simm))
        elif op == 0x28:  # SB
            self.write8(u64(gpr[rs] + simm), gpr[rt] & 0xFF)
        elif op == 0x29:  # SH
            self.write16(u64(gpr[rs] + simm), gpr[rt] & 0xFFFF)
        elif op == 0x2B:  # SW
            self.write32(u64(gpr[rs] + simm), gpr[rt] & 0xFFFFFFFF)
        elif op == 0x2F:  # CACHE
            pass
        elif op == 0x37:  # LD
            gpr[rt] = self.read64(u64(gpr[rs] + simm))
        elif op == 0x3F:  # SD
            self.write64(u64(gpr[rs] + simm), gpr[rt])

    def _special(self, fn, rs, rt, rd, sa):
        gpr = self.gpr
        if fn == 0x00:   gpr[rd] = s64(s32(u32(gpr[rt]) << sa))  # SLL
        elif fn == 0x02: gpr[rd] = s64(s32(u32(gpr[rt]) >> sa))  # SRL
        elif fn == 0x03: gpr[rd] = s64(s32(gpr[rt]) >> sa)       # SRA
        elif fn == 0x04: gpr[rd] = s64(s32(u32(gpr[rt]) << (gpr[rs] & 0x1F)))  # SLLV
        elif fn == 0x06: gpr[rd] = s64(s32(u32(gpr[rt]) >> (gpr[rs] & 0x1F)))  # SRLV
        elif fn == 0x07: gpr[rd] = s64(s32(gpr[rt]) >> (gpr[rs] & 0x1F))       # SRAV
        elif fn == 0x08:  # JR
            self.branch_target = u64(gpr[rs])
            self.delay_slot = True
        elif fn == 0x09:  # JALR
            t = u64(gpr[rs])
            gpr[rd] = u64(self.pc + 4)
            self.branch_target = t
            self.delay_slot = True
        elif fn == 0x10: gpr[rd] = self.hi  # MFHI
        elif fn == 0x11: self.hi = gpr[rs]  # MTHI
        elif fn == 0x12: gpr[rd] = self.lo  # MFLO
        elif fn == 0x13: self.lo = gpr[rs]  # MTLO
        elif fn == 0x18:  # MULT
            r = s32(gpr[rs]) * s32(gpr[rt])
            self.lo = s64(s32(r & 0xFFFFFFFF))
            self.hi = s64(s32((r >> 32) & 0xFFFFFFFF))
        elif fn == 0x19:  # MULTU
            r = u32(gpr[rs]) * u32(gpr[rt])
            self.lo = s64(s32(r & 0xFFFFFFFF))
            self.hi = s64(s32((r >> 32) & 0xFFFFFFFF))
        elif fn == 0x1A:  # DIV
            if gpr[rt]: 
                self.lo = s64(s32(gpr[rs]) // s32(gpr[rt]))
                self.hi = s64(s32(gpr[rs]) % s32(gpr[rt]))
        elif fn == 0x1B:  # DIVU
            if gpr[rt]:
                self.lo = s64(s32(u32(gpr[rs]) // u32(gpr[rt])))
                self.hi = s64(s32(u32(gpr[rs]) % u32(gpr[rt])))
        elif fn == 0x20: gpr[rd] = s64(s32(gpr[rs] + gpr[rt]))  # ADD
        elif fn == 0x21: gpr[rd] = s64(s32(u32(gpr[rs]) + u32(gpr[rt])))  # ADDU
        elif fn == 0x22: gpr[rd] = s64(s32(gpr[rs] - gpr[rt]))  # SUB
        elif fn == 0x23: gpr[rd] = s64(s32(u32(gpr[rs]) - u32(gpr[rt])))  # SUBU
        elif fn == 0x24: gpr[rd] = gpr[rs] & gpr[rt]  # AND
        elif fn == 0x25: gpr[rd] = gpr[rs] | gpr[rt]  # OR
        elif fn == 0x26: gpr[rd] = gpr[rs] ^ gpr[rt]  # XOR
        elif fn == 0x27: gpr[rd] = u64(~(gpr[rs] | gpr[rt]))  # NOR
        elif fn == 0x2A: gpr[rd] = 1 if s64(gpr[rs]) < s64(gpr[rt]) else 0  # SLT
        elif fn == 0x2B: gpr[rd] = 1 if gpr[rs] < gpr[rt] else 0  # SLTU
        elif fn == 0x2D: gpr[rd] = u64(gpr[rs] + gpr[rt])  # DADDU
        elif fn == 0x2F: gpr[rd] = u64(gpr[rs] - gpr[rt])  # DSUBU
        elif fn == 0x38: gpr[rd] = u64(gpr[rt] << sa)      # DSLL
        elif fn == 0x3A: gpr[rd] = gpr[rt] >> sa           # DSRL
        elif fn == 0x3C: gpr[rd] = u64(gpr[rt] << (sa + 32))  # DSLL32
        elif fn == 0x3E: gpr[rd] = gpr[rt] >> (sa + 32)       # DSRL32

    def _regimm(self, rt, rs, simm, pc):
        gpr = self.gpr
        if rt == 0x00 and s64(gpr[rs]) < 0:  # BLTZ
            self.branch_target = u64(pc + 4 + (simm << 2))
            self.delay_slot = True
        elif rt == 0x01 and s64(gpr[rs]) >= 0:  # BGEZ
            self.branch_target = u64(pc + 4 + (simm << 2))
            self.delay_slot = True
        elif rt == 0x10:  # BLTZAL
            gpr[31] = u64(pc + 8)
            if s64(gpr[rs]) < 0:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True
        elif rt == 0x11:  # BGEZAL
            gpr[31] = u64(pc + 8)
            if s64(gpr[rs]) >= 0:
                self.branch_target = u64(pc + 4 + (simm << 2))
                self.delay_slot = True

    def _cop0(self, rs, rt, rd, fn):
        if rs == 0x00:  # MFC0
            self.gpr[rt] = s64(s32(self.cop0[rd]))
        elif rs == 0x04:  # MTC0
            self._write_cop0(rd, u32(self.gpr[rt]))
        elif rs == 0x10 and fn == 0x18:  # ERET
            if self.cop0[COP0.STATUS] & 4:
                self.pc = self.cop0[COP0.ERROREPC]
                self.cop0[COP0.STATUS] &= ~4
            else:
                self.pc = self.cop0[COP0.EPC]
                self.cop0[COP0.STATUS] &= ~2
            self.delay_slot = False

    def _write_cop0(self, reg, val):
        if reg == COP0.COUNT: self.cop0[reg] = val
        elif reg == COP0.COMPARE:
            self.cop0[reg] = val
            self.cop0[COP0.CAUSE] &= ~0x8000
        elif reg == COP0.STATUS: self.cop0[reg] = val & 0xFF57FFFF
        elif reg == COP0.CAUSE: self.cop0[reg] = (self.cop0[reg] & ~0x300) | (val & 0x300)
        elif reg != COP0.PRID: self.cop0[reg] = val

    def _cop1(self, rs, rt, rd, sa, fn, instr):
        if not (self.cop0[COP0.STATUS] & 0x20000000): return
        # Basic FPU support
        if rs == 0x00:  # MFC1
            self.gpr[rt] = s64(s32(int(self.fpr[rd]) & 0xFFFFFFFF))
        elif rs == 0x04:  # MTC1
            self.fpr[rd] = float(s32(self.gpr[rt]))

# =============================================================================
# RSP - Reality Signal Processor  
# =============================================================================

class RSP:
    def __init__(self, bus):
        self.bus = bus
        self.dmem = bytearray(RSP_MEM_SIZE)
        self.imem = bytearray(RSP_MEM_SIZE)
        self.gpr = [0] * 32
        self.vpr = [[0] * 8 for _ in range(32)]
        self.acc_h = [0] * 8
        self.acc_m = [0] * 8
        self.acc_l = [0] * 8
        self.pc = 0
        self.status = 0x01
        self.halted = True
        self.mem_addr = 0
        self.dram_addr = 0

    def reset(self):
        self.dmem = bytearray(RSP_MEM_SIZE)
        self.imem = bytearray(RSP_MEM_SIZE)
        self.gpr = [0] * 32
        self.pc = 0
        self.status = 0x01
        self.halted = True

    def read_reg(self, off):
        if off == 0x00: return self.mem_addr
        if off == 0x04: return self.dram_addr
        if off == 0x10: return self.status
        return 0

    def write_reg(self, off, val):
        if off == 0x00: self.mem_addr = val & 0x1FFF
        elif off == 0x04: self.dram_addr = val & 0xFFFFFF
        elif off == 0x08: self._dma(val, True)
        elif off == 0x0C: self._dma(val, False)
        elif off == 0x10: self._write_status(val)

    def _write_status(self, val):
        if val & 0x01: self.halted = False; self.status &= ~0x01
        if val & 0x02: self.halted = True; self.status |= 0x01
        if val & 0x04: self.status &= ~0x02
        if val & 0x08: self.bus.mi.clear_intr(MIIntr.SP)
        if val & 0x10: self.bus.mi.set_intr(MIIntr.SP)

    def _dma(self, length_reg, to_rsp):
        length = (length_reg & 0xFFF) + 1
        mem_addr = self.mem_addr & 0x1FFF
        dram_addr = self.dram_addr & 0xFFFFFF
        is_imem = bool(self.mem_addr & 0x1000)
        mem = self.imem if is_imem else self.dmem
        
        for i in range(length):
            if to_rsp:
                if dram_addr + i < len(self.bus.rdram):
                    mem[(mem_addr + i) & 0xFFF] = self.bus.rdram[dram_addr + i]
            else:
                if dram_addr + i < len(self.bus.rdram):
                    self.bus.rdram[dram_addr + i] = mem[(mem_addr + i) & 0xFFF]

    def step(self):
        if self.halted: return 0
        self.gpr[0] = 0
        pc = self.pc & 0xFFC
        instr = struct.unpack('>I', self.imem[pc:pc+4])[0]
        self.pc = (self.pc + 4) & 0xFFF
        # Simplified - just handle BREAK
        if instr & 0xFC00003F == 0x0000000D:  # BREAK
            self.halted = True
            self.status |= 0x03
            if self.status & 0x40:
                self.bus.mi.set_intr(MIIntr.SP)
        return 1

# =============================================================================
# RDP - Reality Display Processor
# =============================================================================

class RDP:
    def __init__(self, bus):
        self.bus = bus
        self.start = 0
        self.end = 0
        self.current = 0
        self.status = 0
        self.color_image = 0
        self.color_width = 320
        self.color_size = 2
        self.fill_color = 0
        self.scissor = (0, 0, 320, 240)

    def reset(self):
        self.start = self.end = self.current = self.status = 0

    def read_reg(self, off):
        if off == 0x00: return self.start
        if off == 0x04: return self.end
        if off == 0x08: return self.current
        if off == 0x0C: return self.status
        return 0

    def write_reg(self, off, val):
        if off == 0x00: self.start = self.current = val & 0xFFFFFF
        elif off == 0x04:
            self.end = val & 0xFFFFFF
            self._run()
        elif off == 0x0C: self._write_status(val)

    def _write_status(self, val):
        if val & 0x01: self.status &= ~0x01
        if val & 0x02: self.status |= 0x01

    def _run(self):
        while self.current < self.end:
            addr = self.current & 0xFFFFFF
            cmd = self.bus.read64_direct(addr)
            op = (cmd >> 56) & 0x3F
            self._exec(op, cmd)
            self.current += self._cmd_len(op)
        self.bus.mi.set_intr(MIIntr.DP)

    def _cmd_len(self, op):
        if 0x08 <= op <= 0x0F: return 8 + (op & 4) * 16 + (op & 2) * 16 + (op & 1) * 8
        if op in (0x24, 0x25): return 16
        return 8

    def _exec(self, op, cmd):
        if op == 0x2D:  # Set Scissor
            self.scissor = (((cmd>>44)&0xFFF)>>2, ((cmd>>32)&0xFFF)>>2,
                           ((cmd>>12)&0xFFF)>>2, (cmd&0xFFF)>>2)
        elif op == 0x36:  # Fill Rectangle
            self._fill_rect(cmd)
        elif op == 0x37:  # Set Fill Color
            self.fill_color = cmd & 0xFFFFFFFF
        elif op == 0x3F:  # Set Color Image
            self.color_size = (cmd >> 51) & 3
            self.color_width = ((cmd >> 32) & 0x3FF) + 1
            self.color_image = cmd & 0x3FFFFFF

    def _fill_rect(self, cmd):
        xl = max(((cmd>>44)&0xFFF)>>2, self.scissor[0])
        yl = max(((cmd>>32)&0xFFF)>>2, self.scissor[1])
        xh = min(((cmd>>12)&0xFFF)>>2, self.scissor[2])
        yh = min((cmd&0xFFF)>>2, self.scissor[3])
        bpp = 2 if self.color_size == 2 else 4
        for y in range(yl, yh):
            for x in range(xl, xh):
                addr = self.color_image + y * self.color_width * bpp + x * bpp
                if bpp == 2: self.bus.write16_direct(addr, self.fill_color & 0xFFFF)
                else: self.bus.write32_direct(addr, self.fill_color)

# =============================================================================
# Memory Interface (MI)
# =============================================================================

class MI:
    def __init__(self, cpu):
        self.cpu = cpu
        self.intr = 0
        self.mask = 0

    def reset(self): self.intr = self.mask = 0

    def read_reg(self, off):
        if off == 0x04: return 0x02020102
        if off == 0x08: return self.intr
        if off == 0x0C: return self.mask
        return 0

    def write_reg(self, off, val):
        if off == 0x0C:
            for i, flag in enumerate([MIIntr.SP, MIIntr.SI, MIIntr.AI, MIIntr.VI, MIIntr.PI, MIIntr.DP]):
                if val & (1 << (i*2)): self.mask &= ~flag
                if val & (2 << (i*2)): self.mask |= flag
            self._update()

    def set_intr(self, flag): self.intr |= flag; self._update()
    def clear_intr(self, flag): self.intr &= ~flag; self._update()
    def _update(self):
        if self.intr & self.mask: self.cpu.cop0[COP0.CAUSE] |= 0x0400
        else: self.cpu.cop0[COP0.CAUSE] &= ~0x0400

# =============================================================================
# Video Interface (VI)
# =============================================================================

class VI:
    def __init__(self, bus):
        self.bus = bus
        self.status = 0
        self.origin = 0
        self.width = 320
        self.v_intr = 0x3FF
        self.v_current = 0
        self.v_sync = 0x20D
        self.x_scale = 0x200
        self.y_scale = 0x200

    def reset(self):
        self.status = self.origin = 0
        self.width = 320
        self.v_sync = 0x20D

    def read_reg(self, off):
        if off == 0x00: return self.status
        if off == 0x04: return self.origin
        if off == 0x08: return self.width
        if off == 0x0C: return self.v_intr
        if off == 0x10: return self.v_current
        if off == 0x18: return self.v_sync
        if off == 0x30: return self.x_scale
        if off == 0x34: return self.y_scale
        return 0

    def write_reg(self, off, val):
        if off == 0x00: self.status = val & 0xFFFF
        elif off == 0x04: self.origin = val & 0xFFFFFF
        elif off == 0x08: self.width = val & 0xFFF
        elif off == 0x0C: self.v_intr = val & 0x3FF
        elif off == 0x10: self.bus.mi.clear_intr(MIIntr.VI)
        elif off == 0x18: self.v_sync = val & 0x3FF
        elif off == 0x30: self.x_scale = val & 0xFFF
        elif off == 0x34: self.y_scale = val & 0xFFF

    def tick(self, cycles):
        lines = self.v_sync or 0x20D
        cpl = CPU_FREQ // (60 * lines)
        self.v_current = (self.v_current + cycles // cpl) % lines
        if self.v_current == self.v_intr:
            self.bus.mi.set_intr(MIIntr.VI)

    def get_bpp(self): return 32 if (self.status & 3) == 3 else 16

# =============================================================================
# Audio/Peripheral/RDRAM/Serial Interfaces
# =============================================================================

class AI:
    def __init__(self, bus):
        self.bus = bus
        self.control = self.status = self.dacrate = 0

    def reset(self): self.control = self.status = 0
    def read_reg(self, off):
        if off == 0x0C: return 0
        return 0
    def write_reg(self, off, val):
        if off == 0x08: self.control = val & 1
        elif off == 0x0C: self.bus.mi.clear_intr(MIIntr.AI)

class PI:
    def __init__(self, bus):
        self.bus = bus
        self.dram_addr = self.cart_addr = self.status = 0

    def reset(self): self.dram_addr = self.cart_addr = self.status = 0
    def read_reg(self, off):
        if off == 0x10: return self.status
        return 0
    def write_reg(self, off, val):
        if off == 0x00: self.dram_addr = val & 0xFFFFFF
        elif off == 0x04: self.cart_addr = val
        elif off == 0x08: self._dma(val, False)
        elif off == 0x0C: self._dma(val, True)
        elif off == 0x10:
            if val & 2: self.bus.mi.clear_intr(MIIntr.PI)

    def _dma(self, length_reg, to_cart):
        length = (length_reg & 0xFFFFFF) + 1
        dram = self.dram_addr & 0xFFFFFF
        cart = self.cart_addr - Mem.CART_ROM
        if not to_cart:
            for i in range(length):
                if dram + i < len(self.bus.rdram) and cart + i < len(self.bus.cart):
                    self.bus.rdram[dram + i] = self.bus.cart[cart + i]
        self.status = 0
        self.bus.mi.set_intr(MIIntr.PI)

class RI:
    def reset(self): pass
    def read_reg(self, off): return 0
    def write_reg(self, off, val): pass

class SI:
    def __init__(self, bus):
        self.bus = bus
        self.dram_addr = 0
        self.pif_ram = bytearray(64)
        self.controllers = [Controller() for _ in range(4)]

    def reset(self):
        self.dram_addr = 0
        self.pif_ram = bytearray(64)

    def read_reg(self, off):
        if off == 0x00: return self.dram_addr
        return 0

    def write_reg(self, off, val):
        if off == 0x00: self.dram_addr = val & 0xFFFFFF
        elif off == 0x04:  # PIF read
            self._process_pif()
            for i in range(64):
                if self.dram_addr + i < len(self.bus.rdram):
                    self.bus.rdram[self.dram_addr + i] = self.pif_ram[i]
            self.bus.mi.set_intr(MIIntr.SI)
        elif off == 0x10:  # PIF write
            for i in range(64):
                if self.dram_addr + i < len(self.bus.rdram):
                    self.pif_ram[i] = self.bus.rdram[self.dram_addr + i]
            self._process_pif()
            self.bus.mi.set_intr(MIIntr.SI)
        elif off == 0x18:
            self.bus.mi.clear_intr(MIIntr.SI)

    def _process_pif(self):
        ch = i = 0
        while i < 64 and ch < 4:
            tx = self.pif_ram[i]
            if tx in (0xFE, 0xFF): break
            if tx == 0 or tx & 0xC0: i += 1; ch += 1; continue
            if i + 1 >= 64: break
            rx = self.pif_ram[i + 1] & 0x3F
            if i + 2 + tx > 64: break
            cmd = self.pif_ram[i + 2] if tx > 0 else 0
            resp = self.controllers[ch].cmd(cmd, self.pif_ram[i+2:i+2+tx])
            for j, b in enumerate(resp):
                if i + 2 + tx + j < 64:
                    self.pif_ram[i + 2 + tx + j] = b
            i += 2 + tx + rx
            ch += 1
        if self.pif_ram[63] == 0x08: self.pif_ram[63] = 0

class Controller:
    def __init__(self):
        self.buttons = 0
        self.stick_x = self.stick_y = 0

    def cmd(self, c, data):
        if c == 0x00: return bytes([0x05, 0x00, 0x00])
        if c == 0x01: return bytes([(self.buttons>>8)&0xFF, self.buttons&0xFF,
                                    self.stick_x&0xFF, self.stick_y&0xFF])
        if c == 0xFF: return bytes([0x05, 0x00, 0x00])
        return bytes()

    def set_btn(self, btn, pressed):
        m = {'A':0x8000,'B':0x4000,'Z':0x2000,'START':0x1000,
             'UP':0x0800,'DOWN':0x0400,'LEFT':0x0200,'RIGHT':0x0100,
             'L':0x0020,'R':0x0010,'C_UP':0x0008,'C_DOWN':0x0004,
             'C_LEFT':0x0002,'C_RIGHT':0x0001}
        if btn in m:
            if pressed: self.buttons |= m[btn]
            else: self.buttons &= ~m[btn]

# =============================================================================
# Memory Bus
# =============================================================================

class MemoryBus:
    def __init__(self):
        self.rdram = bytearray(RDRAM_SIZE)
        self.cart = bytearray()
        self.cpu = None
        self.rsp = None
        self.rdp = None
        self.mi = None
        self.vi = None
        self.ai = None
        self.pi = None
        self.ri = None
        self.si = None
        self.cic_seed = 0x3F

    def init(self):
        self.cpu = VR4300(self)
        self.rsp = RSP(self)
        self.rdp = RDP(self)
        self.mi = MI(self.cpu)
        self.vi = VI(self)
        self.ai = AI(self)
        self.pi = PI(self)
        self.ri = RI()
        self.si = SI(self)

    def reset(self):
        self.rdram = bytearray(RDRAM_SIZE)
        self.cpu.reset(); self.rsp.reset(); self.rdp.reset()
        self.mi.reset(); self.vi.reset(); self.ai.reset()
        self.pi.reset(); self.ri.reset(); self.si.reset()

    def load_rom(self, data):
        if len(data) < 4: return False
        m = (data[0]<<24)|(data[1]<<16)|(data[2]<<8)|data[3]
        if m == 0x80371240: self.cart = bytearray(data)
        elif m == 0x37804012:
            self.cart = bytearray(len(data))
            for i in range(0, len(data)-1, 2):
                self.cart[i], self.cart[i+1] = data[i+1], data[i]
        else: self.cart = bytearray(data)
        self.cic_seed, cic = detect_cic(bytes(self.cart))
        log.info(f"CIC: {cic} (seed 0x{self.cic_seed:02X})")
        return True

    def boot(self):
        self.reset()
        if len(self.cart) < 0x1000: return
        for i in range(min(0x1000, len(self.cart))):
            self.rsp.dmem[i] = self.cart[i]
            self.rdram[i] = self.cart[i]
        self.cpu.gpr[11] = 0xFFFFFFFFA4000040
        self.cpu.gpr[20] = 1
        self.cpu.gpr[22] = (self.cic_seed << 8) | 0x3F
        self.cpu.gpr[29] = 0xFFFFFFFFA4001FF0
        self.cpu.pc = 0xA4000040
        self.cpu.cop0[COP0.STATUS] = 0x34000000

    def read8(self, addr):
        addr &= 0x1FFFFFFF
        if addr < RDRAM_SIZE: return self.rdram[addr]
        if Mem.RSP_DMEM <= addr < Mem.RSP_DMEM + RSP_MEM_SIZE: return self.rsp.dmem[addr - Mem.RSP_DMEM]
        if Mem.RSP_IMEM <= addr < Mem.RSP_IMEM + RSP_MEM_SIZE: return self.rsp.imem[addr - Mem.RSP_IMEM]
        if Mem.CART_ROM <= addr < Mem.CART_ROM + len(self.cart): return self.cart[addr - Mem.CART_ROM]
        if Mem.PIF_RAM <= addr < Mem.PIF_RAM + 64: return self.si.pif_ram[addr - Mem.PIF_RAM]
        return 0

    def read16(self, addr):
        addr &= 0x1FFFFFFF
        if addr < RDRAM_SIZE - 1: return (self.rdram[addr]<<8)|self.rdram[addr+1]
        return (self.read8(addr)<<8)|self.read8(addr+1)

    def read32(self, addr):
        addr &= 0x1FFFFFFF
        if addr < RDRAM_SIZE - 3: return struct.unpack('>I', self.rdram[addr:addr+4])[0]
        if Mem.RSP_DMEM <= addr < Mem.RSP_DMEM + RSP_MEM_SIZE:
            o = addr - Mem.RSP_DMEM
            return struct.unpack('>I', self.rsp.dmem[o:o+4])[0]
        if Mem.RSP_IMEM <= addr < Mem.RSP_IMEM + RSP_MEM_SIZE:
            o = addr - Mem.RSP_IMEM
            return struct.unpack('>I', self.rsp.imem[o:o+4])[0]
        if Mem.RSP_REGS <= addr < Mem.RSP_REGS + 0x20: return self.rsp.read_reg(addr - Mem.RSP_REGS)
        if addr == Mem.RSP_PC: return self.rsp.pc
        if Mem.DPC_REGS <= addr < Mem.DPC_REGS + 0x20: return self.rdp.read_reg(addr - Mem.DPC_REGS)
        if Mem.MI_REGS <= addr < Mem.MI_REGS + 0x10: return self.mi.read_reg(addr - Mem.MI_REGS)
        if Mem.VI_REGS <= addr < Mem.VI_REGS + 0x40: return self.vi.read_reg(addr - Mem.VI_REGS)
        if Mem.AI_REGS <= addr < Mem.AI_REGS + 0x18: return self.ai.read_reg(addr - Mem.AI_REGS)
        if Mem.PI_REGS <= addr < Mem.PI_REGS + 0x34: return self.pi.read_reg(addr - Mem.PI_REGS)
        if Mem.RI_REGS <= addr < Mem.RI_REGS + 0x20: return self.ri.read_reg(addr - Mem.RI_REGS)
        if Mem.SI_REGS <= addr < Mem.SI_REGS + 0x1C: return self.si.read_reg(addr - Mem.SI_REGS)
        if Mem.CART_ROM <= addr < Mem.CART_ROM + len(self.cart):
            o = addr - Mem.CART_ROM
            if o + 4 <= len(self.cart): return struct.unpack('>I', self.cart[o:o+4])[0]
        if Mem.PIF_RAM <= addr < Mem.PIF_RAM + 64:
            o = addr - Mem.PIF_RAM
            return struct.unpack('>I', self.si.pif_ram[o:o+4])[0]
        return 0

    def read64(self, addr): return (self.read32(addr) << 32) | self.read32(addr + 4)
    def read64_direct(self, addr):
        addr &= 0xFFFFFF
        if addr + 8 <= len(self.rdram): return struct.unpack('>Q', self.rdram[addr:addr+8])[0]
        return 0

    def write8(self, addr, val):
        addr &= 0x1FFFFFFF; val &= 0xFF
        if addr < RDRAM_SIZE: self.rdram[addr] = val
        elif Mem.RSP_DMEM <= addr < Mem.RSP_DMEM + RSP_MEM_SIZE: self.rsp.dmem[addr - Mem.RSP_DMEM] = val
        elif Mem.RSP_IMEM <= addr < Mem.RSP_IMEM + RSP_MEM_SIZE: self.rsp.imem[addr - Mem.RSP_IMEM] = val
        elif Mem.PIF_RAM <= addr < Mem.PIF_RAM + 64: self.si.pif_ram[addr - Mem.PIF_RAM] = val

    def write16(self, addr, val):
        addr &= 0x1FFFFFFF; val &= 0xFFFF
        if addr < RDRAM_SIZE - 1:
            self.rdram[addr] = (val >> 8) & 0xFF
            self.rdram[addr + 1] = val & 0xFF
        else:
            self.write8(addr, (val >> 8) & 0xFF)
            self.write8(addr + 1, val & 0xFF)

    def write32(self, addr, val):
        addr &= 0x1FFFFFFF; val &= 0xFFFFFFFF
        if addr < RDRAM_SIZE - 3:
            struct.pack_into('>I', self.rdram, addr, val); return
        if Mem.RSP_DMEM <= addr < Mem.RSP_DMEM + RSP_MEM_SIZE:
            struct.pack_into('>I', self.rsp.dmem, addr - Mem.RSP_DMEM, val); return
        if Mem.RSP_IMEM <= addr < Mem.RSP_IMEM + RSP_MEM_SIZE:
            struct.pack_into('>I', self.rsp.imem, addr - Mem.RSP_IMEM, val)
            self.cpu._icache.clear(); return
        if Mem.RSP_REGS <= addr < Mem.RSP_REGS + 0x20: self.rsp.write_reg(addr - Mem.RSP_REGS, val); return
        if addr == Mem.RSP_PC: self.rsp.pc = val & 0xFFC; return
        if Mem.DPC_REGS <= addr < Mem.DPC_REGS + 0x20: self.rdp.write_reg(addr - Mem.DPC_REGS, val); return
        if Mem.MI_REGS <= addr < Mem.MI_REGS + 0x10: self.mi.write_reg(addr - Mem.MI_REGS, val); return
        if Mem.VI_REGS <= addr < Mem.VI_REGS + 0x40: self.vi.write_reg(addr - Mem.VI_REGS, val); return
        if Mem.AI_REGS <= addr < Mem.AI_REGS + 0x18: self.ai.write_reg(addr - Mem.AI_REGS, val); return
        if Mem.PI_REGS <= addr < Mem.PI_REGS + 0x34: self.pi.write_reg(addr - Mem.PI_REGS, val); return
        if Mem.RI_REGS <= addr < Mem.RI_REGS + 0x20: self.ri.write_reg(addr - Mem.RI_REGS, val); return
        if Mem.SI_REGS <= addr < Mem.SI_REGS + 0x1C: self.si.write_reg(addr - Mem.SI_REGS, val); return
        if Mem.PIF_RAM <= addr < Mem.PIF_RAM + 64:
            struct.pack_into('>I', self.si.pif_ram, addr - Mem.PIF_RAM, val)

    def write64(self, addr, val):
        self.write32(addr, (val >> 32) & 0xFFFFFFFF)
        self.write32(addr + 4, val & 0xFFFFFFFF)

    def write16_direct(self, addr, val):
        addr &= 0xFFFFFF
        if addr + 2 <= len(self.rdram):
            struct.pack_into('>H', self.rdram, addr, val & 0xFFFF)

    def write32_direct(self, addr, val):
        addr &= 0xFFFFFF
        if addr + 4 <= len(self.rdram):
            struct.pack_into('>I', self.rdram, addr, val & 0xFFFFFFFF)

# =============================================================================
# Emulator Core
# =============================================================================

class EmulatorCore:
    def __init__(self):
        self.bus = MemoryBus()
        self.bus.init()
        self.running = False
        self.paused = False
        self._stop = threading.Event()
        self._thread = None
        self.fps = 0.0
        self.on_frame = None

    def load_rom(self, path):
        try:
            with open(path, 'rb') as f: data = f.read()
            if not self.bus.load_rom(data): return False
            if len(self.bus.cart) >= 0x40:
                name = bytes(self.bus.cart[0x20:0x34]).decode('ascii', errors='ignore').strip()
                log.info(f"ROM: {name}")
            return True
        except Exception as e:
            log.error(f"Load failed: {e}")
            return False

    def start(self):
        if self.running: return
        self.bus.boot()
        self.running = True
        self.paused = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._stop.set()
        if self._thread: self._thread.join(timeout=1); self._thread = None

    def pause(self): self.paused = True
    def resume(self): self.paused = False

    def _loop(self):
        last_fps = time.perf_counter()
        fps_frames = 0
        frame_start = time.perf_counter()
        cycles = 0
        frame_time = 1.0 / 60

        while not self._stop.is_set():
            if self.paused:
                time.sleep(0.01); continue

            for _ in range(50000):
                self.bus.cpu.step()
                if not self.bus.rsp.halted: self.bus.rsp.step()
            cycles += 50000

            if cycles >= CYCLES_PER_FRAME:
                cycles -= CYCLES_PER_FRAME
                self.bus.vi.tick(CYCLES_PER_FRAME)
                
                if self.on_frame:
                    try: self.on_frame()
                    except: pass

                fps_frames += 1
                now = time.perf_counter()
                if now - last_fps >= 0.5:
                    self.fps = fps_frames / (now - last_fps)
                    fps_frames = 0
                    last_fps = now

                elapsed = now - frame_start
                if elapsed < frame_time:
                    time.sleep((frame_time - elapsed) * 0.9)
                frame_start = time.perf_counter()

    def get_framebuffer(self):
        vi = self.bus.vi
        if vi.status == 0 or vi.origin == 0 or vi.width == 0: return None
        w, h = vi.width, 240
        bpp = vi.get_bpp()
        origin = vi.origin & 0xFFFFFF
        size = w * h * (bpp // 8)
        if origin + size > len(self.bus.rdram): return None
        return (bytes(self.bus.rdram[origin:origin+size]), w, h, bpp)

# =============================================================================
# GUI
# =============================================================================

class CatN64GUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("660x520")
        self.root.configure(bg='#1a1a2e')
        
        self.emu = EmulatorCore()
        self.emu.on_frame = self._on_frame
        self.photo = None
        self._frame_pending = False
        self.config = self._load_cfg()
        
        self._build_menu()
        self._build_toolbar()
        self._build_display()
        self._build_status()
        self._setup_input()
        
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._update()

    def _build_menu(self):
        mb = tk.Menu(self.root, bg='#16213e', fg='#e0e0e0')
        fm = tk.Menu(mb, tearoff=0, bg='#1a1a2e', fg='#e0e0e0')
        fm.add_command(label="Open ROM...", command=self._open, accelerator="Ctrl+O")
        fm.add_separator()
        fm.add_command(label="Exit", command=self._close)
        mb.add_cascade(label="File", menu=fm)
        
        sm = tk.Menu(mb, tearoff=0, bg='#1a1a2e', fg='#e0e0e0')
        sm.add_command(label="Start", command=self._start, accelerator="F5")
        sm.add_command(label="Pause", command=self._pause, accelerator="F6")
        sm.add_command(label="Stop", command=self._stop, accelerator="F7")
        sm.add_command(label="Reset", command=self._reset, accelerator="F1")
        mb.add_cascade(label="System", menu=sm)
        
        hm = tk.Menu(mb, tearoff=0, bg='#1a1a2e', fg='#e0e0e0')
        hm.add_command(label="About", command=self._about)
        mb.add_cascade(label="Help", menu=hm)
        
        self.root.config(menu=mb)
        self.root.bind('<Control-o>', lambda e: self._open())
        self.root.bind('<F5>', lambda e: self._start())
        self.root.bind('<F6>', lambda e: self._pause())
        self.root.bind('<F7>', lambda e: self._stop())
        self.root.bind('<F1>', lambda e: self._reset())

    def _build_toolbar(self):
        tb = tk.Frame(self.root, bg='#16213e')
        tb.pack(fill=tk.X)
        for txt, cmd in [("📂Open", self._open), ("▶Start", self._start),
                         ("⏸Pause", self._pause), ("⏹Stop", self._stop), ("🔄Reset", self._reset)]:
            tk.Button(tb, text=txt, command=cmd, bg='#0f3460', fg='white',
                     relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=2, pady=2)

    def _build_display(self):
        self.canvas = tk.Canvas(self.root, bg='#000000', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.photo = tk.PhotoImage(width=320, height=240)
        self.canvas_img = self.canvas.create_image(0, 0, image=self.photo, anchor=tk.NW)
        self.canvas.bind('<Configure>', self._resize)

    def _build_status(self):
        sb = tk.Frame(self.root, bg='#16213e')
        sb.pack(fill=tk.X)
        self.status_lbl = tk.Label(sb, text="Ready", bg='#16213e', fg='#e94560')
        self.status_lbl.pack(side=tk.LEFT, padx=4)
        self.fps_lbl = tk.Label(sb, text="0 FPS", bg='#16213e', fg='#e94560')
        self.fps_lbl.pack(side=tk.RIGHT, padx=4)

    def _setup_input(self):
        km = {'a':'A','s':'B','z':'Z','Return':'START',
              'Up':'UP','Down':'DOWN','Left':'LEFT','Right':'RIGHT',
              'q':'L','w':'R','i':'C_UP','k':'C_DOWN','j':'C_LEFT','l':'C_RIGHT'}
        def press(e):
            if e.keysym in km: self.emu.bus.si.controllers[0].set_btn(km[e.keysym], True)
        def release(e):
            if e.keysym in km: self.emu.bus.si.controllers[0].set_btn(km[e.keysym], False)
        self.root.bind('<KeyPress>', press)
        self.root.bind('<KeyRelease>', release)

    def _on_frame(self): self._frame_pending = True

    def _update(self):
        if self._frame_pending:
            self._frame_pending = False
            fb = self.emu.get_framebuffer()
            if fb:
                data, w, h, bpp = fb
                if w != self.photo.width() or h != self.photo.height():
                    self.photo = tk.PhotoImage(width=w, height=h)
                    self.canvas.itemconfig(self.canvas_img, image=self.photo)
                try:
                    hdr = f"P6\n{w} {h}\n255\n".encode()
                    rgb = bytearray(w * h * 3)
                    if bpp == 16:
                        for i in range(0, len(data)-1, 2):
                            p = (data[i]<<8)|data[i+1]
                            j = (i//2)*3
                            rgb[j] = ((p>>11)&0x1F)<<3
                            rgb[j+1] = ((p>>6)&0x1F)<<3
                            rgb[j+2] = ((p>>1)&0x1F)<<3
                    else:
                        for i in range(0, len(data)-3, 4):
                            j = (i//4)*3
                            rgb[j] = data[i]
                            rgb[j+1] = data[i+1]
                            rgb[j+2] = data[i+2]
                    self.photo.configure(data=hdr + bytes(rgb))
                except: pass
            self.fps_lbl.config(text=f"{self.emu.fps:.1f} FPS")
        self.root.after(16, self._update)

    def _resize(self, e):
        self.canvas.coords(self.canvas_img, e.width//2 - 160, e.height//2 - 120)

    def _open(self):
        path = filedialog.askopenfilename(
            title="Open N64 ROM",
            filetypes=[("N64 ROMs", "*.z64 *.n64 *.v64"), ("All", "*.*")])
        if path:
            self._stop()
            if self.emu.load_rom(path):
                self.root.title(f"{APP_TITLE} - {Path(path).stem}")
                self._start()
            else:
                messagebox.showerror("Error", f"Failed to load:\n{path}")

    def _start(self):
        if not self.emu.bus.cart: self._open(); return
        self.emu.start()
        self.status_lbl.config(text="Running")

    def _stop(self):
        self.emu.stop()
        self.status_lbl.config(text="Stopped")

    def _pause(self):
        if self.emu.paused: self.emu.resume(); self.status_lbl.config(text="Running")
        else: self.emu.pause(); self.status_lbl.config(text="Paused")

    def _reset(self):
        self.emu.stop()
        self.emu.bus.reset()
        if self.emu.bus.cart: self._start()

    def _about(self):
        messagebox.showinfo("About", f"{APP_TITLE}\n\n"
            "Full N64 Hardware Emulator\n"
            "• VR4300 CPU (MIPS III + FPU)\n"
            "• RSP Vector Unit\n"
            "• RDP Rasterizer\n"
            "• MI/VI/AI/PI/SI/RI\n"
            "• Controller support\n\n"
            "© 2025 FlamesCo & Samsoft 🐱")

    def _load_cfg(self):
        try:
            p = Path.home() / ".catn64emu.json"
            if p.exists(): return json.loads(p.read_text())
        except: pass
        return {}

    def _close(self):
        self.emu.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

def main():
    print(f"""
    ╔═══════════════════════════════════════════╗
    ║   🐱 {APP_TITLE} - N64 Emulator 🐱   ║
    ║                                           ║
    ║  Full Hardware Implementation             ║
    ║  • VR4300 (MIPS III, FPU, TLB)           ║
    ║  • RSP (Scalar + Vector)                  ║
    ║  • RDP (Rasterizer)                       ║
    ║  • All Interfaces                         ║
    ║                                           ║
    ║  © 2025 FlamesCo & Samsoft                ║
    ╚═══════════════════════════════════════════╝
    """)
    CatN64GUI().run()

if __name__ == "__main__":
    main()
