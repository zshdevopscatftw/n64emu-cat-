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
import random
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
APP_VERSION: str = "1.X"  # keep generic per request
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

# Memory map (simplified for test harness)
RDRAM_BASE = 0x00000000
RDRAM_SIZE = 0x04000000  # 64 MB window (oversized for convenience)

VI_BASE = 0x04000000
VI_WINDOW_SIZE = 0x01000000  # 16 MB window (we only use a small portion)

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
        log_bus.error(f"be_store32 out of bounds: off={off}, len={len(mem)}")
        return
    mem[off + 0] = (val >> 24) & 0xFF
    mem[off + 1] = (val >> 16) & 0xFF
    mem[off + 2] = (val >> 8) & 0xFF
    mem[off + 3] = (val >> 0) & 0xFF


def be_load32(mem: bytearray, off: int) -> int:
    """Safely load 32-bit big-endian value with bounds check."""
    if off < 0 or off + 4 > len(mem):
        log_bus.error(f"be_load32 out of bounds: off={off}, len={len(mem)}")
        return 0
    return ((mem[off + 0] << 24) |
            (mem[off + 1] << 16) |
            (mem[off + 2] << 8) |
            (mem[off + 3] << 0))


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
        """Return True if redraw was performed."""
        raise NotImplementedError


class AudioPlugin:
    def push_samples(self, samples):  # placeholder
        pass


class InputPlugin:
    def translate_key(self, keysym, pressed: bool):
        pass


class RSPPlugin:
    def step(self, cycles: int):
        pass


# Default Tkinter video plugin (ARGB32 -> PhotoImage)
class DefaultTkVideoPlugin(VideoPlugin):
    def __init__(self) -> None:
        self._last_frame_id = 0

    def update(self, photo: tk.PhotoImage, fb: bytearray, w: int, h: int, dirty: bool) -> bool:
        if not dirty:
            return False
        # Validate buffer size
        expected_size = w * h * 4
        if len(fb) < expected_size:
            log_vi.error(f"Framebuffer too small: {len(fb)} < {expected_size}")
            return False
        # Convert ARGB32 -> #RRGGBB rows; do one string push
        # (Slow for very large frames, but OK for 320x240 test)
        rows = []
        idx = 0
        try:
            for _ in range(h):
                parts = []
                for _ in range(w):
                    # Layout: A R G B
                    r = fb[idx + 1]
                    g = fb[idx + 2]
                    b = fb[idx + 3]
                    parts.append(f"#{r:02x}{g:02x}{b:02x}")
                    idx += 4
                rows.append("{" + " ".join(parts) + "}")
            data = " ".join(rows)
            photo.put(data, to=(0, 0))
            self._last_frame_id += 1
            return True
        except IndexError as e:
            log_vi.error(f"PhotoImage update index error: {e}")
            return False
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
        self.size = self.pitch * self.height  # bytes
        self.fb = bytearray(self.size)  # ARGB32
        self.dirty = True

    def _in_fb_bounds(self, off: int) -> bool:
        return 0 <= off <= self.size - 4

    def write32(self, paddr: int, value: int) -> bool:
        """Map 0x04000000.. to framebuffer"""
        if not (VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE):
            return False
        off = paddr - VI_BASE  # framebuffer base equals VI_BASE
        if self._in_fb_bounds(off):
            # ARGB32 big-endian store into fb
            self.fb[off + 0] = (value >> 24) & 0xFF  # A
            self.fb[off + 1] = (value >> 16) & 0xFF  # R
            self.fb[off + 2] = (value >> 8) & 0xFF   # G
            self.fb[off + 3] = (value >> 0) & 0xFF   # B
            self.dirty = True
            return True
        # Outside framebuffer, treat as VI regs (ignored for now)
        return True

    def read32(self, paddr: int) -> int:
        if not (VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE):
            return 0
        off = paddr - VI_BASE
        if self._in_fb_bounds(off):
            a = self.fb[off + 0]
            r = self.fb[off + 1]
            g = self.fb[off + 2]
            b = self.fb[off + 3]
            return (a << 24) | (r << 16) | (g << 8) | b
        return 0

    def clear(self, argb: int = 0xFF000000) -> None:
        # Fill framebuffer with color
        a = (argb >> 24) & 0xFF
        r = (argb >> 16) & 0xFF
        g = (argb >> 8) & 0xFF
        b = (argb >> 0) & 0xFF
        fb = self.fb  # local for speed
        for i in range(0, self.size, 4):
            fb[i + 0] = a
            fb[i + 1] = r
            fb[i + 2] = g
            fb[i + 3] = b
        self.dirty = True


