"""
hw_monitor.py -- vendor-neutral hardware monitor + headroom controller.

Built for the SC Toolbox Companion (see ARCHITECTURE.md, "Machine tiers"). The
controller decides tiers on MEASURED headroom, never on RAM labels or vendor
SDKs, and must never make Star Citizen's own frame time worse.

Sampling reads from the SAME source Task Manager reads: Windows Performance
Counters via pdh.dll (ctypes), plus kernel32 (GlobalMemoryStatusEx,
Toolhelp32 process enumeration) and the registry (adapter VRAM size). Python
3.10, stdlib only -- no psutil, no nvidia-smi, no vendor SDKs.

Every Sample field is either a real reading or None with a reason string.
Nothing is ever fabricated as a default.

Public API:
    Sample                  -- dataclass, one point-in-time reading
    HeadroomState            -- enum: TIGHT / OK / ROOMY
    HeadroomController        -- deterministic hysteresis state machine
    sample_all(...)          -- take one real Sample on this machine
    find_pid_by_name(name)   -- pid lookup via Toolhelp32, or None
    aggregate_gpu_engine(items) -- Task-Manager-equivalent GPU % aggregation
    read_vram_total_bytes()  -- registry VRAM size lookup
    PdhError                 -- raised (and caught internally) on PDH faults

CLI:
    python hw_monitor.py --selftest   controller + parser tests, fake clock
    python hw_monitor.py --sample     one real live Sample, every field
"""

from __future__ import annotations

import ctypes
import dataclasses
import enum
import re
import sys
import time
import winreg
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

# =============================================================================
# PART 0 -- shared Win32 handles
# =============================================================================

pdh = ctypes.WinDLL("pdh.dll")
kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)


# =============================================================================
# PART 1a -- PDH (Performance Data Helper) ctypes bindings
# =============================================================================

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000

# PDH_STATUS values we know how to name (pdhmsg.h). Restype is DWORD
# (unsigned) throughout so these compare directly with no sign games.
PDH_MORE_DATA = 0x800007D2

PDH_STATUS_NAMES = {
    0x00000000: "PDH_CSTATUS_VALID_DATA",
    0x00000001: "PDH_CSTATUS_NEW_DATA",
    0x800007D0: "PDH_CSTATUS_NO_MACHINE",
    0x800007D1: "PDH_CSTATUS_NO_INSTANCE",
    0x800007D2: "PDH_MORE_DATA",
    0x800007D3: "PDH_CSTATUS_ITEM_NOT_VALIDATED",
    0x800007D4: "PDH_RETRY",
    0x800007D5: "PDH_NO_DATA",
    0x800007D6: "PDH_CALC_NEGATIVE_DENOMINATOR",
    0x800007D7: "PDH_CALC_NEGATIVE_TIMEBASE",
    0x800007D8: "PDH_CALC_NEGATIVE_VALUE",
    0x800007D9: "PDH_DIALOG_CANCELLED",
    0x800007DA: "PDH_END_OF_LOG_FILE",
    0x800007DB: "PDH_ASYNC_QUERY_TIMEOUT",
    0xC0000BB8: "PDH_CSTATUS_NO_OBJECT",
    0xC0000BB9: "PDH_CSTATUS_NO_COUNTER",
    0xC0000BBA: "PDH_CSTATUS_INVALID_DATA",
    0xC0000BBB: "PDH_MEMORY_ALLOCATION_FAILURE",
    0xC0000BBC: "PDH_INVALID_HANDLE",
    0xC0000BBD: "PDH_INVALID_ARGUMENT",
    0xC0000BBE: "PDH_FUNCTION_NOT_FOUND",
    0xC0000BBF: "PDH_CSTATUS_NO_COUNTERNAME",
    0xC0000BC0: "PDH_CSTATUS_BAD_COUNTERNAME",
    0xC0000BC1: "PDH_INVALID_BUFFER",
    0xC0000BC2: "PDH_INSUFFICIENT_BUFFER",
    0xC0000BC3: "PDH_CANNOT_CONNECT_MACHINE",
    0xC0000BC6: "PDH_INVALID_PATH",
    0xC0000BC7: "PDH_INVALID_INSTANCE",
    0xC0000BC8: "PDH_INVALID_DATA",
}


def _pdh_status_text(code: int) -> str:
    code &= 0xFFFFFFFF
    return f"{PDH_STATUS_NAMES.get(code, 'UNKNOWN_PDH_STATUS')} (0x{code:08X})"


class PdhError(Exception):
    """Raised on any non-success PDH_STATUS. Carries the raw code so callers
    can turn it into a field's `reason` string verbatim."""

    def __init__(self, code: int, where: str):
        self.code = code & 0xFFFFFFFF
        self.where = where
        super().__init__(f"{where}: {_pdh_status_text(self.code)}")


PDH_HQUERY = ctypes.c_void_p
PDH_HCOUNTER = ctypes.c_void_p


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    # Real type is a struct { DWORD CStatus; union { LONG; double; ... }; }.
    # Declaring CStatus then doubleValue as plain sequential fields gives the
    # identical memory layout (ctypes pads doubleValue to its natural 8-byte
    # alignment, exactly where the union starts in the real C struct) as
    # long as we only ever read the double member, which is all we use.
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", PDH_FMT_COUNTERVALUE),
    ]


pdh.PdhOpenQueryW.restype = wintypes.DWORD
pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PDH_HQUERY)]

pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
pdh.PdhAddEnglishCounterW.argtypes = [PDH_HQUERY, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PDH_HCOUNTER)]

pdh.PdhCollectQueryData.restype = wintypes.DWORD
pdh.PdhCollectQueryData.argtypes = [PDH_HQUERY]

pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD
pdh.PdhGetFormattedCounterValue.argtypes = [
    PDH_HCOUNTER,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(PDH_FMT_COUNTERVALUE),
]

pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
pdh.PdhGetFormattedCounterArrayW.argtypes = [
    PDH_HCOUNTER,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]

pdh.PdhCloseQuery.restype = wintypes.DWORD
pdh.PdhCloseQuery.argtypes = [PDH_HQUERY]


