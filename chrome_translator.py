#!/usr/bin/env python3
"""
Live Audio Translator
Dịch âm thanh real-time từ bất kỳ ứng dụng nào (trình duyệt, media player...)
sang tiếng Việt qua Gemini Live API.

Kiến trúc chống vọng (v2):
  - App CHỈ phát âm thanh đã dịch ra loa.
  - Âm thanh gốc vẫn phát tự nhiên từ ứng dụng nguồn (không phát lại → không vọng).
  - Thanh "âm lượng gốc" điều khiển trực tiếp volume của app nguồn qua Windows.

Đóng gói .exe (chạy trên PC khác, không cần Python):
    pip install pyinstaller google-genai numpy psutil pyaudio pycaw comtypes proctap
    pyinstaller --noconfirm --onefile --windowed --name "AudioTranslator" ^
        --collect-all proctap --hidden-import comtypes --hidden-import pycaw app.py
"""

import os
import sys
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
    """Trả (pid, app_name, is_playing). Gộp trùng tên, app đang phát lên đầu."""
    known = set(_APP_NAMES.keys())
    active_pids = _get_active_audio_pids()

    raw: list[tuple[int, str, bool]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pid = proc.info["pid"]
            name = (proc.info["name"] or "").lower()
            if active_pids is not None:
                if pid not in active_pids:
                    continue
                is_playing = active_pids[pid]
            else:
                if name not in known:
                    continue
                is_playing = False
            raw.append((pid, _pretty_app_name(name), is_playing))
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


class AppVolumeController:
    """Điều khiển âm lượng của tất cả session thuộc 1 PID (qua pycaw).
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
        for session in AudioUtilities.GetAllSessions():
            try:
                if session.ProcessId != self.pid:
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
    ):
        self.pid = pid
        self.target_lang = target_lang
        self.original_vol = original_vol
        self.model = model
        self._stop = stop_event or threading.Event()
        self._msg_q = msg_queue

        self.session = None
        self.upload_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.translated_queue: asyncio.Queue = asyncio.Queue()
        self.out_stream = None
        self._pa = None
        self._loop = None
        self._vol_ctl = AppVolumeController(pid)

    def _emit(self, kind: str, text: str):
        if self._msg_q:
            self._msg_q.put((kind, text))
        else:
            tag = {"ok": "[✓]", "error": "[LỖI]", "status": "[→]",
                   "transcript": "[Dịch]"}.get(kind, "[i]")
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

    # ── ProcTap callback (chỉ dùng để GỬI đi dịch, KHÔNG phát lại) ──
    def _on_capture(self, pcm_bytes: bytes, frames: int):
        if self._loop is None:
            return
        samples = np.frombuffer(pcm_bytes, dtype=np.float32)
        if len(samples) == 0:
            return
        if frames and frames > 0 and len(samples) % frames == 0:
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

        mono16k = resample_mono(mono, CAPTURE_RATE, SEND_SAMPLE_RATE)
        pcm16 = (np.clip(mono16k, -1, 1) * 32767).astype(np.int16).tobytes()
        self._loop.call_soon_threadsafe(self._enqueue_upload, pcm16)

    async def start_capture(self):
        from proctap import ProcessAudioCapture

        self._loop = asyncio.get_running_loop()
        # Đặt âm lượng app nguồn theo thanh trượt ngay khi bắt đầu
        self._vol_ctl.set_volume(self.original_vol)
        try:
            tap = ProcessAudioCapture(self.pid, on_data=self._on_capture)
            tap.start()
        except Exception as e:
            self._emit("error", f"Không capture được ứng dụng: {e}")
            return
        self._emit("ok", "Đang lắng nghe ứng dụng…")
        try:
            while not self._stop.is_set():
                if not psutil.pid_exists(self.pid):
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

    async def play_output(self):
        """CHỈ phát âm thanh đã dịch → không còn vọng do phát lại âm gốc."""
        self._pa = pyaudio.PyAudio()
        self.out_stream = self._pa.open(
            format=pyaudio.paInt16, channels=1, rate=PLAYBACK_RATE,
            output=True, frames_per_buffer=OUTPUT_FRAME,
        )
        leftover = np.array([], dtype=np.float32)
        loop = asyncio.get_running_loop()
        last_data_time = loop.time()

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

                if len(leftover) >= OUTPUT_FRAME or should_flush:
                    if len(leftover) >= OUTPUT_FRAME:
                        frame = leftover[:OUTPUT_FRAME]
                        leftover = leftover[OUTPUT_FRAME:]
                    else:
                        frame = np.zeros(OUTPUT_FRAME, dtype=np.float32)
                        frame[:len(leftover)] = leftover
                        leftover = np.array([], dtype=np.float32)
                    out = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    await asyncio.to_thread(self.out_stream.write, out)
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
        self.root.geometry("440x600")
        self.root.configure(bg=self.BG)
        self.root.minsize(400, 540)
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

    def _set_fields_state(self, state: str):
        ro = "readonly" if state == "normal" else "disabled"
        self.api_entry.configure(state=state)
        self.lang_combo.configure(state=ro)
        self.source_combo.configure(state=ro)
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

        init_client(api_key)
        self._stop_event.clear()

        self._translator = Translator(
            pid, target_lang=lang, original_vol=vol,
            stop_event=self._stop_event, msg_queue=self._msg_q,
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