# =============================================================================
# COP0 (very stubby, enough to carry exceptions)
# =============================================================================

class COP0:
    def __init__(self) -> None:
        # Only a few regs for the harness
        self.Status = 0x0
        self.Cause = 0x0
        self.EPC = 0x0

    def raise_exception(self, cause_code: int, epc: int) -> None:
        self.Cause = cause_code & 0xFF
        self.EPC = epc
        log_cop0.warning(f"Exception cause={self.Cause:#x} EPC={self.EPC:#x}")


# =============================================================================
# TLB stub / address translation
# =============================================================================

class TLB:
    @staticmethod
    def vaddr_to_paddr(vaddr: int) -> Optional[int]:
        """Simplified KSEG mapping + identity (for demo)"""
        vaddr &= 0xFFFFFFFF
        # KSEG0: 0x8000_0000 - 0x9FFF_FFFF -> cached => phys low 512MB window
        if 0x80000000 <= vaddr <= 0x9FFFFFFF:
            return vaddr & 0x1FFFFFFF
        # KSEG1: 0xA000_0000 - 0xBFFF_FFFF -> uncached
        if 0xA0000000 <= vaddr <= 0xBFFFFFFF:
            return vaddr & 0x1FFFFFFF
        # KUSEG: identity for the harness (log)
        if vaddr < 0x80000000:
            log_bus.debug(f"TLB: KUSEG identity map vaddr={vaddr:#x}")
            return vaddr
        # Others unmapped
        log_bus.error(f"TLB: unmapped vaddr={vaddr:#x}")
        return None


# =============================================================================
# Memory Bus
# =============================================================================

class MemoryBus:
    def __init__(self, vi: VideoInterface) -> None:
        self.rdram = bytearray(RDRAM_SIZE)  # big scratch
        self.vi = vi
        self.cart = bytearray()            # loaded ROM (big-endian assumed)
        self.cart_size = 0                 # actual cart size for bounds checking
        self.access_log = deque(maxlen=256)  # (type, addr)

    def load_cart(self, data: bytes) -> None:
        # Clamp to max size and pad to 4-byte alignment
        clamped = data[:CART_SIZE_MAX]
        pad_len = (4 - (len(clamped) % 4)) % 4
        if pad_len:
            clamped = clamped + bytes(pad_len)
        self.cart = bytearray(clamped)
        self.cart_size = len(self.cart)
        log_bus.info(f"Cartridge loaded: {self.cart_size} bytes")

    # --- Physical 32-bit access (word aligned) ---
    def read32_p(self, paddr: int) -> int:
        if paddr & 3:
            log_bus.error(f"Unaligned read32 @ {paddr:#x}")
            return 0
        # RDRAM window
        if RDRAM_BASE <= paddr < RDRAM_BASE + RDRAM_SIZE:
            off = paddr - RDRAM_BASE
            if off + 4 <= RDRAM_SIZE:
                return be_load32(self.rdram, off)
            return 0
        # VI window
        if VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE:
            return self.vi.read32(paddr)
        # Cart window (read-only) with proper bounds checking
        if CART_BASE <= paddr < CART_BASE + CART_SIZE_MAX:
            off = paddr - CART_BASE
            # Check bounds against actual cart size
            if self.cart_size > 0 and off + 4 <= self.cart_size:
                # Cart is big-endian
                return ((self.cart[off + 0] << 24) |
                        (self.cart[off + 1] << 16) |
                        (self.cart[off + 2] << 8) |
                        (self.cart[off + 3] << 0))
            else:
                # Out of bounds or no cart - return 0 (open bus)
                return 0
        self.access_log.append(('R', paddr))
        return 0

    def write32_p(self, paddr: int, value: int) -> None:
        value = u32(value)
        if paddr & 3:
            log_bus.error(f"Unaligned write32 @ {paddr:#x}")
            return
        # RDRAM
        if RDRAM_BASE <= paddr < RDRAM_BASE + RDRAM_SIZE:
            off = paddr - RDRAM_BASE
            if off + 4 <= RDRAM_SIZE:
                be_store32(self.rdram, off, value)
            return
        # VI framebuffer / regs
        if VI_BASE <= paddr < VI_BASE + VI_WINDOW_SIZE:
            self.vi.write32(paddr, value)
            return
        # Cart is read-only in this harness; ignore writes but log
        if CART_BASE <= paddr < CART_BASE + CART_SIZE_MAX:
            log_bus.debug(f"Write to cartridge ignored @ {paddr:#x}")
            return
        self.access_log.append(('W', paddr))

    # --- Virtual access (through TLB stub) ---
    def read32(self, vaddr: int) -> int:
        paddr = TLB.vaddr_to_paddr(vaddr)
        if paddr is None:
            log_bus.error(f"read32 unmapped vaddr={vaddr:#x}")
            return 0
        return self.read32_p(paddr)

    def write32(self, vaddr: int, value: int) -> None:
        paddr = TLB.vaddr_to_paddr(vaddr)
        if paddr is None:
            log_bus.error(f"write32 unmapped vaddr={vaddr:#x} val={value:#x}")
            return
        self.write32_p(paddr, value)


