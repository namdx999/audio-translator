#!/usr/bin/env python3
"""
Live Audio Translator
Dịch âm thanh real-time từ bất kỳ ứng dụng nào (trình duyệt, media player...)
sang tiếng Việt qua Gemini Live API.

Kiến trúc chống lặp loa 3 tầng (v4 — mọi phiên bản Windows):
  - Tầng 1 (Win 10 2004+ Build ≥ 19041 & Win 11): Gọi trực tiếp WASAPI
    ActivateAudioInterfaceAsync (VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK) chế độ
    INCLUDE_TARGET_PROCESS_TREE theo Root PID của ứng dụng nguồn (vượt qua giới hạn
    build 20438 sai của proctap, bật AUTOCONVERTPCM cho mọi tần số mẫu).
  - Tầng 2 (Fallback cấp OS Mixer): Dùng chế độ EXCLUDE_TARGET_PROCESS_TREE với
    chính Root PID của AudioTranslator → Windows tự loại bỏ 100% giọng dịch khỏi
    luồng thu.
  - Tầng 3 (Windows đời cũ < Build 19041): Khử vọng số (EchoCanceller: GCC-PHAT +
    bộ lọc FIR Wiener-Hopf + bộ triệt phần dư phi tuyến NLP & khóa chống bypass).

Đóng gói .exe (chạy trên PC khác, không cần Python):
    pip install pyinstaller google-genai numpy psutil pyaudio pycaw comtypes proctap
    pyinstaller --noconfirm --onefile --windowed --name "AudioTranslator" ^
        --collect-all proctap --hidden-import comtypes --hidden-import pycaw chrome_translator.py
"""

import os
import sys
import time
import ctypes
from ctypes import wintypes
import asyncio
import threading
import queue as thread_queue
import collections
import argparse
import webbrowser
import tkinter as tk
from tkinter import ttk

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

import base64
import json
import numpy as np
import psutil
import pyaudio
from google import genai
from google.genai import types

# ────────────────────────────────────────────────────────────────
# Lưu trữ cấu hình (API key) — file JSON trong AppData
# ────────────────────────────────────────────────────────────────
_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AudioTranslator")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def load_api_key() -> str:
    """Ưu tiên: saved config > env var."""
    cfg = _load_config()
    return cfg.get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")


def save_api_key(key: str):
    cfg = _load_config()
    cfg["api_key"] = key
    _save_config(cfg)


def clear_api_key():
    cfg = _load_config()
    cfg.pop("api_key", None)
    _save_config(cfg)

# ────────────────────────────────────────────────────────────────
# Cấu hình
# ────────────────────────────────────────────────────────────────
MODEL = "models/gemini-3.5-live-translate-preview"
TARGET_LANGUAGE = "vi"
API_KEY_URL = "https://aistudio.google.com/apikey"

# ponytail: avatar base64 — circular 80x80 PNG of @dieptrader
AVATAR_B64 = (
    "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"
)

LANGUAGE_CHOICES = [
    ("vi", "Tiếng Việt"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("zh-Hans", "中文 (简体)"),
    ("zh-Hant", "中文 (繁體)"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("ru", "Русский"),
    ("th", "ไทย"),
    ("id", "Bahasa Indonesia"),
]

SEND_SAMPLE_RATE = 16000
CAPTURE_RATE = 48000
CAPTURE_CHANNELS = 2
ORIGINAL_VOLUME = 0.30   # âm lượng app nguồn khi đang dịch (0.0–1.0)
OUTPUT_FRAME = 1024
PLAYBACK_RATE = 24000

client = None


def init_client(api_key: str):
    global client
    client = genai.Client(http_options={"api_version": "v1beta"}, api_key=api_key)


def make_config(target_lang: str) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_lang,
            echo_target_language=False,
        ),
    )


# ────────────────────────────────────────────────────────────────
# App audio: liệt kê + điều khiển âm lượng theo PID (pycaw / WASAPI)
# ────────────────────────────────────────────────────────────────
_APP_NAMES = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "opera.exe": "Opera",
    "vlc.exe": "VLC",
    "mpv.exe": "mpv",
    "mpc-hc.exe": "MPC-HC",
    "mpc-hc64.exe": "MPC-HC",
    "mpc-be.exe": "MPC-BE",
    "mpc-be64.exe": "MPC-BE",
    "potplayermini.exe": "PotPlayer",
    "potplayermini64.exe": "PotPlayer",
    "potplayer.exe": "PotPlayer",
    "wmplayer.exe": "Windows Media Player",
    "foobar2000.exe": "foobar2000",
    "musicbee.exe": "MusicBee",
    "aimp.exe": "AIMP",
    "winamp.exe": "Winamp",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "msedgewebview2.exe": "WebView2",
}


def _pretty_app_name(exe_name: str) -> str:
    nl = exe_name.lower()
    if nl in _APP_NAMES:
        return _APP_NAMES[nl]
    base = exe_name.rsplit(".", 1)[0] if "." in exe_name else exe_name
    return base.capitalize()


def _get_process_label(name: str, cmdline: list[str] | None = None) -> str:
    """Tương thích kiểm thử: nhãn hiển thị cho tiến trình trình duyệt/ứng dụng."""
    nl = (name or "").lower()
    cmdline = cmdline or []
    if nl in ("chrome.exe", "msedge.exe", "brave.exe"):
        browser = "Chrome" if "chrome" in nl else ("Edge" if "msedge" in nl else "Brave")
        profile = "default"
        for arg in cmdline:
            if arg.startswith("--profile-directory="):
                profile = arg.split("=", 1)[1].strip('"').strip("'")
                break
        return f"{browser} ({profile})"
    return name.rsplit(".", 1)[0] if "." in name else name


def _is_child_process(name: str, cmdline: list[str] | None = None) -> bool:
    """Tương thích kiểm thử: kiểm tra tiến trình con của Chromium."""
    nl = (name or "").lower()
    cmdline = cmdline or []
    if nl in ("chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe"):
        return any(arg.startswith("--type=") for arg in cmdline)
    return False


def _own_pids() -> set[int]:
    """PID của chính app (+ tiến trình cha/con bootloader khi đóng gói PyInstaller onefile)."""
    pids = {os.getpid()}
    try:
        me = psutil.Process()
        my_name = me.name().lower()
        parent = me.parent()
        while parent and parent.name().lower() == my_name:
            pids.add(parent.pid)
            parent = parent.parent()
        for child in me.children(recursive=True):
            pids.add(child.pid)
    except Exception:
        pass
    return pids


def _own_root_pid() -> int:
    """Root PID của chính AudioTranslator (dùng cho EXCLUDE_TARGET_PROCESS_TREE)."""
    pid = os.getpid()
    try:
        me = psutil.Process(pid)
        my_name = me.name().lower()
        cur = me
        visited = {pid}
        while True:
            parent = cur.parent()
            if not parent or parent.pid in visited or parent.name().lower() != my_name:
                break
            visited.add(parent.pid)
            cur = parent
        return cur.pid
    except Exception:
        return pid


def _root_process_pid(pid: int) -> int:
    """Tìm tiến trình gốc cùng tên (vd: từ chrome.exe --type=utility lên chrome.exe gốc)
    để INCLUDE_TARGET_PROCESS_TREE bao phủ toàn bộ cây tiến trình của ứng dụng."""
    if not pid or pid <= 0:
        return pid
    own = _own_pids()
    try:
        cur = psutil.Process(pid)
        base_name = (cur.name() or "").lower()
        visited = {pid}
        while True:
            parent = cur.parent()
            if (not parent or parent.pid in visited or parent.pid in own
                    or (parent.name() or "").lower() != base_name):
                break
            visited.add(parent.pid)
            cur = parent
        return cur.pid
    except Exception:
        return pid


def _process_family_pids(pid: int) -> set[int]:
    """Tập hợp mọi PID thuộc cùng cây ứng dụng (hoặc cùng tên thực thi) với pid."""
    if not pid or pid <= 0:
        return set()
    own = _own_pids()
    family = {pid}
    root_pid = _root_process_pid(pid)
    family.add(root_pid)
    try:
        root_proc = psutil.Process(root_pid)
        target_name = (root_proc.name() or "").lower()
        for child in root_proc.children(recursive=True):
            if child.pid not in own:
                family.add(child.pid)
        if target_name:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    p = proc.info["pid"]
                    if p not in own and (proc.info["name"] or "").lower() == target_name:
                        family.add(p)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
    except Exception:
        pass
    return family - own


def _get_active_audio_pids() -> dict[int, bool] | None:
    """Trả {pid: is_playing} theo audio session của Windows. None nếu không có pycaw."""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return None
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return None
    pids: dict[int, bool] = {}
    for session in sessions:
        try:
            pid = session.ProcessId
            if not pid:
                continue
            is_playing = session.State == 1
            pids[pid] = pids.get(pid, False) or is_playing
        except Exception:
            continue
    return pids