class PdhQuery:
    """Thin RAII-ish wrapper around one PDH query handle with N counters."""

    def __init__(self):
        self._h = PDH_HQUERY()
        status = pdh.PdhOpenQueryW(None, None, ctypes.byref(self._h))
        if status != 0:
            raise PdhError(status, "PdhOpenQueryW")

    def add_counter(self, path: str) -> PDH_HCOUNTER:
        hc = PDH_HCOUNTER()
        status = pdh.PdhAddEnglishCounterW(self._h, path, None, ctypes.byref(hc))
        if status != 0:
            raise PdhError(status, f"PdhAddEnglishCounterW({path!r})")
        return hc

    def collect(self) -> None:
        status = pdh.PdhCollectQueryData(self._h)
        if status != 0:
            raise PdhError(status, "PdhCollectQueryData")

    def get_value(self, hcounter: PDH_HCOUNTER) -> float:
        val = PDH_FMT_COUNTERVALUE()
        status = pdh.PdhGetFormattedCounterValue(hcounter, PDH_FMT_DOUBLE, None, ctypes.byref(val))
        if status != 0:
            raise PdhError(status, "PdhGetFormattedCounterValue")
        return val.doubleValue

    def get_array(self, hcounter: PDH_HCOUNTER) -> List[Tuple[str, float, int]]:
        """Returns [(instance_name, value, CStatus), ...]. Entries whose
        CStatus != 0 carry value 0.0 -- caller decides whether to skip them."""
        buf_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(
            hcounter,
            PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
            ctypes.byref(buf_size),
            ctypes.byref(item_count),
            None,
        )
        if status != 0 and status != PDH_MORE_DATA:
            raise PdhError(status, "PdhGetFormattedCounterArrayW(size)")
        if item_count.value == 0 or buf_size.value == 0:
            return []

        buf = ctypes.create_string_buffer(buf_size.value)
        status = pdh.PdhGetFormattedCounterArrayW(
            hcounter,
            PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
            ctypes.byref(buf_size),
            ctypes.byref(item_count),
            buf,
        )
        if status != 0:
            raise PdhError(status, "PdhGetFormattedCounterArrayW(fill)")

        items_arr = ctypes.cast(
            buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W * item_count.value)
        ).contents
        out: List[Tuple[str, float, int]] = []
        for item in items_arr:
            name = item.szName if item.szName else ""
            cstatus = item.FmtValue.CStatus
            value = item.FmtValue.doubleValue if cstatus == 0 else 0.0
            out.append((name, value, cstatus))
        return out

    def close(self) -> None:
        if self._h:
            pdh.PdhCloseQuery(self._h)
            self._h = PDH_HQUERY()


# =============================================================================
# PART 1b -- kernel32: RAM (GlobalMemoryStatusEx) + process enumeration
# =============================================================================


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]


def _sample_ram() -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Returns (total_bytes, avail_bytes, reason). reason is None on success."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ok = kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    if not ok:
        err = ctypes.get_last_error()
        return None, None, f"GlobalMemoryStatusEx failed, GetLastError={err}"
    return stat.ullTotalPhys, stat.ullAvailPhys, None


TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def find_pid_by_name(process_name: str) -> Optional[int]:
    """Case-insensitive exact match on the exe filename (e.g. "StarCitizen.exe").
    Returns the first matching pid, or None if not running / snapshot failed."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == -1:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        target = process_name.lower()
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                return entry.th32ProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        kernel32.CloseHandle(snap)


# =============================================================================
# PART 1c -- registry: adapter VRAM size
# =============================================================================

_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = rf"SYSTEM\ControlSet001\Control\Class\{_DISPLAY_CLASS_GUID}"


def read_vram_total_bytes() -> Tuple[Optional[int], Optional[str]]:
    """Reads HardwareInformation.qwMemorySize from every display-adapter
    subkey under the display class GUID and returns the LARGEST value found
    (the discrete GPU, on machines that also have an iGPU). Never guesses:
    returns (None, reason) if the key or value can't be read."""
    try:
        class_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_PATH)
    except OSError as e:
        return None, f"registry class key not found: {e}"

    largest: Optional[int] = None
    try:
        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(class_key, i)
            except OSError:
                break
            i += 1
            if not re.fullmatch(r"\d{4}", subkey_name):
                continue
            try:
                sub = winreg.OpenKey(class_key, subkey_name)
            except OSError:
                continue
            try:
                try:
                    value, regtype = winreg.QueryValueEx(sub, "HardwareInformation.qwMemorySize")
                except FileNotFoundError:
                    continue
                size: Optional[int] = None
                if regtype == winreg.REG_QWORD:
                    size = int(value)
                elif regtype == winreg.REG_BINARY and len(value) >= 8:
                    size = int.from_bytes(value[:8], "little")
                else:
                    try:
                        size = int(value)
                    except (TypeError, ValueError):
                        size = None
                if size is not None and (largest is None or size > largest):
                    largest = size
            finally:
                sub.Close()
    finally:
        class_key.Close()

    if largest is None:
        return None, "no adapter subkey exposed a readable HardwareInformation.qwMemorySize"
    return largest, None


# =============================================================================
# PART 1d -- GPU Engine instance-name parsing / aggregation
# =============================================================================

# e.g. "pid_5312_luid_0x00000000_0x0000C3A1_phys_0_eng_2_engtype_3D"
_GPU_ENGINE_RE = re.compile(
    r"pid_(?P<pid>\d+)_luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+_phys_(?P<phys>\d+)_eng_(?P<eng>\d+)_engtype_(?P<engtype>.+)$"
)
# e.g. "pid_5312_luid_0x00000000_0x0000C3A1_phys_0" (GPU Process/Adapter Memory)
_GPU_MEM_PID_RE = re.compile(r"pid_(?P<pid>\d+)_luid_")