# =============================================================================
# CPU (VR4300-like, very partial)
# =============================================================================

class CPU:
    def __init__(self, bus: MemoryBus, cop0: COP0) -> None:
        self.bus = bus
        self.cop0 = cop0
        self.reg = [0] * 32  # 32 regs (32-bit for harness)
        self.hi = 0
        self.lo = 0
        self.pc = 0x80000000  # start in KSEG0
        self.running = False
        self._break_hit = False

        # Breakpoints
        self.breakpoints: set[int] = set()

    def reset(self, pc: int = 0x80000000) -> None:
        self.reg = [0] * 32
        self.hi = self.lo = 0
        self.pc = pc & 0xFFFFFFFF
        self.running = False
        self._break_hit = False

    def set_breakpoint(self, addr: int) -> None:
        self.breakpoints.add(addr & 0xFFFFFFFF)

    def clear_breakpoint(self, addr: int) -> None:
        self.breakpoints.discard(addr & 0xFFFFFFFF)

    def fetch32(self, addr: int) -> int:
        return self.bus.read32(addr)

    def step(self, allow_delay_slot: bool = True) -> int:
        """Execute one instruction; returns 'cycles' (1 for harness)"""
        if self.pc in self.breakpoints:
            log_cpu.info(f"Hit breakpoint @ {self.pc:#010x}")
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

        def set_reg(i: int, val: int) -> None:
            if i != 0:
                self.reg[i] = u32(val)

        if op == 0x00:
            # SPECIAL
            if instr == 0x00000000:
                # NOP (SLL r0, r0, 0)
                pass
            elif fn == 0x21:  # ADDU rd, rs, rt
                set_reg(rd, self.reg[rs] + self.reg[rt])
            elif fn == 0x00:  # SLL rd, rt, sa
                set_reg(rd, self.reg[rt] << sa)
            elif fn == 0x08:  # JR rs
                target = self.reg[rs]
                branch_taken = True
            elif fn == 0x09:  # JALR rd, rs
                set_reg(rd, next_pc)
                target = self.reg[rs]
                branch_taken = True
            elif fn == 0x0D:  # BREAK
                self.cop0.raise_exception(0x9, pc)  # arbitrary cause code
                self._break_hit = True
                self.running = False
            else:
                log_cpu.warning(f"Unimpl SPECIAL fn={fn:#x} @ {pc:#x}")
        elif op == 0x02:      # J
            target = (next_pc & 0xF0000000) | (jidx << 2)
            branch_taken = True
        elif op == 0x03:      # JAL
            set_reg(31, next_pc)
            target = (next_pc & 0xF0000000) | (jidx << 2)
            branch_taken = True
        elif op == 0x05:      # BNE rs, rt, offset
            if self.reg[rs] != self.reg[rt]:
                target = u32(next_pc + (simm << 2))
                branch_taken = True
        elif op == 0x09:      # ADDIU rt, rs, imm
            set_reg(rt, self.reg[rs] + simm)
        elif op == 0x0D:      # ORI rt, rs, imm (zero-extended)
            set_reg(rt, self.reg[rs] | (imm & 0xFFFF))
        elif op == 0x0F:      # LUI rt, imm
            set_reg(rt, (imm << 16) & 0xFFFFFFFF)
        elif op == 0x23:      # LW rt, offset(rs)
            addr = u32(self.reg[rs] + simm)
            if addr & 3:
                self.cop0.raise_exception(0x4, pc)  # AdEL
            else:
                set_reg(rt, self.bus.read32(addr))
        elif op == 0x2B:      # SW rt, offset(rs)
            addr = u32(self.reg[rs] + simm)
            if addr & 3:
                self.cop0.raise_exception(0x5, pc)  # AdES
            else:
                self.bus.write32(addr, self.reg[rt])
        else:
            log_cpu.warning(f"Unimpl opcode={op:#x} @ {pc:#x}")

        # Branch delay slot: execute next_pc before taking the branch
        if branch_taken:
            if allow_delay_slot:
                # Execute delay-slot instruction (without recursive delay-slot)
                self.pc = next_pc
                self.step(allow_delay_slot=False)
            self.pc = u32(target if target is not None else next_pc)
        else:
            self.pc = next_pc

        # Enforce r0 = 0
        self.reg[0] = 0
        return 1

    # --- Demo program loader (MIPS code fills the VI framebuffer) ---
    def load_demo_program(self, bus: MemoryBus) -> None:
        """
        A tiny MIPS program:
        - r1 = 0x04000000 (VI framebuffer base)
        - r2 = WIDTH*HEIGHT (pixel count)
        - r3 = pointer
        - r4 = color = 0xFF000000 (opaque black)
        loop:
            sw    r4, 0(r3)
            addiu r3, r3, 4
            addiu r4, r4, 0x0101
            addiu r2, r2, -1
            bne   r2, r0, loop
            nop
        break
        """
        prog = [
            0x3C010400,       # lui  r1, 0x0400
            0x34210000,       # ori  r1, r1, 0x0000
            0x3C020001,       # lui  r2, 0x0001
            0x34422C00,       # ori  r2, r2, 0x2C00   ; 320*240=76800 -> 0x12C00
            0x00201821,       # addu r3, r1, r0
            0x3C04FF00,       # lui  r4, 0xFF00       ; A=0xFF, RGB start 0
            0x34840000,       # ori  r4, r4, 0x0000
            0xAC640000,       # sw   r4, 0(r3)
            0x24630004,       # addiu r3, r3, 4
            0x24840101,       # addiu r4, r4, 0x0101  ; gentle gradient
            0x2442FFFF,       # addiu r2, r2, -1
            0x1440FFFB,       # bne  r2, r0, loop
            0x00000000,       # nop
            0x0000000D,       # break
        ]
        # Place at RDRAM physical 0x0000_0000; execute from 0x8000_0000 (KSEG0)
        off = 0
        for w in prog:
            be_store32(bus.rdram, off, w)
            off += 4
        self.reset(pc=0x80000000)
        self.running = True
        log_cpu.info("Demo MIPS program loaded (fills VI framebuffer).")