def list_audio_sources() -> list[tuple[int, str, bool]]:
    """Trả (root_pid, app_name, is_playing). Gộp trùng tên, app đang phát lên đầu."""
    known = set(_APP_NAMES.keys())
    active_pids = _get_active_audio_pids()
    own_pids = _own_pids()

    raw: list[tuple[int, str, bool]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pid = proc.info["pid"]
            if pid in own_pids:
                continue  # không cho chọn chính app này (sẽ tự dịch giọng dịch của mình)
            name = (proc.info["name"] or "").lower()
            if active_pids is not None:
                if pid not in active_pids:
                    continue
                is_playing = active_pids[pid]
            else:
                if name not in known:
                    continue
                is_playing = False
            root_pid = _root_process_pid(pid)
            if root_pid in own_pids:
                continue
            raw.append((root_pid, _pretty_app_name(name), is_playing))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    by_name: dict[str, tuple[int, str, bool]] = {}
    for pid, app, playing in raw:
        cur = by_name.get(app)
        if cur is None or (playing and not cur[2]):
            by_name[app] = (pid, app, playing)

    results = list(by_name.values())
    results.sort(key=lambda x: (not x[2], x[1].lower()))
    return results


def list_output_devices() -> list[tuple[int, str]]:
    """Thiết bị phát (index PyAudio, tên) thuộc host API mặc định."""
    pa = pyaudio.PyAudio()
    try:
        host = pa.get_default_host_api_info()["index"]
        out: list[tuple[int, str]] = []
        seen: set[str] = set()
        for i in range(pa.get_device_count()):
            try:
                d = pa.get_device_info_by_index(i)
            except Exception:
                continue
            if d.get("hostApi") != host or int(d.get("maxOutputChannels", 0)) < 1:
                continue
            name = str(d.get("name", f"Device {i}"))
            if "sound mapper" in name.lower() or name in seen:
                continue
            seen.add(name)
            out.append((i, name))
        return out
    except Exception:
        return []
    finally:
        pa.terminate()


def is_default_output(index: int | None) -> bool:
    """True nếu index là loa mặc định (chính là loa mà chế độ ghi toàn hệ thống đang ghi)."""
    if index is None:
        return True
    pa = pyaudio.PyAudio()
    try:
        return int(pa.get_default_output_device_info()["index"]) == int(index)
    except Exception:
        return True
    finally:
        pa.terminate()


class AppVolumeController:
    """Điều khiển âm lượng của tất cả audio session thuộc cây tiến trình đích (qua pycaw).
    Hỗ trợ cả trình duyệt đa tiến trình (Chrome/Edge phát âm ở tiến trình con utility).
    Nhớ mức ban đầu để khôi phục khi kết thúc."""

    def __init__(self, pid: int):
        self.pid = pid
        self._original: float | None = None
        self._available = False
        try:
            from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume  # noqa
            self._available = True
        except Exception:
            self._available = False

    def _iter_volume_interfaces(self):
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        target_pids = _process_family_pids(self.pid)
        if not target_pids:
            return
        for session in AudioUtilities.GetAllSessions():
            try:
                if session.ProcessId not in target_pids:
                    continue
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                yield vol
            except Exception:
                continue

    def set_volume(self, level: float):
        """level 0.0–1.0. Lần đầu gọi sẽ lưu lại mức gốc để khôi phục sau."""
        if not self._available:
            return
        level = max(0.0, min(1.0, level))
        try:
            first = True
            for vol in self._iter_volume_interfaces():
                if first and self._original is None:
                    try:
                        self._original = vol.GetMasterVolume()
                    except Exception:
                        self._original = 1.0
                    first = False
                vol.SetMasterVolume(level, None)
        except Exception:
            pass

    def restore(self):
        if not self._available or self._original is None:
            return
        try:
            for vol in self._iter_volume_interfaces():
                vol.SetMasterVolume(self._original, None)
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────
# Audio helpers
# ────────────────────────────────────────────────────────────────
def resample_mono(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    n = int(len(samples) * dst_rate / src_rate)
    if n <= 0:
        return np.array([], dtype=np.float32)
    return np.interp(
        np.linspace(0, 1, n), np.linspace(0, 1, len(samples)), samples
    ).astype(np.float32)


class StreamResampler:
    """Resample tuyến tính CÓ TRẠNG THÁI giữa các chunk.
    Khác resample_mono: không làm rơi mẫu / lệch nhịp ở biên chunk — bắt buộc
    để tín hiệu tham chiếu và bản ghi luôn khớp mẫu-với-mẫu khi khử vọng."""

    def __init__(self, src_rate: int, dst_rate: int):
        self.step = src_rate / dst_rate
        self.pos = 0.0
        self.tail = np.zeros(0, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.step == 1.0 or len(x) == 0:
            return x
        buf = np.concatenate([self.tail, x]) if len(self.tail) else x
        last = len(buf) - 1
        if last < self.pos:
            self.tail = buf
            return np.zeros(0, dtype=np.float32)
        n = int((last - self.pos) // self.step) + 1
        idx = self.pos + np.arange(n) * self.step
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        nxt = self.pos + n * self.step
        keep = min(int(nxt), last)
        self.tail = buf[keep:]
        self.pos = nxt - keep
        return out


class SincResampler:
    """Resample băng thông giới hạn (windowed-sinc) CÓ TRẠNG THÁI.
    Dùng cho tín hiệu tham chiếu: mô phỏng bộ SRC chất lượng cao của Windows
    (24 kHz → tần số mix của loa) để tham chiếu khớp với vọng thật trong bản ghi."""

    def __init__(self, src_rate: int, dst_rate: int, half_taps: int = 16):
        self.step = src_rate / dst_rate
        self.half = half_taps
        self.cut = min(1.0, dst_rate / src_rate)
        self.buf = np.zeros(half_taps, dtype=np.float32)
        self.pos = float(half_taps)
        self._offs = np.arange(-half_taps + 1, half_taps + 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if len(x) == 0:
            return x
        buf = np.concatenate([self.buf, x])
        maxpos = len(buf) - 1 - self.half
        if self.pos > maxpos:
            self.buf = buf
            return np.zeros(0, dtype=np.float32)
        n = int((maxpos - self.pos) // self.step) + 1
        t = self.pos + np.arange(n) * self.step
        base = np.floor(t).astype(np.int64)
        idx = base[:, None] + self._offs[None, :]
        d = t[:, None] - idx
        w = self.cut * np.sinc(self.cut * d) * (0.5 + 0.5 * np.cos(np.pi * d / self.half))
        out = (buf[idx] * w).sum(axis=1).astype(np.float32)
        nxt = self.pos + n * self.step
        drop = max(0, int(np.floor(nxt)) - self.half + 1)
        self.buf = buf[drop:]
        self.pos = nxt - drop
        return out


class RefResampler:
    """Đường tham chiếu = giống hệt đường vọng thật:
    24 kHz → (sinc, như Windows) → tần số mix → (StreamResampler, như nhánh ghi) → 16 kHz."""

    def __init__(self, play_rate: int, mix_rate: int, send_rate: int):
        self.up = SincResampler(play_rate, mix_rate) if mix_rate != play_rate else None
        self.down = StreamResampler(mix_rate, send_rate)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.up is not None:
            x = self.up.process(x)
        return self.down.process(x)


# ────────────────────────────────────────────────────────────────
# Khử vọng số (Tầng 3) — chỉ dùng trên Windows cổ (< Build 19041)
# khi OS không hỗ trợ VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
# ────────────────────────────────────────────────────────────────
class EchoCanceller:
    """Bản ghi toàn hệ thống = âm gốc + giọng dịch do chính app phát ra.
    Ta biết CHÍNH XÁC tín hiệu đã phát (tham chiếu) → ước lượng độ trễ bằng
    GCC-PHAT, ước lượng đường vọng tuyến tính (FIR Wiener-Hopf) để trừ đi,
    kết hợp bộ triệt phần dư phi tuyến (NLP post-filter) và khóa half-duplex
    khi chưa đồng bộ độ trễ (tuyệt đối không bypass khi đang phát giọng dịch).
    Toàn bộ chạy ở 16 kHz mono. Thread-safe."""

    FS = SEND_SAMPLE_RATE
    N = 256                 # khối xử lý (16 ms)
    TAPS = 128              # độ dài FIR đường vọng (8 ms)
    PRE = 32                # tap trước độ trễ ước lượng (đáp ứng bộ lọc SRC)
    MEMORY_S = 3.0          # hằng số thời gian trọng số mũ (theo thời gian có giọng dịch)
    SOLVE_EVERY = 4         # giải lại hệ phương trình mỗi 4 khối
    RING_S = 12
    EST_WIN_S = 1.5
    EST_EVERY_S = 0.5
    LAG_MIN_S, LAG_MAX_S = -0.8, 2.0
    MIN_CONF = 5.5
    REF_ACTIVE_RMS = 1.5e-3
    RECONVERGE_S = 0.6
    HANGOVER_S = 0.15       # giữ cổng NLP 150ms sau khi loa phát xong câu dịch

    def __init__(self, on_event=None):
        self._lock = threading.Lock()
        size = int(self.FS * self.RING_S)
        self._ref = np.zeros(size, dtype=np.float32)
        self._cap = np.zeros(size, dtype=np.float32)
        self._ref_n = 0
        self._cap_n = 0
        self._ref_origin: float | None = None
        self._cap_origin: float | None = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._next_est = int(self.FS * 0.75)
        self.lag: int | None = None      # cap_index = ref_index + lag
        self._cand: int | None = None
        self._mute_until = 0
        self._last_active_m = -10**9
        self.bypass = False
        self._on_event = on_event
        self._reset_filter()

    def _event(self, text: str):
        if self._on_event:
            try:
                self._on_event(text)
            except Exception:
                pass

    def _reset_filter(self):
        L = self.TAPS
        self._h = np.zeros(L, dtype=np.float64)          # FIR đường vọng
        self._R = np.zeros((L, L), dtype=np.float64)     # tự tương quan tham chiếu
        self._p = np.zeros(L, dtype=np.float64)          # tương quan chéo ref↔bản ghi
        self._blocks = 0
        self._lam = float(np.exp(-self.N / (self.FS * self.MEMORY_S)))
        self._ey = 0.0             # tương quan phần dư ↔ ước lượng vọng (phát hiện đường vọng đổi)
        self._yy = 0.0
        self._last_forget = 0

    @staticmethod
    def _ring_write(buf: np.ndarray, count: int, x: np.ndarray):
        size, n = len(buf), len(x)
        if n == 0:
            return
        if n > size:
            count += n - size
            x = x[-size:]
            n = size
        i = count % size
        first = min(n, size - i)
        buf[i:i + first] = x[:first]
        if first < n:
            buf[:n - first] = x[first:]

    @staticmethod
    def _ring_read(buf: np.ndarray, count: int, start: int, length: int):
        size = len(buf)
        if start < 0 or start < count - size or start + length > count:
            return None
        i = start % size
        first = min(length, size - i)
        if first == length:
            return buf[i:i + length].copy()
        return np.concatenate([buf[i:], buf[:length - first]])

    # ── Tham chiếu: đúng những mẫu vừa ghi ra loa (16 kHz) ──
    def push_ref(self, x: np.ndarray, t: float):
        if len(x) == 0:
            return
        with self._lock:
            self._ring_write(self._ref, self._ref_n, x)
            self._ref_n += len(x)
            o = t - self._ref_n / self.FS
            if self._ref_origin is None or o < self._ref_origin:
                self._ref_origin = o

    # ── Bản ghi: trả về tín hiệu đã khử vọng (trễ tối đa 1 khối) ──
    def process(self, y: np.ndarray, t: float) -> np.ndarray:
        if len(y) == 0:
            return y
        with self._lock:
            self._ring_write(self._cap, self._cap_n, y)
            self._cap_n += len(y)
            o = t - self._cap_n / self.FS
            if self._cap_origin is None or o < self._cap_origin:
                self._cap_origin = o
            if self._cap_n >= self._next_est:
                step_s = 0.25 if self.lag is None else self.EST_EVERY_S
                self._next_est = self._cap_n + int(self.FS * step_s)
                try:
                    self._estimate()
                except Exception:
                    pass

            self._pending = np.concatenate([self._pending, y])
            N = self.N
            nblk = len(self._pending) // N
            if nblk == 0:
                return np.zeros(0, dtype=np.float32)
            m = self._cap_n - len(self._pending)
            out = np.empty(nblk * N, dtype=np.float32)
            for b in range(nblk):
                out[b * N:(b + 1) * N] = self._cancel(
                    self._pending[b * N:(b + 1) * N], m + b * N)
            self._pending = self._pending[nblk * N:]
            return out

    def _ref_active_recent(self, window_s: float = 0.35) -> bool:
        L = min(self._ref_n, int(self.FS * window_s))
        if L <= 0:
            return False
        x = self._ring_read(self._ref, self._ref_n, self._ref_n - L, L)
        return x is not None and float(np.sqrt(np.mean(x * x))) > self.REF_ACTIVE_RMS

    def _cancel(self, y: np.ndarray, m: int) -> np.ndarray:
        N = self.N
        if self.bypass:
            return y
        if self.lag is None:
            # Chưa khoá được độ trễ mà giọng dịch đang phát → luôn khoá cổng thu
            # (TUYỆT ĐỐI không bật bypass để tránh lọt tiếng dịch gây lặp vô tận).
            if self._ref_active_recent(0.35):
                return np.zeros(N, dtype=np.float32)
            return y

        L = self.TAPS
        s = m - self.lag + self.PRE
        x = self._ring_read(self._ref, self._ref_n, s - (L - 1), N + L - 1)
        if x is None:
            if self._ref_active_recent(0.25):
                return np.zeros(N, dtype=np.float32)
            return y
        xm = np.lib.stride_tricks.sliding_window_view(x, L)[:, ::-1]  # xm[i,k] = ref[s+i-k]
        yhat = xm @ self._h
        e = y - yhat
        xn = x[L - 1:]
        px = float(np.dot(xn, xn)) / N
        active_thresh = self.REF_ACTIVE_RMS ** 2 * 0.25
        if px > active_thresh:
            self._last_active_m = m + N
            # Cập nhật thống kê Wiener-Hopf (chỉ khi có giọng dịch)
            self._R *= self._lam
            self._R += xm.T @ xm
            self._p *= self._lam
            self._p += xm.T @ y
            self._blocks += 1
            # Đường vọng đổi (vd đổi âm lượng app trong Volume Mixer): phần dư còn
            # tương quan với ước lượng vọng → quên nhanh thống kê cũ để ước lượng lại.
            self._ey = 0.95 * self._ey + 0.05 * float(np.dot(e, yhat))
            self._yy = 0.95 * self._yy + 0.05 * float(np.dot(yhat, yhat))
            if (self._blocks - self._last_forget > 60 and self._yy > 1e-6
                    and abs(self._ey) > 0.2 * self._yy):
                self._R *= 0.05
                self._p *= 0.05
                self._last_forget = self._blocks
                self._ey = 0.0
            if self._blocks % self.SOLVE_EVERY == 0:
                d = 1e-4 * float(np.trace(self._R)) / L + 1e-9
                try:
                    self._h = np.linalg.solve(self._R + d * np.eye(L), self._p)
                except np.linalg.LinAlgError:
                    pass

        if m < self._mute_until and px > active_thresh:
            return np.zeros(N, dtype=np.float32)  # bộ lọc đang hội tụ

        # Bộ triệt phần dư phi tuyến (NLP / Wiener post-filter):
        # Khi giọng dịch đang phát (hoặc trong khoảng ngân hangover), triệt tiêu
        # phần vọng dư phi tuyến do Windows Audio Limiter / Enhancements gây ra.
        in_hangover = (m <= self._last_active_m + int(self.HANGOVER_S * self.FS))
        if px > active_thresh * 0.4 or in_hangover:
            py_hat = float(np.dot(yhat, yhat)) / N
            pe = float(np.dot(e, e)) / N
            echo_floor = 0.25 * py_hat + 0.05 * px
            if pe <= echo_floor:
                return np.zeros(N, dtype=np.float32)
            gain = max(0.0, (pe - echo_floor) / (pe + 1e-12))
            e = e * gain

        return e.astype(np.float32)

    def _estimate(self):
        """Ước lượng độ trễ loa→bản ghi bằng GCC-PHAT."""
        if self._ref_origin is None or self._cap_origin is None:
            return
        FS = self.FS
        W = min(int(FS * self.EST_WIN_S), self._cap_n)
        if W < int(FS * 0.4):
            return
        m1 = self._cap_n
        m0 = m1 - W
        y = self._ring_read(self._cap, self._cap_n, m0, W)
        if y is None:
            return
        A0 = (self._ref_origin - self._cap_origin) * FS
        lo = int(np.floor(A0 + self.LAG_MIN_S * FS))
        hi = int(np.ceil(A0 + self.LAG_MAX_S * FS))
        r0 = max(m0 - hi, self._ref_n - len(self._ref), 0)
        r1 = min(m1 - lo, self._ref_n)
        if r1 - r0 < W + 64:
            return
        x = self._ring_read(self._ref, self._ref_n, r0, r1 - r0)
        if x is None or float(np.sqrt(np.mean(x * x))) < self.REF_ACTIVE_RMS:
            return
        L = len(x)
        nfft = 1 << int(np.ceil(np.log2(L + W)))
        C = np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(y, nfft))
        C /= np.abs(C) + 1e-12
        c = np.fft.irfft(C, nfft)[:L - W + 1]
        j = int(np.argmax(c))
        mask = np.ones(len(c), dtype=bool)
        mask[max(0, j - 32):j + 33] = False
        if mask.sum() < 50:
            return
        conf = float(c[j] / (np.std(c[mask]) + 1e-12))
        if conf < self.MIN_CONF:
            return
        A = m0 - r0 - j

        if self.lag is None or self.bypass:
            self._lock_lag(A)
            self._event(f"Khử vọng: đã đồng bộ (trễ ≈ {A / FS * 1000:.0f} ms so với mốc).")
        elif abs(A - self.lag) > 8:
            # cần 2 lần đo khớp nhau mới đổi → tránh nhảy do đỉnh giả
            if self._cand is not None and abs(A - self._cand) <= 8:
                self._lock_lag(A)
                self._event("Khử vọng: độ trễ thay đổi, đồng bộ lại.")
            else:
                self._cand = A
        else:
            self._cand = None

    def _lock_lag(self, A: int):
        self.lag = A
        self._cand = None
        self.bypass = False
        self._reset_filter()
        self._mute_until = self._cap_n + int(self.FS * self.RECONVERGE_S)


# ────────────────────────────────────────────────────────────────
# Native Windows WASAPI Process Loopback (Tầng 1 & Tầng 2)
# Vượt qua giới hạn giả Build 20438 của proctap → chạy trên cả
# Windows 10 (2004 / 21H2 / 22H2 — Build ≥ 19041) và Windows 11.
# ────────────────────────────────────────────────────────────────
if sys.platform.startswith("win"):
    _ole32 = ctypes.windll.ole32
    _mmdevapi = ctypes.windll.mmdevapi
    _kernel32 = ctypes.windll.kernel32

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_str(cls, s: str) -> "_GUID":
            g = cls()
            _ole32.IIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
            return g

    def _guid_eq(a: _GUID, b: _GUID) -> bool:
        return bytes(a) == bytes(b)

    _IID_IUnknown = _GUID.from_str("{00000000-0000-0000-C000-000000000046}")
    _IID_IAgileObject = _GUID.from_str("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _IID_IActivateAudioInterfaceCompletionHandler = _GUID.from_str(
        "{41D949AB-9862-444A-80F6-C261334DA5EB}"
    )
    _IID_IAudioClient = _GUID.from_str("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _IID_IAudioCaptureClient = _GUID.from_str("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")

    class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
        _fields_ = [
            ("ActivationType", wintypes.DWORD),       # 1 = PROCESS_LOOPBACK
            ("TargetProcessId", wintypes.DWORD),
            ("ProcessLoopbackMode", wintypes.DWORD),  # 0 = INCLUDE, 1 = EXCLUDE
        ]

    class _PROPVARIANT_BLOB(ctypes.Structure):
        _fields_ = [
            ("vt", wintypes.USHORT),                  # 65 = VT_BLOB
            ("wReserved1", wintypes.USHORT),
            ("wReserved2", wintypes.USHORT),
            ("wReserved3", wintypes.USHORT),
            ("cbSize", wintypes.ULONG),
            ("pBlobData", ctypes.c_void_p),
            ("_pad", ctypes.c_void_p),
        ]

    class _WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag", wintypes.WORD),
            ("nChannels", wintypes.WORD),
            ("nSamplesPerSec", wintypes.DWORD),
            ("nAvgBytesPerSec", wintypes.DWORD),
            ("nBlockAlign", wintypes.WORD),
            ("wBitsPerSample", wintypes.WORD),
            ("cbSize", wintypes.WORD),
        ]

    _QI_FUNC = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    _ADDREF_FUNC = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
    _RELEASE_FUNC = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
    _COMPLETED_FUNC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

    class _HandlerVtbl(ctypes.Structure):
        _fields_ = [
            ("QueryInterface", _QI_FUNC),
            ("AddRef", _ADDREF_FUNC),
            ("Release", _RELEASE_FUNC),
            ("ActivateCompleted", _COMPLETED_FUNC),
        ]

    class _HandlerObj(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]

    _mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.c_long

    def _com_vtable(ptr: int):
        return ctypes.cast(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )

    def _com_release(ptr: int | None):
        if ptr:
            try:
                vtbl = _com_vtable(ptr)
                ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])(ptr)
            except Exception:
                pass

    def _activate_wasapi_process_loopback(
        target_pid: int, loopback_mode: int
    ) -> tuple[int, int, dict]:
        """Kích hoạt WASAPI Process Loopback cho target_pid:
          - loopback_mode = 0: INCLUDE_TARGET_PROCESS_TREE (chỉ thu ứng dụng đích)
          - loopback_mode = 1: EXCLUDE_TARGET_PROCESS_TREE (thu mọi thứ TRỪ target_pid)
        Trả về (audio_client_ptr, capture_client_ptr, format_dict)."""
        _ole32.CoInitializeEx(None, 0)
        event = _kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise OSError("CreateEventW failed")
        act_hr = ctypes.c_long(-1)
        act_unk = ctypes.c_void_p(0)

        def _py_qi(this, riid, ppv):
            g = riid.contents
            if (
                _guid_eq(g, _IID_IUnknown)
                or _guid_eq(g, _IID_IActivateAudioInterfaceCompletionHandler)
                or _guid_eq(g, _IID_IAgileObject)
            ):
                ppv[0] = this
                return 0
            ppv[0] = 0
            return -2147467262  # E_NOINTERFACE

        def _py_addref(this):
            return 2

        def _py_release(this):
            return 1

        def _py_completed(this, op_ptr):
            try:
                vtbl = _com_vtable(op_ptr)
                get_res = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_long),
                    ctypes.POINTER(ctypes.c_void_p),
                )(vtbl[3])
                get_res(op_ptr, ctypes.byref(act_hr), ctypes.byref(act_unk))
            finally:
                _kernel32.SetEvent(event)
            return 0

        vtbl = _HandlerVtbl(
            _QI_FUNC(_py_qi),
            _ADDREF_FUNC(_py_addref),
            _RELEASE_FUNC(_py_release),
            _COMPLETED_FUNC(_py_completed),
        )
        handler = _HandlerObj(ctypes.pointer(vtbl))
        params = _AUDIOCLIENT_ACTIVATION_PARAMS(1, int(target_pid), int(loopback_mode))
        prop = _PROPVARIANT_BLOB(
            65, 0, 0, 0, ctypes.sizeof(params), ctypes.addressof(params), None
        )
        async_op = ctypes.c_void_p(0)

        try:
            hr = _mmdevapi.ActivateAudioInterfaceAsync(
                "VAD\\Process_Loopback",
                ctypes.byref(_IID_IAudioClient),
                ctypes.byref(prop),
                ctypes.byref(handler),
                ctypes.byref(async_op),
            )
            if hr < 0:
                raise RuntimeError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}")
            wait_res = _kernel32.WaitForSingleObject(event, 5000)
            if wait_res != 0 or act_hr.value < 0 or not act_unk.value:
                raise RuntimeError(
                    f"Process loopback activation failed: 0x{act_hr.value & 0xFFFFFFFF:08X}"
                )
        finally:
            _com_release(async_op.value)
            _kernel32.CloseHandle(event)

        audio_client = ctypes.c_void_p(0)
        try:
            unk_vtbl = _com_vtable(act_unk.value)
            qi_fn = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(unk_vtbl[0])
            hr_qi = qi_fn(act_unk.value, ctypes.byref(_IID_IAudioClient), ctypes.byref(audio_client))
            if hr_qi < 0 or not audio_client.value:
                raise RuntimeError(f"QueryInterface(IAudioClient) failed: 0x{hr_qi & 0xFFFFFFFF:08X}")
        finally:
            _com_release(act_unk.value)

        ac_vtbl = _com_vtable(audio_client.value)
        init_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.POINTER(_WAVEFORMATEX),
            ctypes.c_void_p,
        )(ac_vtbl[3])

        # LOOPBACK (0x00020000) | AUTOCONVERTPCM (0x80000000) | SRC_DEFAULT_QUALITY (0x08000000)
        stream_flags = 0x00020000 | 0x80000000 | 0x08000000
        # Ưu tiên 48kHz, 2 kênh, float32; fallback 44.1kHz, 2 kênh, int16
        wfx = _WAVEFORMATEX(3, 2, CAPTURE_RATE, CAPTURE_RATE * 8, 8, 32, 0)
        hr_init = init_fn(
            audio_client.value, 0, stream_flags, 2_000_000, 0, ctypes.byref(wfx), None
        )
        if hr_init < 0:
            wfx = _WAVEFORMATEX(1, 2, 44100, 44100 * 4, 4, 16, 0)
            hr_init = init_fn(
                audio_client.value, 0, stream_flags, 2_000_000, 0, ctypes.byref(wfx), None
            )
            if hr_init < 0:
                _com_release(audio_client.value)
                raise RuntimeError(f"IAudioClient.Initialize failed: 0x{hr_init & 0xFFFFFFFF:08X}")

        get_service = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(ac_vtbl[14])
        cap_client = ctypes.c_void_p(0)
        hr_svc = get_service(
            audio_client.value, ctypes.byref(_IID_IAudioCaptureClient), ctypes.byref(cap_client)
        )
        if hr_svc < 0 or not cap_client.value:
            _com_release(audio_client.value)
            raise RuntimeError(f"GetService(IAudioCaptureClient) failed: 0x{hr_svc & 0xFFFFFFFF:08X}")

        fmt = {
            "sample_rate": int(wfx.nSamplesPerSec),
            "channels": int(wfx.nChannels),
            "bits_per_sample": int(wfx.wBitsPerSample),
            "block_align": int(wfx.nBlockAlign),
        }
        return int(audio_client.value), int(cap_client.value), fmt