def aggregate_gpu_engine(
    items: List[Tuple[str, float, int]]
) -> Tuple[float, Dict[str, float], Dict[int, Dict[str, float]]]:
    """Reproduces Task Manager's headline GPU % from raw
    \\GPU Engine(*)\\Utilization Percentage instances:
        for each engine type, SUM utilization across every instance (every
        pid/phys/eng) of that type; the headline number is the MAX across
        engine types.

    Returns (overall_pct, per_engtype_totals, per_pid_engtype) where
    per_pid_engtype[pid][engtype] is that process's own summed contribution
    to that engine type (used for the per-process GPU share)."""
    per_engtype_total: Dict[str, float] = {}
    per_pid_engtype: Dict[int, Dict[str, float]] = {}

    for name, value, cstatus in items:
        if cstatus != 0:
            continue
        m = _GPU_ENGINE_RE.search(name)
        if not m:
            continue
        pid = int(m.group("pid"))
        engtype = m.group("engtype")
        per_engtype_total[engtype] = per_engtype_total.get(engtype, 0.0) + value
        per_pid_engtype.setdefault(pid, {})
        per_pid_engtype[pid][engtype] = per_pid_engtype[pid].get(engtype, 0.0) + value

    overall = max(per_engtype_total.values()) if per_engtype_total else 0.0
    return overall, per_engtype_total, per_pid_engtype


# =============================================================================
# PART 1e -- Sample dataclass + orchestration
# =============================================================================


@dataclasses.dataclass
class Sample:
    timestamp: float

    cpu_total_pct: Optional[float]
    cpu_counter_used: Optional[str]  # which counter path actually worked
    cpu_reason: Optional[str]

    ram_total_bytes: Optional[int]
    ram_avail_bytes: Optional[int]
    ram_reason: Optional[str]

    gpu_total_pct: Optional[float]  # Task-Manager-equivalent headline %
    gpu_per_engtype_pct: Optional[Dict[str, float]]
    gpu_reason: Optional[str]

    sc_pid: Optional[int]
    sc_gpu_pct: Optional[float]  # sum across engine types for that pid
    sc_gpu_per_engtype_pct: Optional[Dict[str, float]]
    sc_gpu_reason: Optional[str]

    vram_total_bytes: Optional[int]
    vram_total_reason: Optional[str]

    vram_used_bytes: Optional[int]  # heuristic "primary adapter" -- see note
    vram_used_per_adapter: Optional[Dict[str, int]]
    vram_used_reason: Optional[str]

    sc_vram_used_bytes: Optional[int]
    sc_vram_used_reason: Optional[str]

    @property
    def ram_avail_gb(self) -> Optional[float]:
        return None if self.ram_avail_bytes is None else self.ram_avail_bytes / (1024**3)

    @property
    def vram_free_bytes(self) -> Optional[int]:
        if self.vram_total_bytes is None or self.vram_used_bytes is None:
            return None
        return max(self.vram_total_bytes - self.vram_used_bytes, 0)

    @property
    def vram_free_gb(self) -> Optional[float]:
        b = self.vram_free_bytes
        return None if b is None else b / (1024**3)


# CPU counter candidates, tried in order. Processor Information/% Processor
# Utility is what Task Manager itself uses on Win8+ (normalized against
# nominal clock, accounts for turbo); % Processor Time is the universal
# fallback if that object isn't present.
_CPU_COUNTER_CANDIDATES = [
    r"\Processor Information(_Total)\% Processor Utility",
    r"\Processor(_Total)\% Processor Time",
]

_GPU_ENGINE_PATH = r"\GPU Engine(*)\Utilization Percentage"
_GPU_ADAPTER_MEM_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"
_GPU_PROCESS_MEM_PATH = r"\GPU Process Memory(*)\Dedicated Usage"