# =============================================================================
# Save States (lightweight placeholders)
# =============================================================================

class SaveStates:
    def __init__(self, cpu: CPU, bus: MemoryBus, vi: VideoInterface) -> None:
        self.cpu = cpu
        self.bus = bus
        self.vi = vi

    def _encode_fb(self) -> str:
        """Base64-encode framebuffer to embed into JSON safely."""
        if not self.vi.fb:
            return ""
        return base64.b64encode(bytes(self.vi.fb)).decode("ascii")

    def _decode_fb(self, text: str) -> bytes:
        if not text:
            return b""
        try:
            return base64.b64decode(text.encode("ascii"))
        except Exception as e:
            log_vi.error(f"Failed to decode framebuffer from savestate: {e}")
            return b""

    def save_state(self) -> tuple[bool, str]:
        # Minimal (registers + pc only) to keep file small; include VI buffer for visual continuity
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
                    "fb_width": self.vi.width,
                    "fb_height": self.vi.height,
                }
            }
            Path("states").mkdir(exist_ok=True)
            name = "states/quick.sav"
            with open(name, "w", encoding="utf-8") as f:
                json.dump(state, f)
            return True, f"State saved → {name}"
        except Exception as e:
            return False, f"Save failed: {e}"

    def load_state(self) -> tuple[bool, str]:
        try:
            name = "states/quick.sav"
            with open(name, "r", encoding="utf-8") as f:
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
    """N64 harness core for UI testing + basic CPU/MEM/VI simulation"""
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

    # --- ROM handling ---
    def load_rom(self, path: str) -> bool:
        try:
            if not os.path.isfile(path):
                log_bus.error(f"ROM not found: {path}")
                return False
            valid_extensions = ['.z64', '.n64', '.v64']
            if not any(path.lower().endswith(ext) for ext in valid_extensions):
                log_bus.error("Invalid ROM extension (expected .z64/.n64/.v64)")
                return False
            
            # Read file with size limit check
            file_size = os.path.getsize(path)
            if file_size > CART_SIZE_MAX:
                log_bus.warning(f"ROM larger than max ({file_size} > {CART_SIZE_MAX}), will be truncated")
            
            with open(path, "rb") as f:
                raw = f.read(CART_SIZE_MAX)  # Limit read to max cart size

            # Minimum ROM size check (N64 header is 64 bytes, need at least 4 for magic)
            MIN_ROM_SIZE = 4096  # 4KB minimum for any practical ROM
            if len(raw) < 4:
                log_bus.error(f"ROM too small: {len(raw)} bytes (need at least 4 for header)")
                return False
            
            if len(raw) < MIN_ROM_SIZE:
                log_bus.warning(f"ROM unusually small: {len(raw)} bytes, padding to {MIN_ROM_SIZE}")
                raw = raw + bytes(MIN_ROM_SIZE - len(raw))

            # Pad to 4-byte alignment for safe word access
            pad_len = (4 - (len(raw) % 4)) % 4
            if pad_len:
                raw = raw + bytes(pad_len)

            # Heuristic endianness fixup to big-endian .z64 form
            # N64 header magic (big): 0x80371240
            fixed: bytes
            b0, b1, b2, b3 = raw[0], raw[1], raw[2], raw[3]
            magic = (b0 << 24) | (b1 << 16) | (b2 << 8) | b3
            
            if magic == 0x80371240:
                fixed = raw  # .z64 (big-endian, native)
                log_bus.info("ROM format: .z64 (big-endian)")
            elif magic == 0x37804012:
                # .v64 (word-swapped / byte-swapped pairs)
                log_bus.info("ROM format: .v64 (byte-swapped), converting...")
                fixed_b = bytearray(len(raw))
                # Process pairs, safely
                for i in range(0, len(raw) - 1, 2):
                    fixed_b[i] = raw[i + 1]
                    fixed_b[i + 1] = raw[i]
                # Handle last byte if somehow odd (shouldn't happen after padding)
                if len(raw) % 2:
                    fixed_b[-1] = raw[-1]
                fixed = bytes(fixed_b)
            elif magic == 0x40123780:
                # .n64 (little-endian), swap 32-bit words
                log_bus.info("ROM format: .n64 (little-endian), converting...")
                fixed_b = bytearray(len(raw))
                # Process 4-byte chunks safely
                for i in range(0, len(raw) - 3, 4):
                    fixed_b[i + 0] = raw[i + 3]
                    fixed_b[i + 1] = raw[i + 2]
                    fixed_b[i + 2] = raw[i + 1]
                    fixed_b[i + 3] = raw[i + 0]
                # Copy any remaining bytes (shouldn't happen after padding)
                rem = len(raw) % 4
                if rem:
                    for j in range(rem):
                        fixed_b[-(rem - j)] = raw[-(rem - j)]
                fixed = bytes(fixed_b)
            else:
                log_bus.warning(f"Unknown ROM format magic: {magic:#010x}, loading as-is")
                fixed = raw

            self.bus.load_cart(fixed)
            with self._state_lock:
                self.rom_loaded = True
                self.rom_info = {"name": Path(path).stem, "path": path, "size": len(fixed)}
            log_bus.info(f"Loaded ROM: {path} ({len(fixed)} bytes)")
            return True
        except MemoryError:
            log_bus.error("ROM too large - out of memory")
            return False
        except PermissionError:
            log_bus.error(f"Permission denied reading ROM: {path}")
            return False
        except IOError as e:
            log_bus.error(f"IO error reading ROM: {e}")
            return False
        except Exception as e:
            log_bus.error(f"ROM load error: {e}")
            return False

    # --- Run control ---
    def start(self) -> None:
        with self._state_lock:
            if self.running:
                return
            self.running = True
            self.paused = False
            self._stop_flag = False
            # If no ROM, load the demo program into RDRAM and run from 0x8000_0000
            # For demo harness we still start at the RDRAM demo even if a cart is loaded.
            self.cpu.load_demo_program(self.bus)

        if self._emu_thread is None or not self._emu_thread.is_alive():
            self._emu_thread = threading.Thread(target=self._run_loop, daemon=True, name="CatN64EMUCore")
            self._emu_thread.start()
        log_cpu.info("Emulation started.")

    def _run_loop(self) -> None:
        last = time.time()
        frame_count = 0
        while True:
            with self._state_lock:
                if not self.running or self._stop_flag:
                    break
                paused = self.paused

            if not paused and self.cpu.running and not self.cpu._break_hit:
                # Execute a budget of instructions per frame slice
                budget = 25_000  # very small for UI snappiness
                executed = 0
                while executed < budget and self.cpu.running:
                    self.cpu.step()
                    executed += 1
            else:
                time.sleep(0.01)

            now = time.time()
            dt = now - last
            if dt >= 0.25:
                fps = frame_count / dt if dt > 0 else 0.0
                self.current_fps = fps
                last = now
                frame_count = 0
            else:
                frame_count += 1

        log_cpu.info("Core loop terminated.")

    def stop(self) -> None:
        with self._state_lock:
            self.running = False
            self._stop_flag = True
            self.cpu.running = False
        if self._emu_thread and self._emu_thread.is_alive():
            try:
                self._emu_thread.join(timeout=1.0)
            except Exception:
                pass
        log_cpu.info("Emulation stopped.")

    def pause(self) -> None:
        with self._state_lock:
            self.paused = not self.paused
        log_cpu.info("Emulation paused." if self.paused else "Emulation resumed.")


