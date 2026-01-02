#!/usr/bin/env python3
"""
CatN64EMU 1.X (GUI Test + CPU/MEM/VI Core)
Project64-style interface with a practical MIPS subset and a fake VI

Originally based on EMUDARKNESS N64 Emulator v1.0 (Harness)
Refactored/renamed and polished for CatN64EMU 1.X

© 2025 FlamesCo & Samsoft - CatN64EMU Project
License: GPL-3.0-or-later
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# =============================================================================
# App metadata
# =============================================================================

APP_NAME: str = "CatN64EMU"
APP_VERSION: str = "1.X"
APP_TITLE: str = f"{APP_NAME} {APP_VERSION}"

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(level=logging.INFO, format='[%(name)s] %(message)s')
log_cpu = logging.getLogger('CPU')
log_bus = logging.getLogger('BUS')
log_vi = logging.getLogger('VI')
log_cop0 = logging.getLogger('COP0')

# =============================================================================
# Constants / Addresses
# =============================================================================

HZ_TARGET = 60
FRAME_TIME_SEC = 1.0 / HZ_TARGET
CYCLES_PER_FRAME = 93_750_000 // HZ_TARGET  # ~1.5M cycles/frame at 93.75MHz

# Memory map (simplified for test harness)
RDRAM_BASE = 0x00000000
RDRAM_SIZE = 0x00800000  # 8 MB (realistic N64 RDRAM)

VI_BASE = 0x04000000
VI_WINDOW_SIZE = 0x01000000

CART_BASE = 0x10000000
CART_SIZE_MAX = 0x04000000  # 64 MB cart window

# Default fake VI framebuffer config (ARGB32)
FB_WIDTH_DEFAULT = 320
FB_HEIGHT_DEFAULT = 240

CONFIG_PATH = Path.home() / ".catn64emu_config.json"

# =============================================================================
# Utility helpers
# =============================================================================


def be_store32(mem: bytearray, off: int, val: int) -> None:
    """Safely store 32-bit big-endian value with bounds check."""
    if off < 0 or off + 4 > len(mem):
        return
    mem[off:off+4] = struct.pack('>I', val & 0xFFFFFFFF)


def be_load32(mem: bytearray, off: int) -> int:
    """Safely load 32-bit big-endian value with bounds check."""
    if off < 0 or off + 4 > len(mem):
        return 0
    return struct.unpack('>I', mem[off:off+4])[0]


def sign16(x: int) -> int:
    x &= 0xFFFF
    return x if x < 0x8000 else x - 0x10000


def u32(x: int) -> int:
    return x & 0xFFFFFFFF


def colorize_fps(fps: float) -> str:
    if fps >= 55:
        return "#00FF00"
    elif fps >= 40:
        return "#CCCC00"
    else:
        return "#FF4444"


# =============================================================================
# Plugin Interfaces
# =============================================================================

class VideoPlugin:
    def update(self, photo: tk.PhotoImage, framebuffer: bytearray, width: int, height: int, dirty: bool) -> bool:
        raise NotImplementedError


class AudioPlugin:
    def push_samples(self, samples):
        pass


class InputPlugin:
    def translate_key(self, keysym, pressed: bool):
        pass


class RSPPlugin:
    def step(self, cycles: int):
        pass


# =============================================================================
# Optimized Video Plugin (PPM-based for speed)
# =============================================================================

class DefaultTkVideoPlugin(VideoPlugin):
    """Optimized video plugin using PPM format for faster updates."""
    
    def __init__(self) -> None:
        self._frame_count = 0
        self._ppm_header: bytes = b""
        self._last_w = 0
        self._last_h = 0
        # Pre-allocate RGB buffer
        self._rgb_buffer: bytearray = bytearray()

    def update(self, photo: tk.PhotoImage, fb: bytearray, w: int, h: int, dirty: bool) -> bool:
        if not dirty:
            return False
        
        expected_size = w * h * 4
        if len(fb) < expected_size:
            return False

        # Cache PPM header and resize buffer if needed
        if w != self._last_w or h != self._last_h:
            self._ppm_header = f"P6\n{w} {h}\n255\n".encode('ascii')
            self._rgb_buffer = bytearray(w * h * 3)
            self._last_w = w
            self._last_h = h

        # Convert ARGB32 to RGB24 efficiently
        rgb = self._rgb_buffer
        src = 0
        dst = 0
        pixel_count = w * h
        
        # Process in chunks for better cache performance
        for _ in range(pixel_count):
            rgb[dst] = fb[src + 1]      # R
            rgb[dst + 1] = fb[src + 2]  # G
            rgb[dst + 2] = fb[src + 3]  # B
            src += 4
            dst += 3

        # Build PPM and update photo
        try:
            ppm_data = self._ppm_header + bytes(rgb)
            photo.configure(data=ppm_data)
            self._frame_count += 1
            return True
        except Exception as e:
            log_vi.error(f"PhotoImage update failed: {e}")
            return False


# =============================================================================
# Video Interface (Fake VI + Framebuffer)
# =============================================================================

class VideoInterface:
    def __init__(self, width: int = FB_WIDTH_DEFAULT, height: int = FB_HEIGHT_DEFAULT) -> None:
        self.width = width
        self.height = height
        self.pitch = width * 4
        self.size = self.pitch * self.height
        self.fb = bytearray(self.size)
        self.dirty = True
        self.clear(0xFF000000)  # Initialize to black

    def _in_fb_bounds(self, off: int) -> bool:
        return 0 <= off <= self.size - 4

    def write32(self, paddr: int, value: int) -> bool:
        if not (VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE):
            return False
        off = paddr - VI_BASE
        if self._in_fb_bounds(off):
            self.fb[off] = (value >> 24) & 0xFF
            self.fb[off + 1] = (value >> 16) & 0xFF
            self.fb[off + 2] = (value >> 8) & 0xFF
            self.fb[off + 3] = value & 0xFF
            self.dirty = True
            return True
        return True

    def read32(self, paddr: int) -> int:
        if not (VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE):
            return 0
        off = paddr - VI_BASE
        if self._in_fb_bounds(off):
            return (self.fb[off] << 24) | (self.fb[off+1] << 16) | (self.fb[off+2] << 8) | self.fb[off+3]
        return 0

    def clear(self, argb: int = 0xFF000000) -> None:
        a = (argb >> 24) & 0xFF
        r = (argb >> 16) & 0xFF
        g = (argb >> 8) & 0xFF
        b = argb & 0xFF
        pixel = bytes([a, r, g, b])
        self.fb[:] = pixel * (self.width * self.height)
        self.dirty = True


# =============================================================================
# COP0 (minimal stub)
# =============================================================================

class COP0:
    def __init__(self) -> None:
        self.Status = 0x0
        self.Cause = 0x0
        self.EPC = 0x0

    def raise_exception(self, cause_code: int, epc: int) -> None:
        self.Cause = cause_code & 0xFF
        self.EPC = epc
        log_cop0.warning(f"Exception cause={self.Cause:#x} EPC={self.EPC:#x}")


# =============================================================================
# TLB stub
# =============================================================================

class TLB:
    @staticmethod
    def vaddr_to_paddr(vaddr: int) -> Optional[int]:
        vaddr &= 0xFFFFFFFF
        if 0x80000000 <= vaddr <= 0x9FFFFFFF:
            return vaddr & 0x1FFFFFFF
        if 0xA0000000 <= vaddr <= 0xBFFFFFFF:
            return vaddr & 0x1FFFFFFF
        if vaddr < 0x80000000:
            return vaddr
        return None


# =============================================================================
# Memory Bus
# =============================================================================

class MemoryBus:
    def __init__(self, vi: VideoInterface) -> None:
        self.rdram = bytearray(RDRAM_SIZE)
        self.vi = vi
        self.cart = bytearray()
        self.cart_size = 0
        self.access_log = deque(maxlen=64)

    def load_cart(self, data: bytes) -> None:
        clamped = data[:CART_SIZE_MAX]
        pad_len = (4 - (len(clamped) % 4)) % 4
        if pad_len:
            clamped = clamped + bytes(pad_len)
        self.cart = bytearray(clamped)
        self.cart_size = len(self.cart)
        log_bus.info(f"Cartridge loaded: {self.cart_size} bytes")

    def read32_p(self, paddr: int) -> int:
        if paddr & 3:
            return 0
        if RDRAM_BASE <= paddr < RDRAM_BASE + RDRAM_SIZE:
            off = paddr - RDRAM_BASE
            return be_load32(self.rdram, off)
        if VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE:
            return self.vi.read32(paddr)
        if CART_BASE <= paddr < CART_BASE + CART_SIZE_MAX:
            off = paddr - CART_BASE
            if self.cart_size > 0 and off + 4 <= self.cart_size:
                return be_load32(self.cart, off)
            return 0
        return 0

    def write32_p(self, paddr: int, value: int) -> None:
        value = u32(value)
        if paddr & 3:
            return
        if RDRAM_BASE <= paddr < RDRAM_BASE + RDRAM_SIZE:
            off = paddr - RDRAM_BASE
            be_store32(self.rdram, off, value)
            return
        if VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE:
            self.vi.write32(paddr, value)
            return

    def read32(self, vaddr: int) -> int:
        paddr = TLB.vaddr_to_paddr(vaddr)
        if paddr is None:
            return 0
        return self.read32_p(paddr)

    def write32(self, vaddr: int, value: int) -> None:
        paddr = TLB.vaddr_to_paddr(vaddr)
        if paddr is None:
            return
        self.write32_p(paddr, value)


# =============================================================================
# CPU (VR4300-like, partial)
# =============================================================================

class CPU:
    def __init__(self, bus: MemoryBus, cop0: COP0) -> None:
        self.bus = bus
        self.cop0 = cop0
        self.reg = [0] * 32
        self.hi = 0
        self.lo = 0
        self.pc = 0x80000000
        self.running = False
        self._break_hit = False
        self.breakpoints: set[int] = set()
        
        # Instruction cache for slight speedup
        self._icache: dict[int, int] = {}
        self._icache_max = 4096

    def reset(self, pc: int = 0x80000000) -> None:
        self.reg = [0] * 32
        self.hi = self.lo = 0
        self.pc = pc & 0xFFFFFFFF
        self.running = False
        self._break_hit = False
        self._icache.clear()

    def set_breakpoint(self, addr: int) -> None:
        self.breakpoints.add(addr & 0xFFFFFFFF)

    def clear_breakpoint(self, addr: int) -> None:
        self.breakpoints.discard(addr & 0xFFFFFFFF)

    def fetch32(self, addr: int) -> int:
        # Simple instruction cache
        if addr in self._icache:
            return self._icache[addr]
        val = self.bus.read32(addr)
        if len(self._icache) < self._icache_max:
            self._icache[addr] = val
        return val

    def step(self, allow_delay_slot: bool = True) -> int:
        if self.pc in self.breakpoints:
            self.running = False
            return 0

        pc = self.pc
        instr = self.fetch32(pc)
        next_pc = u32(pc + 4)

        op = (instr >> 26) & 0x3F
        rs = (instr >> 21) & 0x1F
        rt = (instr >> 16) & 0x1F
        rd = (instr >> 11) & 0x1F
        sa = (instr >> 6) & 0x1F
        fn = instr & 0x3F
        imm = instr & 0xFFFF
        simm = sign16(imm)
        jidx = instr & 0x03FFFFFF

        branch_taken = False
        target: Optional[int] = None
        reg = self.reg  # Local reference for speed

        if op == 0x00:
            if instr == 0x00000000:
                pass  # NOP
            elif fn == 0x21:  # ADDU
                if rd: reg[rd] = u32(reg[rs] + reg[rt])
            elif fn == 0x23:  # SUBU
                if rd: reg[rd] = u32(reg[rs] - reg[rt])
            elif fn == 0x24:  # AND
                if rd: reg[rd] = reg[rs] & reg[rt]
            elif fn == 0x25:  # OR
                if rd: reg[rd] = reg[rs] | reg[rt]
            elif fn == 0x26:  # XOR
                if rd: reg[rd] = reg[rs] ^ reg[rt]
            elif fn == 0x27:  # NOR
                if rd: reg[rd] = u32(~(reg[rs] | reg[rt]))
            elif fn == 0x00:  # SLL
                if rd: reg[rd] = u32(reg[rt] << sa)
            elif fn == 0x02:  # SRL
                if rd: reg[rd] = reg[rt] >> sa
            elif fn == 0x03:  # SRA
                val = reg[rt]
                if val & 0x80000000:
                    val = val | (-1 << 32)
                if rd: reg[rd] = u32(val >> sa)
            elif fn == 0x08:  # JR
                target = reg[rs]
                branch_taken = True
            elif fn == 0x09:  # JALR
                if rd: reg[rd] = next_pc
                target = reg[rs]
                branch_taken = True
            elif fn == 0x0D:  # BREAK
                self._break_hit = True
                self.running = False
            elif fn == 0x2A:  # SLT
                s_rs = reg[rs] if reg[rs] < 0x80000000 else reg[rs] - 0x100000000
                s_rt = reg[rt] if reg[rt] < 0x80000000 else reg[rt] - 0x100000000
                if rd: reg[rd] = 1 if s_rs < s_rt else 0
            elif fn == 0x2B:  # SLTU
                if rd: reg[rd] = 1 if reg[rs] < reg[rt] else 0
        elif op == 0x02:  # J
            target = (next_pc & 0xF0000000) | (jidx << 2)
            branch_taken = True
        elif op == 0x03:  # JAL
            reg[31] = next_pc
            target = (next_pc & 0xF0000000) | (jidx << 2)
            branch_taken = True
        elif op == 0x04:  # BEQ
            if reg[rs] == reg[rt]:
                target = u32(next_pc + (simm << 2))
                branch_taken = True
        elif op == 0x05:  # BNE
            if reg[rs] != reg[rt]:
                target = u32(next_pc + (simm << 2))
                branch_taken = True
        elif op == 0x06:  # BLEZ
            s_rs = reg[rs] if reg[rs] < 0x80000000 else reg[rs] - 0x100000000
            if s_rs <= 0:
                target = u32(next_pc + (simm << 2))
                branch_taken = True
        elif op == 0x07:  # BGTZ
            s_rs = reg[rs] if reg[rs] < 0x80000000 else reg[rs] - 0x100000000
            if s_rs > 0:
                target = u32(next_pc + (simm << 2))
                branch_taken = True
        elif op == 0x08:  # ADDI (treat as ADDIU for harness)
            if rt: reg[rt] = u32(reg[rs] + simm)
        elif op == 0x09:  # ADDIU
            if rt: reg[rt] = u32(reg[rs] + simm)
        elif op == 0x0A:  # SLTI
            s_rs = reg[rs] if reg[rs] < 0x80000000 else reg[rs] - 0x100000000
            if rt: reg[rt] = 1 if s_rs < simm else 0
        elif op == 0x0B:  # SLTIU
            if rt: reg[rt] = 1 if reg[rs] < (imm & 0xFFFF) else 0
        elif op == 0x0C:  # ANDI
            if rt: reg[rt] = reg[rs] & imm
        elif op == 0x0D:  # ORI
            if rt: reg[rt] = reg[rs] | imm
        elif op == 0x0E:  # XORI
            if rt: reg[rt] = reg[rs] ^ imm
        elif op == 0x0F:  # LUI
            if rt: reg[rt] = (imm << 16) & 0xFFFFFFFF
        elif op == 0x20:  # LB
            addr = u32(reg[rs] + simm)
            paddr = TLB.vaddr_to_paddr(addr)
            if paddr is not None and paddr < RDRAM_SIZE:
                val = self.bus.rdram[paddr]
                if val & 0x80:
                    val = val | 0xFFFFFF00
                if rt: reg[rt] = u32(val)
        elif op == 0x21:  # LH
            addr = u32(reg[rs] + simm)
            if not (addr & 1):
                paddr = TLB.vaddr_to_paddr(addr)
                if paddr is not None and paddr + 1 < RDRAM_SIZE:
                    val = (self.bus.rdram[paddr] << 8) | self.bus.rdram[paddr + 1]
                    if val & 0x8000:
                        val = val | 0xFFFF0000
                    if rt: reg[rt] = u32(val)
        elif op == 0x23:  # LW
            addr = u32(reg[rs] + simm)
            if not (addr & 3):
                if rt: reg[rt] = self.bus.read32(addr)
        elif op == 0x24:  # LBU
            addr = u32(reg[rs] + simm)
            paddr = TLB.vaddr_to_paddr(addr)
            if paddr is not None and paddr < RDRAM_SIZE:
                if rt: reg[rt] = self.bus.rdram[paddr]
        elif op == 0x25:  # LHU
            addr = u32(reg[rs] + simm)
            if not (addr & 1):
                paddr = TLB.vaddr_to_paddr(addr)
                if paddr is not None and paddr + 1 < RDRAM_SIZE:
                    if rt: reg[rt] = (self.bus.rdram[paddr] << 8) | self.bus.rdram[paddr + 1]
        elif op == 0x28:  # SB
            addr = u32(reg[rs] + simm)
            paddr = TLB.vaddr_to_paddr(addr)
            if paddr is not None and paddr < RDRAM_SIZE:
                self.bus.rdram[paddr] = reg[rt] & 0xFF
        elif op == 0x29:  # SH
            addr = u32(reg[rs] + simm)
            if not (addr & 1):
                paddr = TLB.vaddr_to_paddr(addr)
                if paddr is not None and paddr + 1 < RDRAM_SIZE:
                    self.bus.rdram[paddr] = (reg[rt] >> 8) & 0xFF
                    self.bus.rdram[paddr + 1] = reg[rt] & 0xFF
        elif op == 0x2B:  # SW
            addr = u32(reg[rs] + simm)
            if not (addr & 3):
                self.bus.write32(addr, reg[rt])

        # Branch delay slot
        if branch_taken:
            if allow_delay_slot:
                self.pc = next_pc
                self.step(allow_delay_slot=False)
            self.pc = u32(target if target is not None else next_pc)
        else:
            self.pc = next_pc

        reg[0] = 0
        return 1

    def run_cycles(self, cycles: int) -> int:
        """Run multiple cycles efficiently."""
        executed = 0
        while executed < cycles and self.running and not self._break_hit:
            self.step()
            executed += 1
        return executed

    def load_demo_program(self, bus: MemoryBus) -> None:
        """Load demo that fills VI framebuffer with gradient."""
        prog = [
            0x3C010400,       # lui  r1, 0x0400       ; VI base
            0x34210000,       # ori  r1, r1, 0x0000
            0x3C020001,       # lui  r2, 0x0001
            0x34422C00,       # ori  r2, r2, 0x2C00   ; 76800 pixels
            0x00201821,       # addu r3, r1, r0       ; ptr = VI base
            0x3C04FF00,       # lui  r4, 0xFF00       ; A=0xFF, RGB=0
            0x34840000,       # ori  r4, r4, 0x0000
            # loop:
            0xAC640000,       # sw   r4, 0(r3)
            0x24630004,       # addiu r3, r3, 4
            0x24840101,       # addiu r4, r4, 0x0101  ; gradient
            0x2442FFFF,       # addiu r2, r2, -1
            0x1440FFFB,       # bne  r2, r0, loop
            0x00000000,       # nop (delay slot)
            0x0000000D,       # break
        ]
        off = 0
        for w in prog:
            be_store32(bus.rdram, off, w)
            off += 4
        self.reset(pc=0x80000000)
        self.running = True
        log_cpu.info("Demo MIPS program loaded.")


# =============================================================================
# Save States
# =============================================================================

class SaveStates:
    def __init__(self, cpu: CPU, bus: MemoryBus, vi: VideoInterface) -> None:
        self.cpu = cpu
        self.bus = bus
        self.vi = vi

    def _encode_fb(self) -> str:
        if not self.vi.fb:
            return ""
        return base64.b64encode(bytes(self.vi.fb)).decode("ascii")

    def _decode_fb(self, text: str) -> bytes:
        if not text:
            return b""
        try:
            return base64.b64decode(text.encode("ascii"))
        except Exception:
            return b""

    def save_state(self) -> tuple[bool, str]:
        try:
            state = {
                "pc": self.cpu.pc,
                "reg": self.cpu.reg[:],
                "hi": self.cpu.hi,
                "lo": self.cpu.lo,
                "fb_b64": self._encode_fb(),
                "meta": {
                    "app": APP_NAME,
                    "version": APP_VERSION,
                    "timestamp": int(time.time()),
                }
            }
            Path("states").mkdir(exist_ok=True)
            with open("states/quick.sav", "w", encoding="utf-8") as f:
                json.dump(state, f)
            return True, "State saved → states/quick.sav"
        except Exception as e:
            return False, f"Save failed: {e}"

    def load_state(self) -> tuple[bool, str]:
        try:
            with open("states/quick.sav", "r", encoding="utf-8") as f:
                state = json.load(f)
            self.cpu.pc = int(state["pc"]) & 0xFFFFFFFF
            self.cpu.reg = [int(x) & 0xFFFFFFFF for x in state["reg"]]
            self.cpu.hi = int(state["hi"]) & 0xFFFFFFFF
            self.cpu.lo = int(state["lo"]) & 0xFFFFFFFF
            fb_bytes = self._decode_fb(state.get("fb_b64", ""))
            if fb_bytes:
                n = min(len(self.vi.fb), len(fb_bytes))
                self.vi.fb[:n] = fb_bytes[:n]
                self.vi.dirty = True
            return True, "State loaded successfully"
        except FileNotFoundError:
            return False, "No saved state found."
        except Exception as e:
            return False, f"Load failed: {e}"


# =============================================================================
# Emulator Core
# =============================================================================

class EmulatorCore:
    def __init__(self) -> None:
        self.vi = VideoInterface(FB_WIDTH_DEFAULT, FB_HEIGHT_DEFAULT)
        self.bus = MemoryBus(self.vi)
        self.cop0 = COP0()
        self.cpu = CPU(self.bus, self.cop0)

        self.video: Optional[VideoPlugin] = DefaultTkVideoPlugin()
        self.audio = AudioPlugin()
        self.input = InputPlugin()
        self.rsp = RSPPlugin()

        self.save_states = SaveStates(self.cpu, self.bus, self.vi)

        self.running = False
        self.paused = False
        self.rom_loaded = False
        self.current_fps = 0.0
        self.rom_info: dict[str, str] = {}

        self._state_lock = threading.Lock()
        self._emu_thread: Optional[threading.Thread] = None
        self._stop_flag = False

    def load_rom(self, path: str) -> bool:
        try:
            if not os.path.isfile(path):
                log_bus.error(f"ROM not found: {path}")
                return False
                
            valid_ext = ['.z64', '.n64', '.v64']
            if not any(path.lower().endswith(e) for e in valid_ext):
                log_bus.error("Invalid ROM extension")
                return False

            file_size = os.path.getsize(path)
            if file_size > CART_SIZE_MAX:
                log_bus.warning(f"ROM will be truncated to {CART_SIZE_MAX} bytes")

            with open(path, "rb") as f:
                raw = f.read(CART_SIZE_MAX)

            if len(raw) < 4:
                log_bus.error("ROM too small")
                return False

            # Pad to 4KB minimum and 4-byte align
            if len(raw) < 4096:
                raw = raw + bytes(4096 - len(raw))
            pad = (4 - (len(raw) % 4)) % 4
            if pad:
                raw = raw + bytes(pad)

            # Detect and convert endianness
            magic = (raw[0] << 24) | (raw[1] << 16) | (raw[2] << 8) | raw[3]
            
            if magic == 0x80371240:
                fixed = raw
            elif magic == 0x37804012:  # .v64
                fixed = bytearray(len(raw))
                for i in range(0, len(raw) - 1, 2):
                    fixed[i] = raw[i + 1]
                    fixed[i + 1] = raw[i]
                fixed = bytes(fixed)
            elif magic == 0x40123780:  # .n64
                fixed = bytearray(len(raw))
                for i in range(0, len(raw) - 3, 4):
                    fixed[i] = raw[i + 3]
                    fixed[i + 1] = raw[i + 2]
                    fixed[i + 2] = raw[i + 1]
                    fixed[i + 3] = raw[i]
                fixed = bytes(fixed)
            else:
                fixed = raw

            self.bus.load_cart(fixed)
            
            with self._state_lock:
                self.rom_loaded = True
                self.rom_info = {"name": Path(path).stem, "path": path}
            
            # Clear framebuffer for clean start
            self.vi.clear(0xFF000000)
            
            log_bus.info(f"Loaded ROM: {path} ({len(fixed)} bytes)")
            return True
            
        except Exception as e:
            log_bus.error(f"ROM load error: {e}")
            return False

    def start(self) -> None:
        with self._state_lock:
            if self.running:
                return
            self.running = True
            self.paused = False
            self._stop_flag = False
            
            # Clear VI and reset CPU state
            self.vi.clear(0xFF000000)
            self.cpu._break_hit = False
            self.cpu.load_demo_program(self.bus)

        if self._emu_thread is None or not self._emu_thread.is_alive():
            self._emu_thread = threading.Thread(target=self._run_loop, daemon=True, name="CatN64EMU")
            self._emu_thread.start()
        log_cpu.info("Emulation started.")

    def _run_loop(self) -> None:
        frame_start = time.perf_counter()
        fps_timer = frame_start
        frames = 0
        
        while True:
            # Check stop flag
            with self._state_lock:
                if not self.running or self._stop_flag:
                    break
                paused = self.paused

            if not paused and self.cpu.running and not self.cpu._break_hit:
                # Run a batch of cycles
                self.cpu.run_cycles(50_000)
                frames += 1
            else:
                time.sleep(0.005)

            # Frame timing
            now = time.perf_counter()
            elapsed = now - frame_start
            
            # Maintain ~60fps timing
            if elapsed < FRAME_TIME_SEC:
                sleep_time = FRAME_TIME_SEC - elapsed
                if sleep_time > 0.001:
                    time.sleep(sleep_time * 0.9)
            
            frame_start = time.perf_counter()

            # FPS calculation every 500ms
            fps_elapsed = now - fps_timer
            if fps_elapsed >= 0.5:
                self.current_fps = frames / fps_elapsed
                frames = 0
                fps_timer = now

        log_cpu.info("Core loop terminated.")

    def stop(self) -> None:
        with self._state_lock:
            self.running = False
            self._stop_flag = True
            self.cpu.running = False
        if self._emu_thread and self._emu_thread.is_alive():
            self._emu_thread.join(timeout=1.0)
        log_cpu.info("Emulation stopped.")

    def pause(self) -> None:
        with self._state_lock:
            self.paused = not self.paused
        log_cpu.info("Paused." if self.paused else "Resumed.")


# =============================================================================
# Debugger UI
# =============================================================================

class DebuggerWindow(tk.Toplevel):
    def __init__(self, root: tk.Tk, emulator: EmulatorCore):
        super().__init__(root)
        self.title(f"{APP_NAME} Debugger")
        self.geometry("680x520")
        self.emu = emulator
        self._build()

    def _build(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True)

        self.reg_frame = ttk.Frame(nb)
        nb.add(self.reg_frame, text="Registers")
        self._build_regs(self.reg_frame)

        self.dis_frame = ttk.Frame(nb)
        nb.add(self.dis_frame, text="Disasm")
        self._build_disasm(self.dis_frame)

        self.mem_frame = ttk.Frame(nb)
        nb.add(self.mem_frame, text="Memory")
        self._build_mem(self.mem_frame)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill=tk.X)
        ttk.Button(ctrl, text="Step", command=self._do_step).pack(side=tk.LEFT, padx=4, pady=4)
        ttk.Button(ctrl, text="Run", command=self._do_run).pack(side=tk.LEFT, padx=4, pady=4)
        ttk.Button(ctrl, text="Pause", command=self._do_pause).pack(side=tk.LEFT, padx=4, pady=4)

        ttk.Label(ctrl, text="BP @").pack(side=tk.LEFT, padx=(16, 4))
        self.bp_entry = ttk.Entry(ctrl, width=10)
        self.bp_entry.pack(side=tk.LEFT)
        ttk.Button(ctrl, text="+", command=self._add_bp, width=2).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl, text="-", command=self._del_bp, width=2).pack(side=tk.LEFT, padx=2)

        self.after(200, self._refresh)

    def _build_regs(self, frame: ttk.Frame) -> None:
        self.reg_tree = ttk.Treeview(frame, columns=("reg", "value"), show="headings", height=18)
        self.reg_tree.heading("reg", text="Reg")
        self.reg_tree.heading("value", text="Value")
        self.reg_tree.column("reg", width=60)
        self.reg_tree.column("value", width=100)
        self.reg_tree.pack(fill=tk.BOTH, expand=True)

    def _build_disasm(self, frame: ttk.Frame) -> None:
        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="PC:").pack(side=tk.LEFT)
        self.pc_entry = ttk.Entry(top, width=10)
        self.pc_entry.pack(side=tk.LEFT)
        ttk.Button(top, text="Go", command=self._go_pc).pack(side=tk.LEFT, padx=4)
        self.dis_list = tk.Listbox(frame, font=("Courier", 9))
        self.dis_list.pack(fill=tk.BOTH, expand=True)

    def _build_mem(self, frame: ttk.Frame) -> None:
        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Addr:").pack(side=tk.LEFT)
        self.mem_entry = ttk.Entry(top, width=10)
        self.mem_entry.pack(side=tk.LEFT)
        ttk.Button(top, text="Peek", command=self._peek32).pack(side=tk.LEFT, padx=4)
        self.mem_out = tk.Text(frame, height=10, font=("Courier", 9))
        self.mem_out.pack(fill=tk.BOTH, expand=True)

    def _refresh(self) -> None:
        self.reg_tree.delete(*self.reg_tree.get_children())
        names = ["$zero","$at","$v0","$v1","$a0","$a1","$a2","$a3",
                 "$t0","$t1","$t2","$t3","$t4","$t5","$t6","$t7",
                 "$s0","$s1","$s2","$s3","$s4","$s5","$s6","$s7",
                 "$t8","$t9","$k0","$k1","$gp","$sp","$fp","$ra"]
        for i, n in enumerate(names):
            self.reg_tree.insert("", "end", values=(n, f"{self.emu.cpu.reg[i]:08X}"))
        self.reg_tree.insert("", "end", values=("PC", f"{self.emu.cpu.pc:08X}"))

        self.dis_list.delete(0, tk.END)
        pc = self.emu.cpu.pc
        for i in range(-4, 12):
            addr = u32(pc + i * 4)
            word = self.emu.bus.read32(addr)
            marker = ">>>" if addr == pc else "   "
            self.dis_list.insert(tk.END, f"{marker} {addr:08X}: {word:08X}")

        self.after(200, self._refresh)

    def _do_step(self) -> None:
        self.emu.paused = True
        if self.emu.cpu.running:
            self.emu.cpu.step()

    def _do_run(self) -> None:
        self.emu.paused = False
        self.emu.cpu.running = True

    def _do_pause(self) -> None:
        self.emu.paused = True
        self.emu.cpu.running = False

    def _add_bp(self) -> None:
        try:
            self.emu.cpu.set_breakpoint(int(self.bp_entry.get(), 16))
        except: pass

    def _del_bp(self) -> None:
        try:
            self.emu.cpu.clear_breakpoint(int(self.bp_entry.get(), 16))
        except: pass

    def _go_pc(self) -> None:
        try:
            self.emu.cpu.pc = int(self.pc_entry.get(), 16) & 0xFFFFFFFF
        except: pass

    def _peek32(self) -> None:
        try:
            v = int(self.mem_entry.get(), 16) & 0xFFFFFFFF
            word = self.emu.bus.read32(v)
            self.mem_out.insert(tk.END, f"{v:08X} = {word:08X}\n")
            self.mem_out.see(tk.END)
        except: pass


# =============================================================================
# GUI
# =============================================================================

class CatN64GUI:
    def __init__(self, root: tk.Tk, emulator: EmulatorCore) -> None:
        self.root = root
        self.emulator = emulator
        self._update_job: Optional[str] = None
        self._alive = True
        self._config = self._load_config()
        self.debugger: Optional[DebuggerWindow] = None

        self.setup_window()
        self.create_menu()
        self.create_toolbar()
        self.create_display()
        self.create_statusbar()
        self.bind_events()
        self.start_update_loop()

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text())
            except:
                return {}
        return {}

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self._config, indent=2))
        except: pass

    def setup_window(self) -> None:
        self.root.title(f"{APP_TITLE} - Project64 Style")
        w = self._config.get("window_w", 800)
        h = self._config.get("window_h", 600)
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(640, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)

    def create_menu(self) -> None:
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open ROM...", command=self.open_rom, accelerator="Ctrl+O")
        file_menu.add_command(label="Load Demo", command=self.load_demo)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_exit)
        menubar.add_cascade(label="File", menu=file_menu)

        system_menu = tk.Menu(menubar, tearoff=0)
        system_menu.add_command(label="Start", command=self.start_emulation, accelerator="F5")
        system_menu.add_command(label="Stop", command=self.stop_emulation, accelerator="F6")
        system_menu.add_command(label="Pause", command=self.pause_emulation, accelerator="F7")
        system_menu.add_separator()
        system_menu.add_command(label="Save State", command=self.save_state, accelerator="F9")
        system_menu.add_command(label="Load State", command=self.load_state, accelerator="F10")
        menubar.add_cascade(label="System", menu=system_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Debugger", command=self.open_debugger, accelerator="F8")
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

    def create_toolbar(self) -> None:
        toolbar = ttk.Frame(self.root, relief=tk.RAISED, borderwidth=1)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        for text, cmd in [("Open", self.open_rom), ("Start", self.start_emulation),
                          ("Stop", self.stop_emulation), ("Pause", self.pause_emulation),
                          ("Save", self.save_state), ("Load", self.load_state),
                          ("Debug", self.open_debugger)]:
            ttk.Button(toolbar, text=text, command=cmd).pack(side=tk.LEFT, padx=2, pady=2)

    def create_display(self) -> None:
        display_frame = ttk.Frame(self.root)
        display_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.canvas = tk.Canvas(display_frame, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        fps_frame = tk.Frame(display_frame, bg="black")
        fps_frame.place(relx=0.02, rely=0.02)
        self.fps_var = tk.StringVar(value="FPS: 0")
        self.fps_label = tk.Label(fps_frame, textvariable=self.fps_var,
                                   fg="#00FF00", bg="black", font=("Courier", 10, "bold"))
        self.fps_label.pack()

        self.display_width = self.emulator.vi.width
        self.display_height = self.emulator.vi.height
        self.photo = tk.PhotoImage(width=self.display_width, height=self.display_height)
        self.canvas_image = self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)
        self.canvas.bind("<Configure>", self.on_canvas_resize)

    def on_canvas_resize(self, event) -> None:
        x = (event.width - self.display_width) // 2
        y = (event.height - self.display_height) // 2
        self.canvas.coords(self.canvas_image, x, y)

    def create_statusbar(self) -> None:
        statusbar = ttk.Frame(self.root, relief=tk.SUNKEN)
        statusbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(statusbar, textvariable=self.status_var, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.rom_var = tk.StringVar(value="No ROM")
        ttk.Label(statusbar, textvariable=self.rom_var, width=25).pack(side=tk.RIGHT)

    def bind_events(self) -> None:
        self.root.bind("<Control-o>", lambda e: self.open_rom())
        self.root.bind("<F5>", lambda e: self.start_emulation())
        self.root.bind("<F6>", lambda e: self.stop_emulation())
        self.root.bind("<F7>", lambda e: self.pause_emulation())
        self.root.bind("<F8>", lambda e: self.open_debugger())
        self.root.bind("<F9>", lambda e: self.save_state())
        self.root.bind("<F10>", lambda e: self.load_state())

    def start_update_loop(self) -> None:
        self.update_loop()

    def update_loop(self) -> None:
        try:
            emu = self.emulator
            if emu.running and not emu.paused:
                if emu.video and emu.vi.dirty:
                    if emu.video.update(self.photo, emu.vi.fb, emu.vi.width, emu.vi.height, True):
                        emu.vi.dirty = False
                        self.canvas.itemconfig(self.canvas_image, image=self.photo)

            self.fps_var.set(f"FPS: {emu.current_fps:5.1f}")
            self.fps_label.configure(fg=colorize_fps(emu.current_fps))
        finally:
            if self._alive:
                self._update_job = self.root.after(16, self.update_loop)

    def open_rom(self) -> None:
        path = filedialog.askopenfilename(
            title="Open N64 ROM",
            filetypes=[("N64 ROMs", "*.z64 *.n64 *.v64"), ("All", "*.*")]
        )
        if not path:
            return
        if self.emulator.load_rom(path):
            self.status_var.set("ROM loaded")
            self.rom_var.set(Path(path).name)
            self.root.title(f"{APP_TITLE} - {Path(path).name}")
            self._config["last_rom"] = path
            self._save_config()
        else:
            messagebox.showerror("Error", "Failed to load ROM")

    def load_demo(self) -> None:
        self.emulator.vi.clear(0xFF000000)
        self.emulator.cpu.load_demo_program(self.emulator.bus)
        self.status_var.set("Demo loaded")
        self.rom_var.set("DEMO")

    def start_emulation(self) -> None:
        self.emulator.start()
        self.status_var.set("Running")

    def stop_emulation(self) -> None:
        self.emulator.stop()
        self.status_var.set("Stopped")

    def pause_emulation(self) -> None:
        self.emulator.pause()
        self.status_var.set("Paused" if self.emulator.paused else "Running")

    def save_state(self) -> None:
        ok, msg = self.emulator.save_states.save_state()
        self.status_var.set(msg)
        if not ok:
            messagebox.showerror("Save State", msg)

    def load_state(self) -> None:
        ok, msg = self.emulator.save_states.load_state()
        self.status_var.set(msg)
        if not ok:
            messagebox.showwarning("Load State", msg)

    def open_debugger(self) -> None:
        if self.debugger is None or not self.debugger.winfo_exists():
            self.debugger = DebuggerWindow(self.root, self.emulator)
        else:
            self.debugger.lift()

    def show_about(self) -> None:
        messagebox.showinfo("About", f"{APP_NAME} {APP_VERSION}\n© 2025 FlamesCo & Samsoft")

    def on_exit(self) -> None:
        self._alive = False
        if self._update_job:
            try:
                self.root.after_cancel(self._update_job)
            except: pass
        try:
            self._config["window_w"] = self.root.winfo_width()
            self._config["window_h"] = self.root.winfo_height()
            self._save_config()
        except: pass
        self.emulator.stop()
        self.root.destroy()


# =============================================================================
# Entry Point
# =============================================================================

def main() -> None:
    root = tk.Tk()
    CatN64GUI(root, EmulatorCore())
    root.mainloop()


if __name__ == "__main__":
    main()