def sample_all(interval: float = 1.0, sc_process_name: str = "StarCitizen.exe") -> Sample:
    """Takes one real Sample on this machine. `interval` is the gap between
    the two PdhCollectQueryData calls the rate counters (CPU %, GPU %) need
    before their first valid value -- Task Manager itself refreshes on
    ~1s, so that is the default."""
    ts = time.time()

    ram_total, ram_avail, ram_reason = _sample_ram()
    vram_total, vram_total_reason = read_vram_total_bytes()
    try:
        sc_pid = find_pid_by_name(sc_process_name)
    except Exception as e:  # pragma: no cover -- defensive only
        sc_pid = None

    cpu_pct: Optional[float] = None
    cpu_counter_used: Optional[str] = None
    cpu_reason: Optional[str] = None

    gpu_total_pct: Optional[float] = None
    gpu_per_engtype: Optional[Dict[str, float]] = None
    gpu_reason: Optional[str] = None

    sc_gpu_pct: Optional[float] = None
    sc_gpu_per_engtype: Optional[Dict[str, float]] = None
    sc_gpu_reason: Optional[str] = None

    vram_used_total: Optional[int] = None
    vram_used_per_adapter: Optional[Dict[str, int]] = None
    vram_used_reason: Optional[str] = None

    sc_vram_used: Optional[int] = None
    sc_vram_used_reason: Optional[str] = None

    try:
        q = PdhQuery()
    except PdhError as e:
        reason = str(e)
        return Sample(
            timestamp=ts,
            cpu_total_pct=None, cpu_counter_used=None, cpu_reason=reason,
            ram_total_bytes=ram_total, ram_avail_bytes=ram_avail, ram_reason=ram_reason,
            gpu_total_pct=None, gpu_per_engtype_pct=None, gpu_reason=reason,
            sc_pid=sc_pid, sc_gpu_pct=None, sc_gpu_per_engtype_pct=None, sc_gpu_reason=reason,
            vram_total_bytes=vram_total, vram_total_reason=vram_total_reason,
            vram_used_bytes=None, vram_used_per_adapter=None, vram_used_reason=reason,
            sc_vram_used_bytes=None, sc_vram_used_reason=reason,
        )

    h_cpu = None
    h_gpu_engine = None
    h_gpu_adapter_mem = None
    h_gpu_process_mem = None

    try:
        # --- add counters, independently: a missing one never blocks the rest
        cpu_add_errors = []
        for path in _CPU_COUNTER_CANDIDATES:
            try:
                h_cpu = q.add_counter(path)
                cpu_counter_used = path
                break
            except PdhError as e:
                cpu_add_errors.append(str(e))
        if h_cpu is None:
            cpu_reason = "; ".join(cpu_add_errors) or "no CPU counter candidate available"

        try:
            h_gpu_engine = q.add_counter(_GPU_ENGINE_PATH)
        except PdhError as e:
            gpu_reason = str(e)
            sc_gpu_reason = str(e)

        try:
            h_gpu_adapter_mem = q.add_counter(_GPU_ADAPTER_MEM_PATH)
        except PdhError as e:
            vram_used_reason = str(e)

        try:
            h_gpu_process_mem = q.add_counter(_GPU_PROCESS_MEM_PATH)
        except PdhError as e:
            sc_vram_used_reason = str(e)

        # --- two collections, one interval apart: rate counters have no
        # valid value until the second sample.
        try:
            q.collect()
            time.sleep(interval)
            q.collect()
        except PdhError as e:
            err = str(e)
            if h_cpu is not None:
                cpu_reason = err
                h_cpu = None
            if h_gpu_engine is not None:
                gpu_reason = err
                sc_gpu_reason = err
                h_gpu_engine = None
            if h_gpu_adapter_mem is not None:
                vram_used_reason = err
                h_gpu_adapter_mem = None
            if h_gpu_process_mem is not None:
                sc_vram_used_reason = err
                h_gpu_process_mem = None

        # --- CPU
        if h_cpu is not None:
            try:
                cpu_pct = q.get_value(h_cpu)
            except PdhError as e:
                cpu_reason = str(e)

        # --- GPU engine (headline % + per-process share)
        if h_gpu_engine is not None:
            try:
                items = q.get_array(h_gpu_engine)
                overall, per_engtype, per_pid_engtype = aggregate_gpu_engine(items)
                gpu_total_pct = overall
                gpu_per_engtype = per_engtype
                if sc_pid is not None:
                    pid_engines = per_pid_engtype.get(sc_pid, {})
                    sc_gpu_pct = sum(pid_engines.values())
                    sc_gpu_per_engtype = pid_engines
                else:
                    sc_gpu_reason = f"{sc_process_name} not running"
            except PdhError as e:
                gpu_reason = str(e)
                sc_gpu_reason = str(e)

        # --- GPU adapter memory (VRAM used)
        if h_gpu_adapter_mem is not None:
            try:
                items = q.get_array(h_gpu_adapter_mem)
                per_adapter: Dict[str, int] = {}
                for name, value, cstatus in items:
                    if cstatus != 0:
                        continue
                    per_adapter[name] = int(value)
                if per_adapter:
                    vram_used_per_adapter = per_adapter
                    # Heuristic: the busiest/largest-usage adapter instance
                    # stands in for "the GPU" (matches the largest-adapter
                    # heuristic used for vram_total). Exact on single-GPU
                    # machines (e.g. this one: RTX 4070, no iGPU); on a
                    # multi-adapter machine, use vram_used_per_adapter and
                    # correlate by LUID yourself.
                    vram_used_total = max(per_adapter.values())
                else:
                    vram_used_reason = "no adapter memory instances returned"
            except PdhError as e:
                vram_used_reason = str(e)

        # --- GPU process memory (VRAM used by SC specifically)
        if h_gpu_process_mem is not None:
            if sc_pid is not None:
                try:
                    items = q.get_array(h_gpu_process_mem)
                    total = 0
                    found = False
                    for name, value, cstatus in items:
                        if cstatus != 0:
                            continue
                        m = _GPU_MEM_PID_RE.search(name)
                        if not m:
                            continue
                        if int(m.group("pid")) == sc_pid:
                            total += int(value)
                            found = True
                    sc_vram_used = total if found else 0
                except PdhError as e:
                    sc_vram_used_reason = str(e)
            else:
                sc_vram_used_reason = f"{sc_process_name} not running"
    finally:
        q.close()

    return Sample(
        timestamp=ts,
        cpu_total_pct=cpu_pct, cpu_counter_used=cpu_counter_used, cpu_reason=cpu_reason,
        ram_total_bytes=ram_total, ram_avail_bytes=ram_avail, ram_reason=ram_reason,
        gpu_total_pct=gpu_total_pct, gpu_per_engtype_pct=gpu_per_engtype, gpu_reason=gpu_reason,
        sc_pid=sc_pid, sc_gpu_pct=sc_gpu_pct, sc_gpu_per_engtype_pct=sc_gpu_per_engtype, sc_gpu_reason=sc_gpu_reason,
        vram_total_bytes=vram_total, vram_total_reason=vram_total_reason,
        vram_used_bytes=vram_used_total, vram_used_per_adapter=vram_used_per_adapter, vram_used_reason=vram_used_reason,
        sc_vram_used_bytes=sc_vram_used, sc_vram_used_reason=sc_vram_used_reason,
    )


# =============================================================================
# PART 2 -- HeadroomController (hysteresis state machine)
# =============================================================================
#
# All thresholds are tunable constants, gathered here so the "agreed
# hysteresis" in ARCHITECTURE.md's "Machine tiers" section lives in one
# place. Units: RAM/VRAM floors in GB, GPU/VRAM-share ceilings in percent.
#
# ENTER a higher state only after its condition holds for ENTER_SUSTAIN_S
# seconds; EXIT to a lower (non-emergency) state after its condition holds
# for EXIT_SUSTAIN_S seconds (EXIT_SUSTAIN_S < ENTER_SUSTAIN_S -- come down
# faster than you go up). EMERGENCY_EXIT to TIGHT is immediate, no sustain.

# --- promotion floors/ceilings (TIGHT -> OK, OK -> ROOMY) ------------------
MIN_FREE_RAM_GB_FOR_OK = 4.0
MIN_FREE_RAM_GB_FOR_ROOMY = 8.0
MAX_GPU_LOAD_PCT_FOR_OK = 70.0
MAX_GPU_LOAD_PCT_FOR_ROOMY = 50.0
MIN_VRAM_FREE_GB_FOR_OK = 1.5
MIN_VRAM_FREE_GB_FOR_ROOMY = 3.0
MAX_SC_GPU_SHARE_PCT_FOR_OK = 60.0
MAX_SC_GPU_SHARE_PCT_FOR_ROOMY = 40.0