# =============================================================================
# Debugger UI (very light)
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

        # Registers
        self.reg_frame = ttk.Frame(nb)
        nb.add(self.reg_frame, text="Registers")
        self._build_regs(self.reg_frame)

        # Disasm
        self.dis_frame = ttk.Frame(nb)
        nb.add(self.dis_frame, text="Disasm")
        self._build_disasm(self.dis_frame)

        # Memory
        self.mem_frame = ttk.Frame(nb)
        nb.add(self.mem_frame, text="Memory")
        self._build_mem(self.mem_frame)

        # Controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill=tk.X)
        ttk.Button(ctrl, text="Step", command=self._do_step).pack(side=tk.LEFT, padx=4, pady=4)
        ttk.Button(ctrl, text="Run", command=self._do_run).pack(side=tk.LEFT, padx=4, pady=4)
        ttk.Button(ctrl, text="Pause/Break", command=self._do_pause).pack(side=tk.LEFT, padx=4, pady=4)

        ttk.Label(ctrl, text="Breakpoint @ hex addr:").pack(side=tk.LEFT, padx=(16, 4))
        self.bp_entry = ttk.Entry(ctrl, width=12)
        self.bp_entry.pack(side=tk.LEFT)
        ttk.Button(ctrl, text="Add", command=self._add_bp).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl, text="Del", command=self._del_bp).pack(side=tk.LEFT, padx=4)

        self.after(200, self._refresh)

    def _build_regs(self, frame: ttk.Frame) -> None:
        cols = ("reg", "value")
        self.reg_tree = ttk.Treeview(frame, columns=cols, show="headings", height=18)
        for c in cols:
            self.reg_tree.heading(c, text=c)
        self.reg_tree.pack(fill=tk.BOTH, expand=True)

    def _build_disasm(self, frame: ttk.Frame) -> None:
        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="PC (hex):").pack(side=tk.LEFT)
        self.pc_entry = ttk.Entry(top, width=12)
        self.pc_entry.pack(side=tk.LEFT)
        ttk.Button(top, text="Go", command=self._go_pc).pack(side=tk.LEFT, padx=4)

        self.dis_list = tk.Listbox(frame)
        self.dis_list.pack(fill=tk.BOTH, expand=True)

    def _build_mem(self, frame: ttk.Frame) -> None:
        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Address (hex):").pack(side=tk.LEFT)
        self.mem_entry = ttk.Entry(top, width=12)
        self.mem_entry.pack(side=tk.LEFT)
        ttk.Button(top, text="Peek32", command=self._peek32).pack(side=tk.LEFT, padx=4)
        self.mem_out = tk.Text(frame, height=10)
        self.mem_out.pack(fill=tk.BOTH, expand=True)

    def _refresh(self) -> None:
        # Registers
        self.reg_tree.delete(*self.reg_tree.get_children())
        reg = self.emu.cpu.reg
        names = [
            "$r0", "$at", "$v0", "$v1", "$a0", "$a1", "$a2", "$a3",
            "$t0", "$t1", "$t2", "$t3", "$t4", "$t5", "$t6", "$t7",
            "$s0", "$s1", "$s2", "$s3", "$s4", "$s5", "$s6", "$s7",
            "$t8", "$t9", "$k0", "$k1", "$gp", "$sp", "$fp", "$ra"
        ]
        for i, n in enumerate(names):
            self.reg_tree.insert("", "end", values=(n, f"{reg[i]:08X}"))
        self.reg_tree.insert("", "end", values=("PC", f"{self.emu.cpu.pc:08X}"))
        self.reg_tree.insert("", "end", values=("HI", f"{self.emu.cpu.hi:08X}"))
        self.reg_tree.insert("", "end", values=("LO", f"{self.emu.cpu.lo:08X}"))

        # Disasm around PC
        self.dis_list.delete(0, tk.END)
        pc = self.emu.cpu.pc
        base = (pc - 16) & 0xFFFFFFFF
        for i in range(16):
            addr = (base + i * 4) & 0xFFFFFFFF
            word = self.emu.bus.read32(addr)
            self.dis_list.insert(tk.END, f"{addr:08X}: {self._dis_word(word, addr)}")

        self.after(250, self._refresh)

    def _dis_word(self, instr: int, addr: int) -> str:
        op = (instr >> 26) & 0x3F
        rs = (instr >> 21) & 0x1F
        rt = (instr >> 16) & 0x1F
        rd = (instr >> 11) & 0x1F
        sa = (instr >> 6) & 0x1F
        fn = instr & 0x3F
        imm = instr & 0xFFFF
        simm = sign16(imm)
        jidx = instr & 0x03FFFFFF
        if instr == 0x00000000:
            return "nop"
        if op == 0x00:
            if fn == 0x21:
                return f"addu ${rd},{rs},{rt}"
            if fn == 0x00:
                return f"sll ${rd},{rt},{sa}"
            if fn == 0x08:
                return f"jr ${rs}"
            if fn == 0x09:
                return f"jalr ${rd},{rs}"
            if fn == 0x0D:
                return "break"
            return f"special fn={fn:#x}"
        if op == 0x02:
            target = ((addr + 4) & 0xF0000000) | (jidx << 2)
            return f"j 0x{target:08X}"
        if op == 0x03:
            target = ((addr + 4) & 0xF0000000) | (jidx << 2)
            return f"jal 0x{target:08X}"
        if op == 0x05:
            return f"bne ${rs},${rt},{simm}"
        if op == 0x09:
            return f"addiu ${rt},${rs},{simm}"
        if op == 0x0D:
            return f"ori ${rt},${rs},{imm:#x}"
        if op == 0x0F:
            return f"lui ${rt},{imm:#x}"
        if op == 0x23:
            return f"lw ${rt},{simm}(${rs})"
        if op == 0x2B:
            return f"sw ${rt},{simm}(${rs})"
        return f"op={op:#x}"

    def _do_step(self) -> None:
        # Pause core, single-step the CPU
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
            val = int(self.bp_entry.get(), 16) & 0xFFFFFFFF
            self.emu.cpu.set_breakpoint(val)
        except Exception:
            pass

    def _del_bp(self) -> None:
        try:
            val = int(self.bp_entry.get(), 16) & 0xFFFFFFFF
            self.emu.cpu.clear_breakpoint(val)
        except Exception:
            pass

    def _go_pc(self) -> None:
        try:
            val = int(self.pc_entry.get(), 16) & 0xFFFFFFFF
            self.emu.cpu.pc = val
        except Exception:
            pass

    def _peek32(self) -> None:
        try:
            v = int(self.mem_entry.get(), 16) & 0xFFFFFFFF
            word = self.emu.bus.read32(v)
            self.mem_out.insert(tk.END, f"{v:08X} -> {word:08X}\n")
            self.mem_out.see(tk.END)
        except Exception:
            pass