class WinProcessAudioCapture:
    """Bộ thu âm thanh 3 tầng chống lặp loa cho mọi phiên bản Windows:
      1. process_include (Win 10 2004+ Build ≥ 19041 & Win 11):
         Thu riêng cây tiến trình nguồn (INCLUDE_TARGET_PROCESS_TREE).
      2. process_exclude_self (Fallback cấp OS Mixer):
         Thu âm hệ thống nhưng loại trừ cây tiến trình của chính AudioTranslator
         (EXCLUDE_TARGET_PROCESS_TREE) → 0% tiếng dịch lọt vào luồng thu.
      3. system_loopback (Windows đời cũ < Build 19041):
         Fallback sang proctap toàn hệ thống + bật EchoCanceller có NLP."""

    def __init__(self, pid: int, on_data=None, prefer_mode: str = "auto"):
        self.raw_pid = pid
        self.root_pid = _root_process_pid(pid) if pid and pid > 0 else pid
        self._on_data = on_data
        self._prefer_mode = prefer_mode
        self._audio_client: int | None = None
        self._cap_client: int | None = None
        self._fallback_tap = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self.mode = "system_loopback"
        self.is_process_specific = False
        self.format: dict = {
            "sample_rate": CAPTURE_RATE,
            "channels": CAPTURE_CHANNELS,
            "bits_per_sample": 32,
            "block_align": 8,
        }
        self.last_error: str | None = None
        self._active_target_pid: int = self.root_pid
        self._active_loopback_mode: int = 0
        self._init_capture()

    def _init_capture(self):
        errors: list[str] = []
        if sys.platform.startswith("win"):
            # Tầng 1: INCLUDE_TARGET_PROCESS_TREE trên root_pid (và raw_pid nếu khác)
            if self._prefer_mode in ("auto", "include") and self.root_pid and self.root_pid > 0:
                candidates = [self.root_pid]
                if self.raw_pid != self.root_pid and self.raw_pid > 0:
                    candidates.append(self.raw_pid)
                for cand_pid in candidates:
                    try:
                        ac, cc, fmt = _activate_wasapi_process_loopback(cand_pid, 0)
                        self._audio_client, self._cap_client, self.format = ac, cc, fmt
                        self._active_target_pid = cand_pid
                        self._active_loopback_mode = 0
                        self.mode = "process_include"
                        self.is_process_specific = True
                        self.last_error = None
                        return
                    except Exception as e:
                        errors.append(f"include(pid={cand_pid}): {e}")

            # Tầng 2: EXCLUDE_TARGET_PROCESS_TREE trên chính tiến trình AudioTranslator
            if self._prefer_mode in ("auto", "exclude_self"):
                own_candidates = [_own_root_pid()]
                if os.getpid() not in own_candidates:
                    own_candidates.append(os.getpid())
                for own_pid in own_candidates:
                    try:
                        ac, cc, fmt = _activate_wasapi_process_loopback(own_pid, 1)
                        self._audio_client, self._cap_client, self.format = ac, cc, fmt
                        self._active_target_pid = own_pid
                        self._active_loopback_mode = 1
                        self.mode = "process_exclude_self"
                        self.is_process_specific = True
                        self.last_error = "; ".join(errors) if errors else None
                        return
                    except Exception as e:
                        errors.append(f"exclude_self(pid={own_pid}): {e}")

        # Tầng 3: Fallback về proctap (cho Windows < Build 19041)
        from proctap import ProcessAudioCapture
        self._fallback_tap = ProcessAudioCapture(self.raw_pid, on_data=self._on_data)
        ps, fmt, err = _inspect_tap(self._fallback_tap)
        self.is_process_specific = ps
        self.mode = "process_include" if ps else "system_loopback"
        if fmt:
            self.format = fmt
        if err:
            errors.append(err)
        self.last_error = "; ".join(errors) if errors else None

    def start(self):
        if self._fallback_tap is not None:
            self._fallback_tap.start()
            return
        if not self._audio_client:
            raise RuntimeError("WASAPI audio client not initialized")
        ac_vtbl = _com_vtable(self._audio_client)
        start_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vtbl[10])
        hr = start_fn(self._audio_client)
        if hr < 0:
            raise RuntimeError(f"IAudioClient.Start failed: 0x{hr & 0xFFFFFFFF:08X}")
        self._running.set()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _reopen_native(self) -> bool:
        """Tự động mở lại stream khi người dùng đổi thiết bị loa mặc định (0x88890004)."""
        self._release_native()
        try:
            ac, cc, fmt = _activate_wasapi_process_loopback(
                self._active_target_pid, self._active_loopback_mode
            )
            self._audio_client, self._cap_client, self.format = ac, cc, fmt
            ac_vtbl = _com_vtable(self._audio_client)
            start_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vtbl[10])
            return start_fn(self._audio_client) >= 0
        except Exception:
            return False

    def _capture_loop(self):
        _ole32.CoInitializeEx(None, 0)
        try:
            while self._running.is_set():
                cc = self._cap_client
                if not cc:
                    break
                cc_vtbl = _com_vtable(cc)
                get_next = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)
                )(cc_vtbl[5])
                get_buf = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(wintypes.UINT),
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                )(cc_vtbl[3])
                rel_buf = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, wintypes.UINT
                )(cc_vtbl[4])

                pkt = wintypes.UINT(0)
                hr = get_next(cc, ctypes.byref(pkt))
                if (hr & 0xFFFFFFFF) == 0x88890004:  # AUDCLNT_E_DEVICE_INVALIDATED
                    if not self._reopen_native():
                        time.sleep(0.2)
                    continue
                if hr < 0 or pkt.value == 0:
                    time.sleep(0.005)
                    continue

                while self._running.is_set() and pkt.value > 0:
                    data_ptr = ctypes.c_void_p(0)
                    nframes = wintypes.UINT(0)
                    flags = wintypes.DWORD(0)
                    hr_b = get_buf(
                        cc,
                        ctypes.byref(data_ptr),
                        ctypes.byref(nframes),
                        ctypes.byref(flags),
                        None,
                        None,
                    )
                    if hr_b < 0:
                        break
                    nf = int(nframes.value)
                    pcm_bytes = b""
                    if nf > 0:
                        ch = int(self.format.get("channels", 2))
                        bits = int(self.format.get("bits_per_sample", 32))
                        block_align = int(self.format.get("block_align", ch * (bits // 8)))
                        if (flags.value & 0x2) or not data_ptr.value:  # AUDCLNT_BUFFERFLAGS_SILENT
                            pcm_bytes = bytes(nf * ch * 4)
                        elif bits == 32:
                            pcm_bytes = ctypes.string_at(data_ptr.value, nf * block_align)
                        else:
                            raw = ctypes.string_at(data_ptr.value, nf * block_align)
                            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                            pcm_bytes = arr.tobytes()
                    rel_buf(cc, nframes.value)
                    if pcm_bytes and self._on_data is not None:
                        try:
                            self._on_data(pcm_bytes, nf)
                        except Exception:
                            pass
                    if get_next(cc, ctypes.byref(pkt)) < 0:
                        break
        finally:
            try:
                _ole32.CoUninitialize()
            except Exception:
                pass

    def _release_native(self):
        if self._audio_client:
            try:
                ac_vtbl = _com_vtable(self._audio_client)
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vtbl[11])(self._audio_client)
            except Exception:
                pass
        _com_release(self._cap_client)
        _com_release(self._audio_client)
        self._cap_client = None
        self._audio_client = None

    def close(self):
        self._running.clear()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        if self._fallback_tap is not None:
            try:
                self._fallback_tap.close()
            except Exception:
                pass
            self._fallback_tap = None
        self._release_native()


def _inspect_tap(tap) -> tuple[bool, dict | None, str | None]:
    """(is_process_specific, native_format, last_error) của bộ thu âm."""
    if isinstance(tap, WinProcessAudioCapture):
        return tap.is_process_specific, tap.format, tap.last_error
    native = getattr(getattr(tap, "_backend", None), "_native", None)
    if native is None:
        return True, None, None
    try:
        ps = bool(native.is_process_specific())
    except Exception:
        ps = True
    try:
        fmt = native.get_format()
    except Exception:
        fmt = None
    try:
        err = native.get_last_error()
    except Exception:
        err = None
    return ps, fmt, err


# ────────────────────────────────────────────────────────────────
# Translator engine
# ────────────────────────────────────────────────────────────────
class _StopSignal(Exception):
    pass


class Translator:
    def __init__(
        self,
        pid: int,
        target_lang: str = TARGET_LANGUAGE,
        original_vol: float = ORIGINAL_VOLUME,
        model: str = MODEL,
        stop_event: threading.Event | None = None,
        msg_queue: thread_queue.Queue | None = None,
        output_device: int | None = None,
    ):
        self.pid = pid
        self.root_pid = _root_process_pid(pid)
        self.target_lang = target_lang
        self.original_vol = original_vol
        self.model = model
        self.output_device = output_device   # None = loa mặc định của Windows
        self._stop = stop_event or threading.Event()
        self._msg_q = msg_queue

        self.session = None
        self.upload_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.translated_queue: asyncio.Queue = asyncio.Queue()
        self.out_stream = None
        self._pa = None
        self._loop = None
        self._vol_ctl = AppVolumeController(pid)
        # Capture pipeline (thiết lập lại mỗi phiên trong start_capture)
        self._aec: EchoCanceller | None = None
        self._cap_rs: StreamResampler | None = None
        self._cap_rate = CAPTURE_RATE
        self._cap_channels: int | None = None
        self._capture_ready: asyncio.Event | None = None
        self._warned_fallback = False
        self._process_specific = True

    def _emit(self, kind: str, text: str):
        if self._msg_q:
            self._msg_q.put((kind, text))
        else:
            tag = {"ok": "[✓]", "error": "[LỖI]", "status": "[→]",
                   "transcript": "[Dịch]", "warn": "[!]"}.get(kind, "[i]")
            print(f"{tag} {text}")

    def set_original_volume(self, level: float):
        """Gọi từ GUI khi kéo thanh trượt — chỉnh volume app nguồn ngay lập tức."""
        self.original_vol = level
        self._vol_ctl.set_volume(level)

    def _enqueue_upload(self, pcm16: bytes):
        try:
            self.upload_queue.put_nowait(pcm16)
        except asyncio.QueueFull:
            pass

    def _clear_translated_queue(self):
        try:
            while True:
                self.translated_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    # ── Capture callback (chỉ dùng để GỬI đi dịch, KHÔNG phát lại) ──
    def _on_capture(self, pcm_bytes: bytes, frames: int):
        if self._loop is None or self._cap_rs is None:
            return
        samples = np.frombuffer(pcm_bytes, dtype=np.float32)
        if len(samples) == 0:
            return
        if self._cap_channels:
            channels = self._cap_channels
        elif frames and frames > 0 and len(samples) % frames == 0:
            channels = len(samples) // frames
        else:
            channels = CAPTURE_CHANNELS
        if channels < 1:
            channels = 1
        usable = (len(samples) // channels) * channels
        if usable == 0:
            return
        samples = samples[:usable]
        mono = samples if channels == 1 else samples.reshape(-1, channels).mean(axis=1)

        mono16k = self._cap_rs.process(mono)
        aec = self._aec
        if aec is not None:
            mono16k = aec.process(mono16k, time.monotonic())
        if len(mono16k) == 0:
            return
        pcm16 = (np.clip(mono16k, -1, 1) * 32767).astype(np.int16).tobytes()
        self._loop.call_soon_threadsafe(self._enqueue_upload, pcm16)

    def _setup_capture_mode(self, tap):
        """Thiết lập bộ chuyển tần số mẫu và kiểm tra chế độ chống lặp loa."""
        process_specific, fmt, err = _inspect_tap(tap)

        self._cap_rate, self._cap_channels = CAPTURE_RATE, None
        if isinstance(tap, WinProcessAudioCapture) and tap._fallback_tap is None:
            self._cap_rate = int((fmt or {}).get("sample_rate") or CAPTURE_RATE)
            self._cap_channels = int((fmt or {}).get("channels") or CAPTURE_CHANNELS)
        elif fmt and int(fmt.get("bits_per_sample", 0)) == 32:
            fb = getattr(tap, "_fallback_tap", tap)
            backend = getattr(fb, "_backend", None)
            if backend is not None and getattr(backend, "_converter", None) is not None:
                backend._converter = None
            self._cap_rate = int(fmt.get("sample_rate") or CAPTURE_RATE)
            self._cap_channels = int(fmt.get("channels") or CAPTURE_CHANNELS)
        self._cap_rs = StreamResampler(self._cap_rate, SEND_SAMPLE_RATE)

        self._aec = None
        self._process_specific = process_specific
        mode = getattr(tap, "mode", "process_include" if process_specific else "system_loopback")
        if process_specific:
            if not self._warned_fallback and mode == "process_exclude_self":
                self._warned_fallback = True
                self._emit(
                    "info",
                    "→ Đang thu âm chế độ loại trừ tiến trình AudioTranslator ở cấp Windows Mixer "
                    "(không thu lại giọng dịch).",
                )
            return
        same_device = is_default_output(self.output_device)
        if same_device:
            self._aec = EchoCanceller(on_event=lambda t: self._emit("info", t))
        if self._warned_fallback:
            return
        self._warned_fallback = True
        self._emit("warn", "⚠ Phiên bản Windows này quá cũ (trước Windows 10 2004 / Build 19041) "
                           "→ đang thu âm toàn hệ thống kèm bộ khử vọng + NLP.")
        if err:
            self._emit("info", f"   Chi tiết: {err}")
        if same_device:
            self._emit("warn", "→ Đã bật khử vọng số + cổng triệt phần dư phi tuyến (NLP) "
                               "để chặn lặp giọng dịch.")
            self._emit("info", "   Mẹo: chọn “Loa phát giọng dịch” là tai nghe/thiết bị khác "
                               "để tách hoàn toàn phần cứng.")
        else:
            self._emit("info", "→ Giọng dịch phát ra thiết bị khác loa mặc định → không bị vọng.")

    async def start_capture(self):
        self._loop = asyncio.get_running_loop()
        # Đặt âm lượng app nguồn theo thanh trượt ngay khi bắt đầu
        self._vol_ctl.set_volume(self.original_vol)
        try:
            tap = WinProcessAudioCapture(self.pid, on_data=self._on_capture)
            self._setup_capture_mode(tap)   # TRƯỚC start() → callback đã có AEC nếu cần
            tap.start()
        except Exception as e:
            self._emit("error", f"Không capture được ứng dụng: {e}")
            return
        finally:
            if self._capture_ready is not None:
                self._capture_ready.set()
        self._emit("ok", "Đang lắng nghe ứng dụng…")
        try:
            while not self._stop.is_set():
                if not psutil.pid_exists(self.pid) and not psutil.pid_exists(self.root_pid):
                    self._emit("error", "Ứng dụng đã đóng.")
                    self._stop.set()
                    break
                await asyncio.sleep(0.5)
        finally:
            tap.close()

    async def send_audio(self):
        while not self._stop.is_set():
            try:
                pcm16 = await asyncio.wait_for(self.upload_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self.session:
                try:
                    await self.session.send_realtime_input(
                        audio=types.Blob(
                            data=pcm16,
                            mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                        )
                    )
                except Exception as e:
                    self._emit("error", f"Gửi audio: {e}")
                    raise  # let TaskGroup catch → reconnect

    async def receive_audio(self):
        while not self._stop.is_set():
            if self.session is None:
                await asyncio.sleep(0.05)
                continue
            try:
                async for resp in self.session.receive():
                    if self._stop.is_set():
                        break
                    sc = resp.server_content
                    if sc is None:
                        continue
                    if getattr(sc, "interrupted", False):
                        self._clear_translated_queue()
                    if sc.output_transcription and sc.output_transcription.text:
                        self._emit("transcript", sc.output_transcription.text)
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and inline.data:
                                self.translated_queue.put_nowait(inline.data)
                if not self._stop.is_set():
                    self._emit("info", "Phiên Gemini kết thúc.")
                    raise ConnectionError("session ended")  # → reconnect
            except ConnectionError:
                raise  # propagate to TaskGroup
            except Exception as e:
                self._emit("error", f"Nhận audio: {e}")
                raise  # → reconnect

    def _open_output(self):
        kw = dict(format=pyaudio.paInt16, channels=1, rate=PLAYBACK_RATE,
                  output=True, frames_per_buffer=OUTPUT_FRAME)
        if self.output_device is not None:
            try:
                return self._pa.open(output_device_index=self.output_device, **kw)
            except Exception as e:
                self._emit("error", f"Không mở được loa đã chọn ({e}) → dùng loa mặc định.")
                self.output_device = None
                if not self._process_specific and self._aec is None:
                    self._aec = EchoCanceller(on_event=lambda t: self._emit("info", t))
                    self._emit("warn", "→ Đã bật khử vọng vì giọng dịch phát ra loa đang bị ghi.")
        return self._pa.open(**kw)

    async def play_output(self):
        """CHỈ phát âm thanh đã dịch → không còn vọng do phát lại âm gốc.
        Chế độ khử vọng: phát LIÊN TỤC (im lặng khi không có dữ liệu) để dòng tham
        chiếu khớp đúng nhịp với loa, và đẩy mọi mẫu đã phát vào EchoCanceller."""
        if self._capture_ready is not None:
            try:
                await asyncio.wait_for(self._capture_ready.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        self._pa = pyaudio.PyAudio()
        self.out_stream = self._open_output()
        aec = self._aec
        ref_rs = RefResampler(PLAYBACK_RATE, self._cap_rate, SEND_SAMPLE_RATE) if aec else None
        try:
            buf_cap = int(self.out_stream.get_write_available())
        except Exception:
            buf_cap = 0
        leftover = np.array([], dtype=np.float32)
        loop = asyncio.get_running_loop()
        last_data_time = loop.time()

        async def _write(frame: np.ndarray):
            frame = np.clip(frame, -1.0, 1.0)
            if aec is not None:
                aec.push_ref(ref_rs.process(frame), time.monotonic())
            out = (frame * 32767).astype(np.int16).tobytes()
            await asyncio.to_thread(self.out_stream.write, out)

        def _buffer_low() -> bool:
            if buf_cap <= 2 * OUTPUT_FRAME:
                return True
            try:
                queued = buf_cap - int(self.out_stream.get_write_available())
            except Exception:
                return True
            return queued < 2 * OUTPUT_FRAME

        try:
            while not self._stop.is_set():
                has_new = False
                while not self.translated_queue.empty():
                    try:
                        raw = self.translated_queue.get_nowait()
                        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
                        leftover = np.concatenate([leftover, arr])
                        has_new = True
                    except asyncio.QueueEmpty:
                        break
                if has_new:
                    last_data_time = loop.time()

                time_since = loop.time() - last_data_time
                should_flush = len(leftover) > 0 and time_since > 0.1

                if len(leftover) >= OUTPUT_FRAME:
                    await _write(leftover[:OUTPUT_FRAME])
                    leftover = leftover[OUTPUT_FRAME:]
                    last_data_time = loop.time()
                elif should_flush or (aec is not None and _buffer_low()):
                    # flush phần dư / hoặc (chế độ khử vọng) bơm im lặng trước khi loa cạn
                    frame = np.zeros(OUTPUT_FRAME, dtype=np.float32)
                    frame[:len(leftover)] = leftover
                    leftover = np.array([], dtype=np.float32)
                    await _write(frame)
                    if should_flush:
                        last_data_time = loop.time()
                else:
                    await asyncio.sleep(0.005)
        finally:
            if self.out_stream:
                self.out_stream.stop_stream()
                self.out_stream.close()
                self.out_stream = None
            if self._pa:
                self._pa.terminate()
                self._pa = None

    async def _watch_stop(self):
        while not self._stop.is_set():
            await asyncio.sleep(0.2)
        raise _StopSignal()

    async def run(self):
        config = make_config(self.target_lang)
        # ponytail: auto-reconnect — Gemini Live sessions expire after ~15min
        MAX_RETRIES = 5
        retry = 0
        while not self._stop.is_set():
            self._emit("status", "Đang kết nối Gemini…")
            try:
                async with client.aio.live.connect(model=self.model, config=config) as session:
                    self.session = session
                    retry = 0  # reset on successful connect
                    self._emit("ok", "Đã kết nối. Đang dịch…")
                    _user_stopped = False
                    self._capture_ready = asyncio.Event()
                    self._aec = None
                    try:
                        async with asyncio.TaskGroup() as tg:
                            tg.create_task(self.start_capture())
                            tg.create_task(self.send_audio())
                            tg.create_task(self.receive_audio())
                            tg.create_task(self.play_output())
                            tg.create_task(self._watch_stop())
                    except* _StopSignal:
                        _user_stopped = True
                    except* Exception as eg:
                        for e in eg.exceptions:
                            self._emit("error", f"{type(e).__name__}: {e}")
                        # fall through to reconnect
                    if _user_stopped:
                        break
            except Exception as e:
                if self._stop.is_set():
                    break
                retry += 1
                if retry > MAX_RETRIES:
                    self._emit("error", f"Không thể kết nối sau {MAX_RETRIES} lần: {e}")
                    break
                wait = min(2 ** retry, 16)
                self._emit("info", f"Mất kết nối, tự động kết nối lại sau {wait}s… ({retry}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
                continue
            # Session ended normally (GoAway) — reconnect immediately
            if not self._stop.is_set():
                self.session = None
                self._emit("info", "Phiên hết hạn, đang kết nối lại…")
                await asyncio.sleep(0.5)
                continue
            break
        # Khôi phục âm lượng app nguồn về mức ban đầu
        self._vol_ctl.restore()
        self._emit("stopped", "")



# ────────────────────────────────────────────────────────────────
# Tooltip nhỏ
# ────────────────────────────────────────────────────────────────
class ToolTip:
    def __init__(self, widget, text, bg, fg, border):
        self.widget = widget
        self.text = text
        self.bg, self.fg, self.border = bg, fg, border
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(tw, bg=self.border)
        frame.pack()
        tk.Label(frame, text=self.text, bg=self.bg, fg=self.fg,
                 font=("Segoe UI", 8), justify="left",
                 wraplength=260, padx=10, pady=6).pack(padx=1, pady=1)

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ────────────────────────────────────────────────────────────────
# GUI
# ────────────────────────────────────────────────────────────────
class TranslatorGUI:
    BG = "#10131a"
    CARD = "#1a1f2b"
    BORDER = "#2a3140"
    TEXT = "#e8edf5"
    MUTED = "#8a94a6"
    ACCENT = "#5b8cff"
    ACCENT_HOVER = "#4a7bf0"
    GREEN = "#3ddc84"
    RED = "#ff6b6b"
    YELLOW = "#ffd166"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Audio Translator")
        self.root.geometry("440x660")
        self.root.configure(bg=self.BG)
        self.root.minsize(400, 600)
        # ponytail: set window icon — look next to exe/script
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "icon.ico")
        if not os.path.exists(_icon_path):
            _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(_icon_path):
            try:
                self.root.iconbitmap(_icon_path)
            except tk.TclError:
                pass

        self._thread = None
        self._translator: Translator | None = None
        self._stop_event = threading.Event()
        self._msg_q: thread_queue.Queue = thread_queue.Queue()
        self._sources: list[tuple[int, str, bool]] = []
        self._outputs: list[tuple[int | None, str]] = []

        self._build_ui()
        self._refresh_sources()
        self._poll()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _draw_logo(self, parent):
        """Logo vector đơn giản (không cần file ảnh) — vòng tròn dịch + sóng âm."""
        size = 40
        c = tk.Canvas(parent, width=size, height=size, bg=self.BG,
                      highlightthickness=0)
        c.create_oval(3, 3, size - 3, size - 3, outline=self.ACCENT, width=2)
        # ký tự "文/A" tượng trưng dịch thuật
        c.create_text(size / 2, size / 2, text="文A", fill=self.TEXT,
                      font=("Segoe UI Semibold", 12))
        return c

    def _build_ui(self):
        s = ttk.Style()
        s.theme_use("clam")
        base_font = ("Segoe UI", 10)
        s.configure(".", background=self.BG, foreground=self.TEXT, font=base_font)
        s.configure("Title.TLabel", background=self.BG, foreground=self.TEXT,
                    font=("Segoe UI Semibold", 17))
        s.configure("Sub.TLabel", background=self.BG, foreground=self.MUTED,
                    font=("Segoe UI", 9))
        s.configure("Field.TLabel", background=self.BG, foreground=self.MUTED,
                    font=("Segoe UI", 9))
        s.configure("Status.TLabel", background=self.BG, foreground=self.MUTED,
                    font=("Segoe UI", 9))
        s.configure("TEntry", fieldbackground=self.CARD, foreground=self.TEXT,
                    bordercolor=self.BORDER, insertcolor=self.TEXT,
                    borderwidth=1, padding=8)
        s.configure("TCombobox", fieldbackground=self.CARD, background=self.CARD,
                    foreground=self.TEXT, bordercolor=self.BORDER,
                    arrowcolor=self.MUTED, borderwidth=1, padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", self.CARD)],
              selectbackground=[("readonly", self.CARD)],
              selectforeground=[("readonly", self.TEXT)])
        s.configure("Ghost.TButton", background=self.CARD, foreground=self.TEXT,
                    borderwidth=0, padding=6)
        s.map("Ghost.TButton", background=[("active", self.BORDER)])
        s.configure("Primary.TButton", background=self.ACCENT, foreground="#ffffff",
                    borderwidth=0, padding=(0, 12), font=("Segoe UI Semibold", 11))
        s.map("Primary.TButton", background=[("active", self.ACCENT_HOVER)])
        s.configure("Stop.TButton", background=self.RED, foreground="#ffffff",
                    borderwidth=0, padding=(0, 12), font=("Segoe UI Semibold", 11))
        s.map("Stop.TButton", background=[("active", "#e85555")])

        pad = {"padx": 24}

        # ── Header với logo + avatar ──
        header = tk.Frame(self.root, bg=self.BG)
        header.pack(fill="x", padx=24, pady=(20, 16))
        self._draw_logo(header).pack(side="left", padx=(0, 12))
        htext = tk.Frame(header, bg=self.BG)
        htext.pack(side="left", anchor="w", fill="x", expand=True)
        ttk.Label(htext, text="Audio Translator", style="Title.TLabel").pack(anchor="w")
        ttk.Label(htext, text="Dịch âm thanh real-time bằng Gemini",
                  style="Sub.TLabel").pack(anchor="w")
        # Nút thông tin tác giả
        info_btn = tk.Label(header, text="ℹ", bg=self.BG, fg=self.MUTED,
                            font=("Segoe UI", 16), cursor="hand2")
        info_btn.pack(side="right", padx=(8, 0))
        info_btn.bind("<Button-1>", lambda e: self._show_about())
        info_btn.bind("<Enter>", lambda e: info_btn.configure(fg=self.ACCENT))
        info_btn.bind("<Leave>", lambda e: info_btn.configure(fg=self.MUTED))

        # ── API key + link hướng dẫn ──
        api_head = tk.Frame(self.root, bg=self.BG)
        api_head.pack(fill="x", **pad)
        ttk.Label(api_head, text="API KEY", style="Field.TLabel").pack(side="left")
        help_lbl = tk.Label(api_head, text="Lấy API key ↗", bg=self.BG,
                            fg=self.ACCENT, font=("Segoe UI", 9, "underline"),
                            cursor="hand2")
        help_lbl.pack(side="right")
        help_lbl.bind("<Button-1>", lambda e: webbrowser.open(API_KEY_URL))
        ToolTip(help_lbl,
                "Nhấn để mở Google AI Studio.\n"
                "Đăng nhập → Create API key → sao chép và dán vào ô này.",
                self.CARD, self.TEXT, self.BORDER)

        api_row = tk.Frame(self.root, bg=self.BG)
        api_row.pack(fill="x", padx=24, pady=(4, 0))
        self.api_entry = ttk.Entry(api_row, show="•")
        self.api_entry.pack(side="left", fill="x", expand=True)
        save_btn = ttk.Button(api_row, text="💾", width=3, style="Ghost.TButton",
                              command=self._save_key)
        save_btn.pack(side="left", padx=(6, 0))
        ToolTip(save_btn, "Lưu API key vào máy (nhớ cho lần sau).",
                self.CARD, self.TEXT, self.BORDER)
        clear_btn = ttk.Button(api_row, text="🗑", width=3, style="Ghost.TButton",
                               command=self._clear_key)
        clear_btn.pack(side="left", padx=(4, 0))
        ToolTip(clear_btn, "Xóa API key đã lưu khỏi máy.",
                self.CARD, self.TEXT, self.BORDER)

        ttk.Label(self.root,
                  text="Miễn phí tại Google AI Studio — chỉ cần tài khoản Google.",
                  style="Sub.TLabel").pack(anchor="w", padx=24, pady=(4, 12))
        key = load_api_key()
        if key:
            self.api_entry.insert(0, key)

        # ── Nguồn âm thanh ──
        ttk.Label(self.root, text="NGUỒN ÂM THANH", style="Field.TLabel").pack(anchor="w", **pad)
        src_row = tk.Frame(self.root, bg=self.BG)
        src_row.pack(fill="x", padx=24, pady=(4, 14))
        self.source_combo = ttk.Combobox(src_row, state="readonly")
        self.source_combo.pack(side="left", fill="x", expand=True)
        refresh_btn = ttk.Button(src_row, text="⟳", width=3, style="Ghost.TButton",
                                 command=self._refresh_sources)
        refresh_btn.pack(side="left", padx=(8, 0))
        ToolTip(refresh_btn, "Làm mới danh sách ứng dụng đang phát âm thanh.",
                self.CARD, self.TEXT, self.BORDER)

        # ── Ngôn ngữ ──
        ttk.Label(self.root, text="DỊCH SANG", style="Field.TLabel").pack(anchor="w", **pad)
        self._lang_labels = [n for _, n in LANGUAGE_CHOICES]
        self._lang_codes = [c for c, _ in LANGUAGE_CHOICES]
        self.lang_combo = ttk.Combobox(self.root, state="readonly", values=self._lang_labels)
        try:
            self.lang_combo.current(self._lang_codes.index(TARGET_LANGUAGE))
        except ValueError:
            self.lang_combo.current(0)
        self.lang_combo.pack(fill="x", padx=24, pady=(4, 14))

        # ── Loa phát giọng dịch ──
        out_head = tk.Frame(self.root, bg=self.BG)
        out_head.pack(fill="x", **pad)
        ttk.Label(out_head, text="LOA PHÁT GIỌNG DỊCH", style="Field.TLabel").pack(side="left")
        out_help = tk.Label(out_head, text="?", bg=self.BG, fg=self.MUTED,
                            font=("Segoe UI", 9, "bold"), cursor="question_arrow")
        out_help.pack(side="left", padx=(6, 0))
        ToolTip(out_help,
                "Trên Windows 10 (2004+) và Windows 11, app tự động tách riêng âm thanh "
                "của ứng dụng nguồn ở tầng Windows Mixer (không bị lặp loa).\n"
                "Tùy chọn này giúp bạn phát giọng dịch ra tai nghe/loa phụ hoặc dùng cho "
                "các bản Windows đời cũ trước 2020.",
                self.CARD, self.TEXT, self.BORDER)
        self.out_combo = ttk.Combobox(self.root, state="readonly")
        self.out_combo.pack(fill="x", padx=24, pady=(4, 14))

        # ── Âm lượng gốc ──
        vol_head = tk.Frame(self.root, bg=self.BG)
        vol_head.pack(fill="x", padx=24)
        ttk.Label(vol_head, text="ÂM LƯỢNG GỐC", style="Field.TLabel").pack(side="left")
        self.vol_var = tk.IntVar(value=int(ORIGINAL_VOLUME * 100))
        self.vol_label = ttk.Label(vol_head, text=f"{self.vol_var.get()}%", style="Field.TLabel")
        self.vol_label.pack(side="right")
        self.vol_scale = tk.Scale(
            self.root, from_=0, to=100, orient="horizontal", variable=self.vol_var,
            showvalue=False, bg=self.BG, fg=self.TEXT, troughcolor=self.CARD,
            activebackground=self.ACCENT, highlightthickness=0, sliderrelief="flat",
            bd=0, sliderlength=20, command=self._on_vol,
        )
        self.vol_scale.pack(fill="x", padx=22, pady=(2, 4))
        ttk.Label(self.root,
                  text="Điều chỉnh âm lượng của ứng dụng nguồn (giọng gốc).",
                  style="Sub.TLabel").pack(anchor="w", padx=24, pady=(0, 16))

        # ── Nút chính ──
        self.start_btn = ttk.Button(self.root, text="▶  Bắt đầu dịch",
                                    style="Primary.TButton", command=self._toggle)
        self.start_btn.pack(fill="x", padx=24, pady=(0, 14))

        # ── Footer (status + author) — pack TRƯỚC log để luôn chiếm bottom ──
        footer = tk.Frame(self.root, bg=self.BG)
        footer.pack(side="bottom", fill="x", padx=24, pady=(4, 10))
        self.status_var = tk.StringVar(value="● Sẵn sàng")
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left")
        author = tk.Label(footer, text="by @dieptrader  ✈", bg=self.BG,
                          fg=self.MUTED, font=("Segoe UI", 8), cursor="hand2")
        author.pack(side="right")
        author.bind("<Button-1>", lambda e: webbrowser.open("https://t.me/dieptrader"))

        # ── Log ──
        log_frame = tk.Frame(self.root, bg=self.CARD, highlightthickness=1,
                             highlightbackground=self.BORDER)
        log_frame.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        self.log_text = tk.Text(
            log_frame, bg=self.CARD, fg=self.TEXT, insertbackground=self.TEXT,
            font=("Consolas", 9), relief="flat", borderwidth=0, wrap="word",
            padx=10, pady=8, highlightthickness=0,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_configure("ok", foreground=self.GREEN)
        self.log_text.tag_configure("error", foreground=self.RED)
        self.log_text.tag_configure("status", foreground=self.ACCENT)
        self.log_text.tag_configure("transcript", foreground=self.YELLOW)
        self.log_text.tag_configure("info", foreground=self.MUTED)
        self.log_text.tag_configure("warn", foreground="#ffa94d")
        self.log_text.configure(state="disabled")

        self._log("Chọn ứng dụng đang phát rồi nhấn Bắt đầu.", "info")

    def _on_vol(self, _=None):
        val = self.vol_var.get()
        self.vol_label.configure(text=f"{val}%")
        if self._translator is not None:
            self._translator.set_original_volume(val / 100.0)

    def _save_key(self):
        key = self.api_entry.get().strip()
        if not key:
            self._log("Chưa có key để lưu.", "error")
            return
        save_api_key(key)
        self._log("Đã lưu API key.", "ok")

    def _clear_key(self):
        clear_api_key()
        self.api_entry.delete(0, "end")
        self._log("Đã xóa API key khỏi máy.", "info")

    def _show_about(self):
        """Popup thông tin tác giả."""
        about = tk.Toplevel(self.root)
        about.title("Thông tin")
        about.configure(bg=self.BG)
        about.resizable(False, False)
        about.geometry("320x340")
        about.transient(self.root)
        about.grab_set()
        # Center on parent
        about.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 320) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 340) // 2
        about.geometry(f"+{x}+{y}")

        # Avatar
        try:
            self._about_img = tk.PhotoImage(data=base64.b64decode(AVATAR_B64))
            tk.Label(about, image=self._about_img, bg=self.BG).pack(pady=(24, 12))
        except Exception:
            pass

        # Info text
        tk.Label(about, text="Audio Translator", bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI Semibold", 14)).pack()
        tk.Label(about, text="Phần mềm dịch âm thanh real-time\n"
                             "Powered by Google Gemini",
                 bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9),
                 justify="center").pack(pady=(4, 12))
        tk.Label(about, text="Phát triển bởi", bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack()

        # Author link
        link = tk.Label(about, text="@dieptrader", bg=self.BG, fg=self.ACCENT,
                        font=("Segoe UI Semibold", 12, "underline"),
                        cursor="hand2")
        link.pack(pady=(2, 4))
        link.bind("<Button-1>",
                  lambda e: webbrowser.open("https://t.me/dieptrader"))
        tk.Label(about, text="Telegram  •  Liên hệ & hỗ trợ", bg=self.BG,
                 fg=self.MUTED, font=("Segoe UI", 8)).pack()

        # Close button
        ttk.Button(about, text="Đóng", style="Ghost.TButton",
                   command=about.destroy).pack(pady=(16, 12))

    def _log(self, text: str, tag: str = "info"):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_sources(self):
        self._refresh_outputs()
        self._sources = list_audio_sources()
        labels = []
        for _, app, playing in self._sources:
            labels.append(f"🔊 {app}  ·  đang phát" if playing else app)
        self.source_combo["values"] = labels
        if self._sources:
            self.source_combo.current(0)
            self._log(f"Tìm thấy {len(self._sources)} ứng dụng có âm thanh.", "info")
        else:
            self._log("Không thấy ứng dụng nào có âm thanh. Hãy mở và phát thử.", "info")

    def _refresh_outputs(self):
        cur = self.out_combo.get() if self._outputs else _load_config().get("output_device", "")
        self._outputs = [(None, "Mặc định của Windows")] + list_output_devices()
        names = [n for _, n in self._outputs]
        self.out_combo["values"] = names
        self.out_combo.current(names.index(cur) if cur in names else 0)

    def _selected_output(self) -> int | None:
        i = self.out_combo.current()
        if 0 <= i < len(self._outputs):
            return self._outputs[i][0]
        return None

    def _set_fields_state(self, state: str):
        ro = "readonly" if state == "normal" else "disabled"
        self.api_entry.configure(state=state)
        self.lang_combo.configure(state=ro)
        self.source_combo.configure(state=ro)
        self.out_combo.configure(state=ro)
        # KHÔNG khoá thanh âm lượng — cho phép chỉnh trong lúc dịch

    def _toggle(self):
        if self._thread and self._thread.is_alive():
            self._do_stop()
        else:
            self._do_start()

    def _do_start(self):
        api_key = self.api_entry.get().strip()
        if not api_key:
            self._log("Chưa nhập API Key.", "error")
            return
        idx = self.source_combo.current()
        if idx < 0 or idx >= len(self._sources):
            self._log("Chưa chọn nguồn âm thanh.", "error")
            return

        pid = self._sources[idx][0]
        lang = self._lang_codes[max(self.lang_combo.current(), 0)]
        vol = self.vol_var.get() / 100.0
        out_dev = self._selected_output()
        try:
            cfg = _load_config()
            cfg["output_device"] = "" if out_dev is None else self.out_combo.get()
            _save_config(cfg)
        except Exception:
            pass

        init_client(api_key)
        self._stop_event.clear()

        self._translator = Translator(
            pid, target_lang=lang, original_vol=vol,
            stop_event=self._stop_event, msg_queue=self._msg_q,
            output_device=out_dev,
        )

        def _run():
            try:
                asyncio.run(self._translator.run())
            except Exception as e:
                self._msg_q.put(("error", str(e)))

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

        self.start_btn.configure(text="■  Dừng", style="Stop.TButton")
        self.status_var.set("● Đang dịch…")
        self._set_fields_state("disabled")

    def _do_stop(self):
        self._stop_event.set()
        self.status_var.set("● Đang dừng…")

    def _poll(self):
        while True:
            try:
                kind, text = self._msg_q.get_nowait()
            except thread_queue.Empty:
                break
            if kind == "stopped":
                self.start_btn.configure(text="▶  Bắt đầu dịch", style="Primary.TButton")
                self.status_var.set("● Sẵn sàng")
                self._set_fields_state("normal")
                self._translator = None
                self._log("Đã dừng.", "info")
            else:
                self._log(text, kind)
                if kind == "ok":
                    self.status_var.set(f"● {text}")
        self.root.after(100, self._poll)

    def _on_close(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=3)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────
def choose_audio_pid() -> int | None:
    srcs = list_audio_sources()
    if not srcs:
        print("Không tìm thấy ứng dụng có âm thanh.")
        return None
    print("\n=== Ứng dụng có âm thanh ===")
    for i, (pid, app, playing) in enumerate(srcs):
        print(f"  [{i}] {app}{' (đang phát)' if playing else ''}")
    while True:
        c = input("\nChọn số (q=thoát): ").strip()
        if c.lower() == "q":
            return None
        try:
            idx = int(c)
            if 0 <= idx < len(srcs):
                return srcs[idx][0]
        except ValueError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Live Audio Translator")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--lang", default=TARGET_LANGUAGE)
    parser.add_argument("--original-volume", type=float, default=ORIGINAL_VOLUME)
    parser.add_argument("--pid", type=int, default=None)
    args = parser.parse_args()

    if args.cli:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("[LỖI] Chưa set GEMINI_API_KEY.")
            sys.exit(1)
        init_client(api_key)
        pid = args.pid or choose_audio_pid()
        if not pid:
            return
        print(f"\n[→] Dịch sang '{args.lang}'…")
        t = Translator(pid, target_lang=args.lang, original_vol=args.original_volume)
        asyncio.run(t.run())
    else:
        TranslatorGUI().run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