# --- demotion floors/ceilings (OK -> TIGHT, ROOMY -> OK), deliberately
# looser than the matching promotion threshold so a value sitting exactly on
# the promotion line doesn't also sit on the demotion line (that gap is what
# stops fast flapping right at one number). ---------------------------------
EXIT_FREE_RAM_GB_TO_TIGHT = 3.0
EXIT_FREE_RAM_GB_TO_OK = 6.0
EXIT_GPU_LOAD_PCT_TO_TIGHT = 85.0
EXIT_GPU_LOAD_PCT_TO_OK = 65.0
EXIT_VRAM_FREE_GB_TO_TIGHT = 1.0
EXIT_VRAM_FREE_GB_TO_OK = 2.0
EXIT_SC_GPU_SHARE_PCT_TO_TIGHT = 80.0
EXIT_SC_GPU_SHARE_PCT_TO_OK = 65.0

# --- emergency (immediate, no sustain) --------------------------------------
EMERGENCY_FREE_RAM_GB_FLOOR = 1.5
EMERGENCY_GPU_LOAD_PCT_WHILE_SC = 95.0

# --- sustain windows ---------------------------------------------------------
ENTER_SUSTAIN_S = 20.0  # N
EXIT_SUSTAIN_S = 5.0  # M  (M < N)


class HeadroomState(str, enum.Enum):
    TIGHT = "TIGHT"  # only tiny/no-model work
    OK = "OK"  # small realizer allowed
    ROOMY = "ROOMY"  # bigger model / dream jobs allowed


def _free_ram_gb(sample: Sample) -> Optional[float]:
    return None if sample.ram_avail_bytes is None else sample.ram_avail_bytes / (1024**3)


def _vram_free_gb(sample: Sample) -> Optional[float]:
    if sample.vram_total_bytes is None or sample.vram_used_bytes is None:
        return None
    return max(sample.vram_total_bytes - sample.vram_used_bytes, 0) / (1024**3)


class HeadroomController:
    """Deterministic hysteresis state machine over a stream of Samples.

    Fully injected clock: every call takes `now` explicitly. No threads, no
    internal wall-clock reads -- callers (and tests) control time exactly."""

    def __init__(self, now: float, initial_state: HeadroomState = HeadroomState.TIGHT):
        self.state = initial_state
        self._promote_since: Optional[float] = None
        self._demote_since: Optional[float] = None

    # -- condition predicates -------------------------------------------------

    def _meets_thresholds(
        self, sample: Sample, ram_floor_gb: float, gpu_ceiling_pct: float,
        vram_floor_gb: float, sc_share_ceiling_pct: float,
    ) -> bool:
        """All conditions must hold, using REAL data -- missing data never
        promotes (conservative by construction)."""
        free_ram = _free_ram_gb(sample)
        if free_ram is None or free_ram < ram_floor_gb:
            return False
        if sample.gpu_total_pct is None or sample.gpu_total_pct > gpu_ceiling_pct:
            return False
        vram_free = _vram_free_gb(sample)
        if vram_free is None or vram_free < vram_floor_gb:
            return False
        if sample.sc_pid is not None:
            if sample.sc_gpu_pct is None or sample.sc_gpu_pct > sc_share_ceiling_pct:
                return False
        return True

    def _fails_any(
        self, sample: Sample, ram_floor_gb: float, gpu_ceiling_pct: float,
        vram_floor_gb: float, sc_share_ceiling_pct: float,
    ) -> bool:
        """True if ANY dimension has dropped below its exit floor / above its
        exit ceiling. Missing RAM data always fails (RAM is the one signal we
        can always read via GlobalMemoryStatusEx; if even that is gone,
        headroom can't be verified, so treat it as not-safe). Missing
        GPU/VRAM/SC data does NOT itself force a demotion -- a transient PDH
        hiccup on one field shouldn't drop the tier."""
        free_ram = _free_ram_gb(sample)
        if free_ram is None or free_ram < ram_floor_gb:
            return True
        if sample.gpu_total_pct is not None and sample.gpu_total_pct > gpu_ceiling_pct:
            return True
        vram_free = _vram_free_gb(sample)
        if vram_free is not None and vram_free < vram_floor_gb:
            return True
        if sample.sc_pid is not None and sample.sc_gpu_pct is not None and sample.sc_gpu_pct > sc_share_ceiling_pct:
            return True
        return False

    def _qualifies_ok(self, sample: Sample) -> bool:
        return self._meets_thresholds(
            sample, MIN_FREE_RAM_GB_FOR_OK, MAX_GPU_LOAD_PCT_FOR_OK,
            MIN_VRAM_FREE_GB_FOR_OK, MAX_SC_GPU_SHARE_PCT_FOR_OK,
        )

    def _qualifies_roomy(self, sample: Sample) -> bool:
        return self._meets_thresholds(
            sample, MIN_FREE_RAM_GB_FOR_ROOMY, MAX_GPU_LOAD_PCT_FOR_ROOMY,
            MIN_VRAM_FREE_GB_FOR_ROOMY, MAX_SC_GPU_SHARE_PCT_FOR_ROOMY,
        )

    def _below_ok_floor(self, sample: Sample) -> bool:
        return self._fails_any(
            sample, EXIT_FREE_RAM_GB_TO_TIGHT, EXIT_GPU_LOAD_PCT_TO_TIGHT,
            EXIT_VRAM_FREE_GB_TO_TIGHT, EXIT_SC_GPU_SHARE_PCT_TO_TIGHT,
        )

    def _below_roomy_floor(self, sample: Sample) -> bool:
        return self._fails_any(
            sample, EXIT_FREE_RAM_GB_TO_OK, EXIT_GPU_LOAD_PCT_TO_OK,
            EXIT_VRAM_FREE_GB_TO_OK, EXIT_SC_GPU_SHARE_PCT_TO_OK,
        )

    def _is_emergency(self, sample: Sample) -> bool:
        free_ram = _free_ram_gb(sample)
        if free_ram is None:
            return True  # can't verify the one signal we can always read
        if free_ram < EMERGENCY_FREE_RAM_GB_FLOOR:
            return True
        if (
            sample.sc_pid is not None
            and sample.gpu_total_pct is not None
            and sample.gpu_total_pct >= EMERGENCY_GPU_LOAD_PCT_WHILE_SC
        ):
            return True
        return False

    # -- sustain bookkeeping ---------------------------------------------------

    def _track(self, flag: bool, now: float, since_attr: str) -> None:
        since = getattr(self, since_attr)
        if flag:
            if since is None:
                setattr(self, since_attr, now)
        else:
            setattr(self, since_attr, None)

    def _sustained(self, now: float, since_attr: str, threshold_s: float) -> bool:
        since = getattr(self, since_attr)
        return since is not None and (now - since) >= threshold_s

    # -- main entry point --------------------------------------------------------

    def update(self, sample: Sample, now: float) -> HeadroomState:
        """Advance the state machine with one new sample observed at `now`
        (any monotonically-nondecreasing clock the caller supplies -- tests
        use synthetic numbers). Returns the resulting state."""

        if self._is_emergency(sample):
            self.state = HeadroomState.TIGHT
            self._promote_since = None
            self._demote_since = None
            return self.state

        if self.state == HeadroomState.TIGHT:
            qualifies = self._qualifies_ok(sample)
            self._track(qualifies, now, "_promote_since")
            if qualifies and self._sustained(now, "_promote_since", ENTER_SUSTAIN_S):
                self.state = HeadroomState.OK
                self._promote_since = None

        elif self.state == HeadroomState.OK:
            qualifies_roomy = self._qualifies_roomy(sample)
            below_ok = self._below_ok_floor(sample)
            self._track(qualifies_roomy, now, "_promote_since")
            self._track(below_ok, now, "_demote_since")
            if qualifies_roomy and self._sustained(now, "_promote_since", ENTER_SUSTAIN_S):
                self.state = HeadroomState.ROOMY
                self._promote_since = None
                self._demote_since = None
            elif below_ok and self._sustained(now, "_demote_since", EXIT_SUSTAIN_S):
                self.state = HeadroomState.TIGHT
                self._promote_since = None
                self._demote_since = None

        elif self.state == HeadroomState.ROOMY:
            below_roomy = self._below_roomy_floor(sample)
            self._track(below_roomy, now, "_demote_since")
            if below_roomy and self._sustained(now, "_demote_since", EXIT_SUSTAIN_S):
                self.state = HeadroomState.OK
                self._demote_since = None

        return self.state