# =============================================================================
# GUI
# =============================================================================

class CatN64GUI:
    """Project64-like UI with harness backend"""
    def __init__(self, root: tk.Tk, emulator: EmulatorCore) -> None:
        self.root = root
        self.emulator = emulator

        self._update_job: Optional[str] = None
        self._alive = True

        self._config = self._load_config()

        self.setup_window()
        self.create_menu()
        self.create_toolbar()
        self.create_display()
        self.create_statusbar()
        self.bind_events()

        self.debugger: Optional[DebuggerWindow] = None
        self.start_update_loop()

    # --- Config ---
    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text())
            except Exception:
                return {}
        return {}

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self._config, indent=2))
        except Exception:
            pass

    # --- UI ---
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

        # File
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open ROM...", command=self.open_rom, accelerator="Ctrl+O")
        file_menu.add_command(label="Load Demo (Gradient)", command=self.load_demo)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_exit, accelerator="Alt+F4")
        menubar.add_cascade(label="File", menu=file_menu)

        # System
        system_menu = tk.Menu(menubar, tearoff=0)
        system_menu.add_command(label="Start", command=self.start_emulation, accelerator="F5")
        system_menu.add_command(label="Stop", command=self.stop_emulation, accelerator="F6")
        system_menu.add_command(label="Pause", command=self.pause_emulation, accelerator="F7")
        system_menu.add_separator()
        system_menu.add_command(label="Save State", command=self.save_state, accelerator="F9")
        system_menu.add_command(label="Load State", command=self.load_state, accelerator="F10")
        menubar.add_cascade(label="System", menu=system_menu)

        # Tools
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Debugger", command=self.open_debugger, accelerator="F8")
        menubar.add_cascade(label="Tools", menu=tools_menu)

        # Help
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

    def create_toolbar(self) -> None:
        toolbar = ttk.Frame(self.root, relief=tk.RAISED, borderwidth=1)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        buttons = [
            ("Open", self.open_rom),
            ("Start", self.start_emulation),
            ("Stop", self.stop_emulation),
            ("Pause", self.pause_emulation),
            ("Save", self.save_state),
            ("Load", self.load_state),
            ("Debug", self.open_debugger),
        ]
        for text, command in buttons:
            btn = ttk.Button(toolbar, text=text, command=command)
            btn.pack(side=tk.LEFT, padx=2, pady=2)

    def create_display(self) -> None:
        display_frame = ttk.Frame(self.root)
        display_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.canvas = tk.Canvas(display_frame, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        fps_frame = tk.Frame(display_frame, bg="black")
        fps_frame.place(relx=0.02, rely=0.02)
        self.fps_var = tk.StringVar(value="FPS: 0")
        self.fps_label = tk.Label(
            fps_frame,
            textvariable=self.fps_var,
            fg="#00FF00",
            bg="black",
            font=("Courier", 10, "bold"),
        )
        self.fps_label.pack()

        self.display_width, self.display_height = self.emulator.vi.width, self.emulator.vi.height
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
        self.rom_var = tk.StringVar(value="No ROM loaded")
        ttk.Label(statusbar, textvariable=self.rom_var, width=30).pack(side=tk.RIGHT)

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

    # --- Update loop: blit framebuffer, show FPS ---
    def update_loop(self) -> None:
        try:
            emu = self.emulator
            if emu.running and not emu.paused:
                # Video blit only if VI is dirty
                if emu.video:
                    redrawn = emu.video.update(self.photo, emu.vi.fb, emu.vi.width, emu.vi.height, emu.vi.dirty)
                    if redrawn:
                        emu.vi.dirty = False
                        self.canvas.itemconfig(self.canvas_image, image=self.photo)
                        self.canvas.update_idletasks()

            fps = getattr(emu, "current_fps", 0.0)
            self.fps_var.set(f"FPS: {fps:4.1f}")
            self.fps_label.configure(fg=colorize_fps(fps))
        finally:
            if self._alive:
                self._update_job = self.root.after(16, self.update_loop)

    # --- Commands ---
    def open_rom(self) -> None:
        path = filedialog.askopenfilename(
            title="Open N64 ROM",
            filetypes=[("N64 ROMs", "*.z64 *.n64 *.v64"), ("All Files", "*.*")]
        )
        if not path:
            return
        if self.emulator.load_rom(path):
            self.status_var.set("ROM loaded successfully")
            self.rom_var.set(Path(path).name)
            self.root.title(f"{APP_TITLE} - {Path(path).name}")
            self._config["last_rom"] = path
            self._save_config()
        else:
            messagebox.showerror("Open ROM", "Failed to load ROM.")

    def load_demo(self) -> None:
        self.emulator.cpu.load_demo_program(self.emulator.bus)
        self.status_var.set("Demo program loaded (CPU will fill VI).")
        self.rom_var.set("DEMO")
        self.root.title(f"{APP_TITLE} - DEMO")

    def start_emulation(self) -> None:
        self.emulator.start()
        self.status_var.set("Emulation started")

    def stop_emulation(self) -> None:
        self.emulator.stop()
        self.status_var.set("Emulation stopped")

    def pause_emulation(self) -> None:
        self.emulator.pause()
        status = "Paused" if getattr(self.emulator, "paused", False) else "Resumed"
        self.status_var.set(status)

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
        messagebox.showinfo(
            "About",
            f"{APP_NAME} {APP_VERSION} (Harness)\n© 2025 FlamesCo & Samsoft",
        )

    def on_exit(self) -> None:
        self._alive = False
        if self._update_job is not None:
            try:
                self.root.after_cancel(self._update_job)
            except Exception:
                pass
            self._update_job = None
        # Save last window size
        try:
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            self._config["window_w"] = w
            self._config["window_h"] = h
            self._save_config()
        except Exception:
            pass
        self.emulator.stop()
        self.root.destroy()


# =============================================================================
# Entry Point
# =============================================================================

def main() -> None:
    root = tk.Tk()
    app = CatN64GUI(root, EmulatorCore())
    root.mainloop()


if __name__ == "__main__":
    main()