# =============================================================================
# PART 3 -- selftest (controller, fake clock, synthetic Samples) + live sample
# =============================================================================


def _mk_sample(
    ts: float = 0.0,
    ram_avail_gb: Optional[float] = 16.0,
    ram_total_gb: float = 32.0,
    gpu_pct: Optional[float] = 10.0,
    vram_total_gb: float = 12.0,
    vram_used_gb: Optional[float] = 2.0,
    sc_running: bool = False,
    sc_gpu_pct: Optional[float] = None,
) -> Sample:
    """Builds a synthetic Sample for controller tests -- no real hardware
    reads, so the tests are fast and fully deterministic."""
    return Sample(
        timestamp=ts,
        cpu_total_pct=20.0, cpu_counter_used="synthetic", cpu_reason=None,
        ram_total_bytes=int(ram_total_gb * 1024**3),
        ram_avail_bytes=None if ram_avail_gb is None else int(ram_avail_gb * 1024**3),
        ram_reason=None if ram_avail_gb is not None else "synthetic: missing",
        gpu_total_pct=gpu_pct, gpu_per_engtype_pct={"3D": gpu_pct} if gpu_pct is not None else None,
        gpu_reason=None if gpu_pct is not None else "synthetic: missing",
        sc_pid=1234 if sc_running else None,
        sc_gpu_pct=sc_gpu_pct if sc_running else None,
        sc_gpu_per_engtype_pct=None,
        sc_gpu_reason=None if sc_running else "StarCitizen.exe not running",
        vram_total_bytes=int(vram_total_gb * 1024**3),
        vram_total_reason=None,
        vram_used_bytes=None if vram_used_gb is None else int(vram_used_gb * 1024**3),
        vram_used_per_adapter=None,
        vram_used_reason=None if vram_used_gb is not None else "synthetic: missing",
        sc_vram_used_bytes=None,
        sc_vram_used_reason=None,
    )


def _case(name: str, fn) -> Tuple[str, bool, str]:
    try:
        fn()
        return name, True, ""
    except AssertionError as e:
        return name, False, str(e)
    except Exception as e:  # pragma: no cover
        return name, False, f"unexpected {type(e).__name__}: {e}"


def _test_sustained_promotes():
    ctrl = HeadroomController(now=0.0)
    roomy_sample = _mk_sample(ram_avail_gb=20.0, gpu_pct=5.0, vram_used_gb=1.0)
    # Feed qualifying samples every second for ENTER_SUSTAIN_S seconds; the
    # state should NOT change until the sustain window has fully elapsed.
    t = 0.0
    state = ctrl.state
    while t < ENTER_SUSTAIN_S:
        state = ctrl.update(roomy_sample, now=t)
        assert state == HeadroomState.TIGHT, f"promoted early at t={t}: {state}"
        t += 1.0
    state = ctrl.update(roomy_sample, now=ENTER_SUSTAIN_S)
    assert state == HeadroomState.OK, f"expected OK after {ENTER_SUSTAIN_S}s sustained, got {state}"


def _test_single_spike_does_not_promote():
    ctrl = HeadroomController(now=0.0)
    tight_sample = _mk_sample(ram_avail_gb=1.0, gpu_pct=90.0, vram_used_gb=11.5)
    qualifying_sample = _mk_sample(ram_avail_gb=20.0, gpu_pct=5.0, vram_used_gb=1.0)
    ctrl.update(tight_sample, now=0.0)
    state = ctrl.update(qualifying_sample, now=1.0)  # one single qualifying sample
    assert state == HeadroomState.TIGHT, f"single spike promoted: {state}"
    state = ctrl.update(tight_sample, now=2.0)  # reverts immediately
    assert state == HeadroomState.TIGHT
    # even after a long time, since the qualifying condition never sustained,
    # we should never have promoted along the way
    for t in range(3, int(ENTER_SUSTAIN_S) + 5):
        state = ctrl.update(tight_sample, now=float(t))
    assert state == HeadroomState.TIGHT, f"promoted without sustain: {state}"


def _test_no_flapping_on_noisy_oscillation():
    ctrl = HeadroomController(now=0.0)
    # First get comfortably into OK (sustained qualifying samples).
    roomy_sample = _mk_sample(ram_avail_gb=20.0, gpu_pct=5.0, vram_used_gb=1.0)
    t = 0.0
    for _ in range(int(ENTER_SUSTAIN_S) + 2):
        ctrl.update(roomy_sample, now=t)
        t += 1.0
    assert ctrl.state == HeadroomState.OK, f"setup failed to reach OK: {ctrl.state}"

    # Now oscillate RAM right around the OK enter/exit boundary, changing
    # every 1s -- much faster than EXIT_SUSTAIN_S. Values alternate between
    # just above the promotion floor for ROOMY-adjacent noise and just below
    # the OK exit floor, i.e. genuine boundary noise around the OK state.
    just_ok = _mk_sample(ram_avail_gb=MIN_FREE_RAM_GB_FOR_OK + 0.2, gpu_pct=10.0, vram_used_gb=2.0)
    just_below_exit = _mk_sample(ram_avail_gb=EXIT_FREE_RAM_GB_TO_TIGHT - 0.2, gpu_pct=10.0, vram_used_gb=2.0)
    transitions = 0
    prev = ctrl.state
    for i in range(60):
        s = just_ok if i % 2 == 0 else just_below_exit
        t += 1.0
        new_state = ctrl.update(s, now=t)
        if new_state != prev:
            transitions += 1
        prev = new_state
    # Oscillating every 1s never holds continuously for EXIT_SUSTAIN_S (5s),
    # so the demote timer keeps resetting and we should never actually leave
    # OK; zero transitions.
    assert transitions == 0, f"flapped {transitions} times on 1s-period noise (EXIT_SUSTAIN_S={EXIT_SUSTAIN_S})"
    assert ctrl.state == HeadroomState.OK, f"ended outside OK: {ctrl.state}"


def _test_exit_needs_sustain():
    ctrl = HeadroomController(now=0.0)
    roomy_sample = _mk_sample(ram_avail_gb=20.0, gpu_pct=5.0, vram_used_gb=1.0)
    t = 0.0
    for _ in range(int(ENTER_SUSTAIN_S) + 2):
        ctrl.update(roomy_sample, now=t)
        t += 1.0
    assert ctrl.state == HeadroomState.OK

    # Drop below the OK exit floor but for LESS than EXIT_SUSTAIN_S -- should
    # still be OK, then revert to qualifying and confirm we never left OK.
    below = _mk_sample(ram_avail_gb=EXIT_FREE_RAM_GB_TO_TIGHT - 0.5, gpu_pct=10.0, vram_used_gb=2.0)
    for _ in range(int(EXIT_SUSTAIN_S) - 1):
        t += 1.0
        state = ctrl.update(below, now=t)
        assert state == HeadroomState.OK, f"exited before sustain window elapsed at dt, state={state}"
    # revert before sustain completes
    t += 1.0
    state = ctrl.update(roomy_sample, now=t)
    assert state == HeadroomState.OK, f"unexpectedly left OK: {state}"

    # Now hold below the floor for the FULL sustain window -- must exit.
    t2 = t
    for _ in range(int(EXIT_SUSTAIN_S) + 2):
        t2 += 1.0
        state = ctrl.update(below, now=t2)
    assert state == HeadroomState.TIGHT, f"failed to exit after full sustain window: {state}"


def _test_emergency_drops_immediately():
    ctrl = HeadroomController(now=0.0)
    roomy_sample = _mk_sample(ram_avail_gb=20.0, gpu_pct=5.0, vram_used_gb=1.0)
    t = 0.0
    for _ in range(int(ENTER_SUSTAIN_S) + 2):
        ctrl.update(roomy_sample, now=t)
        t += 1.0
        ctrl.update(roomy_sample, now=t)
        t += 1.0
        # give it a chance to reach ROOMY too
    for _ in range(int(ENTER_SUSTAIN_S) + 2):
        t += 1.0
        state = ctrl.update(roomy_sample, now=t)
    assert state == HeadroomState.ROOMY, f"setup failed to reach ROOMY: {state}"

    # Severe RAM pressure: must drop to TIGHT on the very next sample, no
    # sustain required.
    emergency = _mk_sample(ram_avail_gb=EMERGENCY_FREE_RAM_GB_FLOOR - 0.1, gpu_pct=50.0, vram_used_gb=2.0)
    t += 1.0
    state = ctrl.update(emergency, now=t)
    assert state == HeadroomState.TIGHT, f"emergency (low RAM) did not drop immediately: {state}"

    # Reset, get back to ROOMY, then test the "GPU >= 95% while SC runs" path.
    ctrl2 = HeadroomController(now=0.0)
    t = 0.0
    state = HeadroomState.TIGHT
    for _ in range(2 * (int(ENTER_SUSTAIN_S) + 2)):
        t += 1.0
        state = ctrl2.update(roomy_sample, now=t)
    assert state == HeadroomState.ROOMY, f"setup 2 failed to reach ROOMY: {state}"

    sc_gpu_emergency = _mk_sample(
        ram_avail_gb=20.0, gpu_pct=EMERGENCY_GPU_LOAD_PCT_WHILE_SC, vram_used_gb=2.0,
        sc_running=True, sc_gpu_pct=90.0,
    )
    t += 1.0
    state = ctrl2.update(sc_gpu_emergency, now=t)
    assert state == HeadroomState.TIGHT, f"emergency (GPU>=95% w/ SC) did not drop immediately: {state}"


def _test_gpu_engine_aggregation():
    # Two processes, three engine types. Task Manager's headline number =
    # MAX across engine types of the SUM across every instance of that type.
    items = [
        # 3D engine: two instances (two pids), sum = 30 + 15 = 45
        ("pid_100_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D", 30.0, 0),
        ("pid_200_luid_0x00000000_0x0000AAAA_phys_0_eng_1_engtype_3D", 15.0, 0),
        # Video Decode: one instance, sum = 60 -- this should win the MAX
        ("pid_100_luid_0x00000000_0x0000AAAA_phys_0_eng_2_engtype_Video Decode", 60.0, 0),
        # Copy: one instance, sum = 5
        ("pid_200_luid_0x00000000_0x0000AAAA_phys_0_eng_3_engtype_Copy", 5.0, 0),
        # a bad-status instance must be ignored entirely
        ("pid_300_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D", 999.0, 0xC0000BBA),
        # an instance that doesn't match the pattern must be ignored, not crash
        ("garbage_instance_name", 42.0, 0),
    ]
    overall, per_engtype, per_pid = aggregate_gpu_engine(items)
    assert per_engtype == {"3D": 45.0, "Video Decode": 60.0, "Copy": 5.0}, per_engtype
    assert overall == 60.0, f"expected MAX-of-sums 60.0, got {overall}"
    assert per_pid[100] == {"3D": 30.0, "Video Decode": 60.0}, per_pid.get(100)
    assert per_pid[200] == {"3D": 15.0, "Copy": 5.0}, per_pid.get(200)
    assert 300 not in per_pid, "bad-status instance leaked into per_pid"


def _test_gpu_engine_aggregation_empty():
    overall, per_engtype, per_pid = aggregate_gpu_engine([])
    assert overall == 0.0
    assert per_engtype == {}
    assert per_pid == {}


def run_selftests(verbose: bool = True) -> bool:
    cases = [
        ("sustained headroom promotes", _test_sustained_promotes),
        ("single spike does not promote", _test_single_spike_does_not_promote),
        ("no flapping on noisy oscillation around a threshold", _test_no_flapping_on_noisy_oscillation),
        ("exit needs sustain", _test_exit_needs_sustain),
        ("emergency drops immediately", _test_emergency_drops_immediately),
        ("GPU engine instance-name aggregation (sum per engtype, then max)", _test_gpu_engine_aggregation),
        ("GPU engine aggregation on empty input", _test_gpu_engine_aggregation_empty),
    ]
    all_ok = True
    for name, fn in cases:
        cname, ok, msg = _case(name, fn)
        all_ok = all_ok and ok
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {cname}"
        if not ok:
            line += f" -- {msg}"
        if verbose:
            print(line)
    if verbose:
        print(f"\n{'ALL PASS' if all_ok else 'SOME FAILED'} ({len(cases)} cases)")
    return all_ok


# =============================================================================
# PART 4 -- CLI
# =============================================================================


def _fmt_bytes_gb(b: Optional[int]) -> str:
    return "None" if b is None else f"{b / (1024**3):.2f} GB"


def _fmt_pct(p: Optional[float]) -> str:
    return "None" if p is None else f"{p:.1f}%"


def print_live_sample(interval: float = 1.0) -> None:
    print(f"Sampling live hardware state (rate-counter interval={interval}s)...")
    s = sample_all(interval=interval)
    print(f"\ntimestamp: {s.timestamp}")

    print("\n-- CPU --")
    print(f"cpu_total_pct:      {_fmt_pct(s.cpu_total_pct)}")
    print(f"cpu_counter_used:   {s.cpu_counter_used}")
    print(f"cpu_reason:         {s.cpu_reason}")

    print("\n-- RAM --")
    print(f"ram_total_bytes:    {s.ram_total_bytes} ({_fmt_bytes_gb(s.ram_total_bytes)})")
    print(f"ram_avail_bytes:    {s.ram_avail_bytes} ({_fmt_bytes_gb(s.ram_avail_bytes)})")
    print(f"ram_reason:         {s.ram_reason}")

    print("\n-- GPU load (system headline, Task-Manager-equivalent) --")
    print(f"gpu_total_pct:      {_fmt_pct(s.gpu_total_pct)}")
    print(f"gpu_per_engtype_pct: {s.gpu_per_engtype_pct}")
    print(f"gpu_reason:         {s.gpu_reason}")

    print("\n-- StarCitizen.exe --")
    print(f"sc_pid:             {s.sc_pid}")
    print(f"sc_gpu_pct:         {_fmt_pct(s.sc_gpu_pct)}")
    print(f"sc_gpu_per_engtype_pct: {s.sc_gpu_per_engtype_pct}")
    print(f"sc_gpu_reason:      {s.sc_gpu_reason}")

    print("\n-- VRAM --")
    print(f"vram_total_bytes:   {s.vram_total_bytes} ({_fmt_bytes_gb(s.vram_total_bytes)})")
    print(f"vram_total_reason:  {s.vram_total_reason}")
    print(f"vram_used_bytes:    {s.vram_used_bytes} ({_fmt_bytes_gb(s.vram_used_bytes)})")
    print(f"vram_used_per_adapter: {s.vram_used_per_adapter}")
    print(f"vram_used_reason:   {s.vram_used_reason}")
    print(f"vram_free_gb:       {s.vram_free_gb}")
    print(f"sc_vram_used_bytes: {s.sc_vram_used_bytes} ({_fmt_bytes_gb(s.sc_vram_used_bytes)})")
    print(f"sc_vram_used_reason: {s.sc_vram_used_reason}")

    print("\n-- HeadroomController classification of this one sample --")
    ctrl = HeadroomController(now=0.0)
    state = ctrl.update(s, now=0.0)
    print(f"(single-sample state, no sustain applied yet): {state.value}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        ok = run_selftests(verbose=True)
        return 0 if ok else 1
    if "--sample" in argv:
        print_live_sample()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
