from __future__ import annotations

import csv
import ctypes
import json
import queue
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import psutil
import tkinter as tk
from PIL import Image, ImageTk
from rapidocr_onnxruntime import RapidOCR
from tkinter import messagebox, ttk
import win32gui
import win32process
import win32ui


PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
CHINESE_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff]")
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else RESOURCE_DIR
)
OUTPUT_DIR = APP_DIR / "outputs"
CONFIG_PATH = APP_DIR / "config.json"
CARD_MAP_FILENAMES = ("card_image_text_map.csv", "card_image_text_map.json")
USER32 = ctypes.windll.user32
SHCORE = getattr(ctypes.windll, "shcore", None)
DEFAULT_TARGET_KEYWORD = "Gremlins Inc|Gremlins, Inc|GremlinsInc"
ROI_MODE_MANUAL = "manual"
ROI_MODE_BOTTOM_CARD = "bottom_card"
DEFAULT_BOTTOM_CARD_RELATIVE_ROI = {
    "x": 0.25,
    "y": 0.84,
    "width": 0.52,
    "height": 0.16,
}
BOTTOM_CARD_NOISE_PHRASES = (
    "使用卡牌时会发生什么",
    "选择表现",
    "请选择",
)


@dataclass(slots=True)
class Roi:
    x: int = 0
    y: int = 0
    width: int = 640
    height: int = 240


@dataclass(slots=True)
class RelativeRoi:
    x: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["x"]
    y: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["y"]
    width: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["width"]
    height: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["height"]


@dataclass(slots=True)
class AppConfig:
    roi: Roi = field(default_factory=Roi)
    roi_mode: str = ROI_MODE_MANUAL
    bottom_card_roi: RelativeRoi = field(default_factory=RelativeRoi)
    interval_seconds: float = 1.0
    count_repeated_frames: bool = False
    selected_hwnd: int | None = None
    target_keyword: str = DEFAULT_TARGET_KEYWORD
    auto_detect_enabled: bool = True
    auto_lock_detected: bool = True


@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    class_name: str

    @property
    def label(self) -> str:
        return f"{self.title} [PID {self.pid}] ({self.class_name})"


@dataclass(slots=True)
class CaptureRecord:
    timestamp: str
    text: str
    items_counted: dict[str, int]


@dataclass(slots=True)
class CardInfo:
    name: str
    image_path: Path | None
    description: str = ""
    flavor: str = ""
    card_type: str = ""


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        return AppConfig()
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        roi_data = payload.get("roi", {})
        roi = Roi(
            x=int(roi_data.get("x", 0)),
            y=int(roi_data.get("y", 0)),
            width=int(roi_data.get("width", 640)),
            height=int(roi_data.get("height", 240)),
        )
        relative_data = payload.get("bottom_card_roi", {})
        bottom_card_roi = RelativeRoi(
            x=float(relative_data.get("x", DEFAULT_BOTTOM_CARD_RELATIVE_ROI["x"])),
            y=float(relative_data.get("y", DEFAULT_BOTTOM_CARD_RELATIVE_ROI["y"])),
            width=float(relative_data.get("width", DEFAULT_BOTTOM_CARD_RELATIVE_ROI["width"])),
            height=float(relative_data.get("height", DEFAULT_BOTTOM_CARD_RELATIVE_ROI["height"])),
        )
        return AppConfig(
            roi=roi,
            roi_mode=str(payload.get("roi_mode", ROI_MODE_MANUAL)),
            bottom_card_roi=bottom_card_roi,
            interval_seconds=float(payload.get("interval_seconds", 1.0)),
            count_repeated_frames=bool(payload.get("count_repeated_frames", False)),
            selected_hwnd=(
                int(payload["selected_hwnd"]) if payload.get("selected_hwnd") else None
            ),
            target_keyword=str(payload.get("target_keyword", DEFAULT_TARGET_KEYWORD)).replace(
                "Gremlins_lnc", "Gremlins Inc"
            ),
            auto_detect_enabled=bool(payload.get("auto_detect_enabled", True)),
            auto_lock_detected=bool(payload.get("auto_lock_detected", True)),
        )
    except (ValueError, OSError, TypeError):
        return AppConfig()


def save_config(config: AppConfig) -> None:
    CONFIG_PATH.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def enum_windows() -> list[WindowInfo]:
    windows: list[WindowInfo] = []

    def callback(hwnd: int, _: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        class_name = win32gui.GetClassName(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        windows.append(WindowInfo(hwnd=hwnd, title=title, pid=pid, class_name=class_name))
        return True

    win32gui.EnumWindows(callback, None)
    windows.sort(key=lambda item: item.title.lower())
    return windows


def split_keywords(raw_text: str) -> list[str]:
    return [part.strip() for part in raw_text.split("|") if part.strip()]


def normalize_probe_text(raw_text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", raw_text).lower()


def get_process_names(pid: int) -> list[str]:
    names = []
    try:
        process = psutil.Process(pid)
        names.append(process.name())
        try:
            names.append(Path(process.exe()).name)
        except (psutil.Error, OSError):
            pass
    except (psutil.Error, OSError):
        pass
    return [name for name in names if name]


def window_matches_keyword(window: WindowInfo, keyword: str) -> bool:
    probes = [window.title, window.class_name, *get_process_names(window.pid)]
    normalized_probes = [normalize_probe_text(item) for item in probes]
    for alias in split_keywords(keyword):
        alias_norm = normalize_probe_text(alias)
        if not alias_norm:
            continue
        for probe in normalized_probes:
            if alias_norm in probe or probe in alias_norm:
                return True
    return False


def find_target_window(keyword: str) -> WindowInfo | None:
    for window in enum_windows():
        if window_matches_keyword(window, keyword):
            return window
    return None


def is_window_alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(win32gui.IsWindow(hwnd))


def enable_dpi_awareness() -> None:
    try:
        USER32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        if SHCORE:
            SHCORE.SetProcessDpiAwareness(2)
            return
    except Exception:
        pass
    try:
        USER32.SetProcessDPIAware()
    except Exception:
        pass


def looks_like_blank_capture(image: np.ndarray) -> bool:
    if image.size == 0:
        return True
    return float(image.std()) < 1.0


def capture_window_client(hwnd: int) -> np.ndarray:
    if not is_window_alive(hwnd):
        raise RuntimeError("目标窗口不存在或已经关闭。")
    if win32gui.IsIconic(hwnd):
        raise RuntimeError("目标窗口已最小化，后台抓图通常会失败。请恢复窗口后继续。")

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("目标窗口的客户区大小无效。")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    save_bitmap = win32ui.CreateBitmap()

    try:
        save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(save_bitmap)

        result = 0
        for flags in (
            PW_CLIENTONLY | PW_RENDERFULLCONTENT,
            PW_CLIENTONLY,
            PW_RENDERFULLCONTENT,
            0,
        ):
            result = USER32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
            if result == 1:
                break

        if result != 1:
            try:
                save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), SRCCOPY)
            except Exception as exc:
                raise RuntimeError(
                    "后台抓图失败：这个游戏窗口当前可能不支持 PrintWindow 后台渲染。"
                ) from exc

        bitmap_info = save_bitmap.GetInfo()
        bitmap_bytes = save_bitmap.GetBitmapBits(True)
        image = np.frombuffer(bitmap_bytes, dtype=np.uint8)
        channel_count = max(1, len(bitmap_bytes) // (width * height))
        if channel_count == 4:
            image = image.reshape((height, width, 4))
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
        elif channel_count == 3:
            image = image.reshape((height, width, 3))
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        else:
            raise RuntimeError(f"不支持的位图通道数: {channel_count}")

        if bitmap_info["bmWidth"] != width or bitmap_info["bmHeight"] != height:
            image = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
        if looks_like_blank_capture(image):
            raise RuntimeError(
                "抓图结果疑似空白。游戏可能在后台/最小化时停止渲染，请改用窗口化或无边框窗口模式。"
            )
        return image
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def clamp_roi(image: np.ndarray, roi: Roi) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    height, width = image.shape[:2]
    x = max(0, min(roi.x, width - 1))
    y = max(0, min(roi.y, height - 1))
    max_width = max(1, width - x)
    max_height = max(1, height - y)
    roi_width = max(1, min(roi.width, max_width))
    roi_height = max(1, min(roi.height, max_height))
    cropped = image[y : y + roi_height, x : x + roi_width]
    return cropped, (x, y, roi_width, roi_height)


def relative_roi_to_absolute(image: np.ndarray, roi: RelativeRoi) -> Roi:
    height, width = image.shape[:2]
    x = round(width * roi.x)
    y = round(height * roi.y)
    roi_width = round(width * roi.width)
    roi_height = round(height * roi.height)
    return Roi(x=x, y=y, width=roi_width, height=roi_height)


def resolve_roi(config: AppConfig, image: np.ndarray) -> Roi:
    if config.roi_mode == ROI_MODE_BOTTOM_CARD:
        return relative_roi_to_absolute(image, config.bottom_card_roi)
    return config.roi


def prepare_ocr_image(image: np.ndarray) -> np.ndarray:
    scaled = cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_RGB2GRAY)
    sharpened = cv2.addWeighted(gray, 1.6, cv2.GaussianBlur(gray, (0, 0), 1.2), -0.6, 0)
    return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2RGB)


def normalize_text(lines: list[str]) -> str:
    return "\n".join(line.strip() for line in lines if line.strip())


def clean_card_area_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9+，。！？、：:（）()一-龥]", "", line).strip()
        if not cleaned:
            continue
        if any(phrase in cleaned for phrase in BOTTOM_CARD_NOISE_PHRASES):
            continue
        if not CHINESE_CHAR_PATTERN.search(cleaned):
            continue
        lines.append(cleaned)
    return "\n".join(lines)


def normalize_card_match_text(raw_text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", raw_text).lower()


def iter_card_map_paths() -> list[Path]:
    paths: list[Path] = []
    for base_dir in (APP_DIR, RESOURCE_DIR):
        for filename in CARD_MAP_FILENAMES:
            path = base_dir / filename
            if path not in paths:
                paths.append(path)
    return paths


def read_card_map_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as file_obj:
            return list(csv.DictReader(file_obj))
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return [row for row in payload if isinstance(row, dict)]


def resolve_resource_path(raw_path: str) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for base_dir in (APP_DIR, RESOURCE_DIR):
        path = base_dir / raw_path
        if path.exists():
            return path
    return None


def load_card_catalog() -> dict[str, CardInfo]:
    for path in iter_card_map_paths():
        if not path.exists():
            continue
        catalog: dict[str, CardInfo] = {}
        for row in read_card_map_rows(path):
            name = (row.get("zh_name") or "").strip()
            if not name or name in catalog:
                continue
            image_path = resolve_resource_path((row.get("image_path") or "").strip())
            catalog[name] = CardInfo(
                name=name,
                image_path=image_path,
                description=(row.get("zh_description") or "").strip(),
                flavor=(row.get("zh_flavor") or "").strip(),
                card_type=(row.get("card_type_zh") or "").strip(),
            )
        if catalog:
            return catalog
    return {}


def load_card_names() -> list[str]:
    return sorted(load_card_catalog(), key=lambda item: (-len(item), item))


def extract_card_name_items(text: str, card_names: list[str]) -> Counter[str]:
    normalized_text = normalize_card_match_text(text)
    if not normalized_text:
        return Counter()

    items: Counter[str] = Counter()
    for card_name in card_names:
        normalized_name = normalize_card_match_text(card_name)
        if normalized_name and normalized_name in normalized_text:
            items[card_name] += 1
    return items


def card_match_signature(items: Counter[str], text: str) -> str:
    if not items:
        return ""
    names_part = "|".join(f"{name}:{count}" for name, count in sorted(items.items()))
    return f"{names_part}#{normalize_card_match_text(text)}"


class OcrEngine:
    def __init__(self) -> None:
        self._engine = RapidOCR()

    def read_text(self, image: np.ndarray) -> tuple[str, list[str]]:
        result, _ = self._engine(image)
        if not result:
            return "", []
        lines = [str(item[1]) for item in result if len(item) >= 2]
        return normalize_text(lines), lines


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        hwnd: int,
        config: AppConfig,
        ocr_engine: OcrEngine,
        card_names: list[str],
        events: queue.Queue[dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.hwnd = hwnd
        self.config = config
        self.ocr_engine = ocr_engine
        self.card_names = card_names
        self.events = events
        self.stop_event = stop_event
        self.last_counted_signature = ""
        self.current_seen_signature = ""
        self.counter: Counter[str] = Counter()
        self.records: list[CaptureRecord] = []
        self.failure_count = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            started_at = time.perf_counter()
            try:
                frame = capture_window_client(self.hwnd)
                active_roi = resolve_roi(self.config, frame)
                roi_frame, roi_bounds = clamp_roi(frame, active_roi)
                text, _ = self.ocr_engine.read_text(prepare_ocr_image(roi_frame))
                if self.config.roi_mode == ROI_MODE_BOTTOM_CARD:
                    text = clean_card_area_text(text)
                matched_items = extract_card_name_items(text, self.card_names)
                signature = card_match_signature(matched_items, text)
                should_count = bool(matched_items)
                duplicate = should_count and signature == self.current_seen_signature
                items_counted: Counter[str] = Counter()
                if should_count and not duplicate:
                    items_counted = matched_items
                    self.counter.update(items_counted)
                    record = CaptureRecord(
                        timestamp=datetime.now().isoformat(timespec="seconds"),
                        text=text,
                        items_counted=dict(items_counted),
                    )
                    self.records.append(record)
                    self.last_counted_signature = signature

                self.current_seen_signature = signature

                self.events.put(
                    {
                        "type": "capture",
                        "text": text,
                        "roi_bounds": roi_bounds,
                        "frame_shape": frame.shape[:2],
                        "items_counted": dict(items_counted),
                        "counter": dict(self.counter),
                        "duplicate": duplicate,
                        "record_count": len(self.records),
                    }
                )
                self.failure_count = 0
            except Exception as exc:
                self.failure_count += 1
                self.events.put(
                    {
                        "type": "capture_error",
                        "message": str(exc),
                        "failure_count": self.failure_count,
                        "counter": dict(self.counter),
                        "record_count": len(self.records),
                    }
                )

            elapsed = time.perf_counter() - started_at
            sleep_for = max(0.1, self.config.interval_seconds - elapsed)
            if self.stop_event.wait(sleep_for):
                break


class GremlinsAssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Gremlins Window OCR Counter")
        self.root.geometry("1080x720")

        self.config = load_config()
        OUTPUT_DIR.mkdir(exist_ok=True)

        self.window_infos: list[WindowInfo] = []
        self.window_labels = tk.StringVar(value=[])
        self.selected_window_index = tk.IntVar(value=0)
        self.interval_var = tk.StringVar(value=str(self.config.interval_seconds))
        self.roi_mode_var = tk.StringVar(value=self.config.roi_mode)
        self.target_keyword_var = tk.StringVar(value=self.config.target_keyword)
        self.roi_x_var = tk.StringVar(value=str(self.config.roi.x))
        self.roi_y_var = tk.StringVar(value=str(self.config.roi.y))
        self.roi_width_var = tk.StringVar(value=str(self.config.roi.width))
        self.roi_height_var = tk.StringVar(value=str(self.config.roi.height))
        self.count_repeated_frames_var = tk.BooleanVar(
            value=self.config.count_repeated_frames
        )
        self.auto_detect_var = tk.BooleanVar(value=self.config.auto_detect_enabled)
        self.auto_lock_var = tk.BooleanVar(value=self.config.auto_lock_detected)
        self.status_var = tk.StringVar(value="准备就绪")
        self.locked_window_var = tk.StringVar(value="尚未锁定窗口")
        self.detected_status_var = tk.StringVar(value="自动检测未开始")
        self.summary_target_var = tk.StringVar(value="未检测到目标程序")
        self.summary_lock_var = tk.StringVar(value="未锁定")
        self.summary_monitor_var = tk.StringVar(value="未启动")
        self.summary_ocr_var = tk.StringVar(value="暂无识别结果")
        self.summary_count_var = tk.StringVar(value="累计 0 次卡牌名称")
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker: CaptureWorker | None = None
        self.ocr_engine = OcrEngine()
        self.card_catalog = load_card_catalog()
        self.card_names = sorted(self.card_catalog, key=lambda item: (-len(item), item))
        self.counter: Counter[str] = Counter()
        self.records: list[CaptureRecord] = []
        self.last_text = ""
        self.overlay_button: tk.Toplevel | None = None
        self.used_cards_window: tk.Toplevel | None = None
        self.card_photo_cache: dict[str, ImageTk.PhotoImage] = {}

        self._build_ui()
        self.refresh_windows(initial=True)
        self.root.after(200, self._drain_events)
        self.root.after(1000, self._poll_target_application)
        self.root.after(1200, self._update_overlay_button)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        top_frame = ttk.LabelFrame(main, text="1. 选择并锁定窗口", padding=12)
        top_frame.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        top_frame.columnconfigure(2, weight=1)

        ttk.Button(
            top_frame,
            text="手动选择并锁定窗口",
            command=self.open_window_picker,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(top_frame, text="单次识别", command=self.capture_once).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Label(top_frame, textvariable=self.locked_window_var).grid(
            row=0, column=2, sticky="e", padx=(12, 0)
        )

        detect_frame = ttk.Frame(top_frame)
        detect_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        detect_frame.columnconfigure(1, weight=1)

        ttk.Label(detect_frame, text="目标关键词").grid(row=0, column=0, sticky="w")
        ttk.Entry(detect_frame, textvariable=self.target_keyword_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 8)
        )
        ttk.Checkbutton(
            detect_frame,
            text="自动检测是否开启",
            variable=self.auto_detect_var,
        ).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(
            detect_frame,
            text="自动锁定检测到的窗口",
            variable=self.auto_lock_var,
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))
        ttk.Label(detect_frame, textvariable=self.detected_status_var).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        overview_frame = ttk.LabelFrame(top_frame, text="现状总览", padding=10)
        overview_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        overview_frame.columnconfigure(1, weight=1)
        overview_frame.columnconfigure(3, weight=1)

        ttk.Label(overview_frame, text="目标程序").grid(row=0, column=0, sticky="w")
        ttk.Label(overview_frame, textvariable=self.summary_target_var).grid(
            row=0, column=1, sticky="w", padx=(8, 16)
        )
        ttk.Label(overview_frame, text="锁定窗口").grid(row=0, column=2, sticky="w")
        ttk.Label(overview_frame, textvariable=self.summary_lock_var).grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )
        ttk.Label(overview_frame, text="监控状态").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(overview_frame, textvariable=self.summary_monitor_var).grid(
            row=1, column=1, sticky="w", padx=(8, 16), pady=(6, 0)
        )
        ttk.Label(overview_frame, text="最近 OCR").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Label(overview_frame, textvariable=self.summary_ocr_var).grid(
            row=1, column=3, sticky="w", padx=(8, 0), pady=(6, 0)
        )
        ttk.Label(overview_frame, text="累计计数").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(overview_frame, textvariable=self.summary_count_var).grid(
            row=2, column=1, sticky="w", padx=(8, 16), pady=(6, 0)
        )

        control_frame = ttk.LabelFrame(main, text="2. 采集参数", padding=12)
        control_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        control_frame.columnconfigure(1, weight=1)

        ttk.Label(control_frame, text="采集间隔(秒)").grid(row=0, column=0, sticky="w")
        ttk.Entry(control_frame, textvariable=self.interval_var).grid(
            row=0, column=1, sticky="ew", pady=(0, 8)
        )

        roi_mode_frame = ttk.Frame(control_frame)
        roi_mode_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(roi_mode_frame, text="识别区域").pack(side=tk.LEFT)
        ttk.Radiobutton(
            roi_mode_frame,
            text="下方卡牌区(按比例)",
            variable=self.roi_mode_var,
            value=ROI_MODE_BOTTOM_CARD,
            command=self.apply_bottom_card_preset,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(
            roi_mode_frame,
            text="手动 ROI",
            variable=self.roi_mode_var,
            value=ROI_MODE_MANUAL,
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(control_frame, text="ROI X").grid(row=2, column=0, sticky="w")
        ttk.Entry(control_frame, textvariable=self.roi_x_var).grid(
            row=2, column=1, sticky="ew", pady=(0, 8)
        )
        ttk.Label(control_frame, text="ROI Y").grid(row=3, column=0, sticky="w")
        ttk.Entry(control_frame, textvariable=self.roi_y_var).grid(
            row=3, column=1, sticky="ew", pady=(0, 8)
        )
        ttk.Label(control_frame, text="ROI 宽").grid(row=4, column=0, sticky="w")
        ttk.Entry(control_frame, textvariable=self.roi_width_var).grid(
            row=4, column=1, sticky="ew", pady=(0, 8)
        )
        ttk.Label(control_frame, text="ROI 高").grid(row=5, column=0, sticky="w")
        ttk.Entry(control_frame, textvariable=self.roi_height_var).grid(
            row=5, column=1, sticky="ew", pady=(0, 8)
        )

        ttk.Label(
            control_frame,
            text="同一张卡牌画面连续出现时只累计一次",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 8))

        button_row = ttk.Frame(control_frame)
        button_row.grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Button(button_row, text="开始监控", command=self.start_monitoring).pack(
            side=tk.LEFT
        )
        ttk.Button(button_row, text="停止监控", command=self.stop_monitoring).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(button_row, text="导出结果", command=self.export_results).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(button_row, text="清空计数", command=self.reset_counts).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(button_row, text="预览识别范围", command=self.preview_roi).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        result_frame = ttk.LabelFrame(main, text="3. OCR 结果与统计", padding=12)
        result_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(1, weight=1)
        result_frame.rowconfigure(3, weight=1)

        ttk.Label(result_frame, text="最近一次 OCR 文本").grid(row=0, column=0, sticky="w")
        self.text_output = tk.Text(result_frame, height=12, wrap="word")
        self.text_output.grid(row=1, column=0, sticky="nsew")

        ttk.Label(result_frame, text="卡牌名称出现次数").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.counter_output = tk.Text(result_frame, height=12, wrap="word")
        self.counter_output.grid(row=3, column=0, sticky="nsew")

        status_bar = ttk.Label(main, textvariable=self.status_var, relief=tk.SUNKEN, anchor="w")
        status_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    def refresh_windows(self, initial: bool = False) -> None:
        current_hwnd = self.config.selected_hwnd
        self.window_infos = enum_windows()

        selected_index = 0
        if current_hwnd:
            for idx, item in enumerate(self.window_infos):
                if item.hwnd == current_hwnd:
                    selected_index = idx
                    break
        if self.window_infos:
            self.selected_window_index.set(selected_index)
        if not initial:
            self.status_var.set(f"已刷新窗口列表，共 {len(self.window_infos)} 个可见窗口")
        self.summary_target_var.set(
            f"已发现 {len(self.window_infos)} 个可见窗口"
        )

    def open_window_picker(self) -> None:
        self.refresh_windows()
        if not self.window_infos:
            messagebox.showwarning("没有可用窗口", "当前没有找到可见窗口。")
            return

        picker = tk.Toplevel(self.root)
        picker.title("手动选择窗口")
        picker.geometry("760x420")
        picker.transient(self.root)
        picker.grab_set()

        picker_frame = ttk.Frame(picker, padding=12)
        picker_frame.pack(fill=tk.BOTH, expand=True)
        picker_frame.columnconfigure(0, weight=1)
        picker_frame.rowconfigure(1, weight=1)

        ttk.Label(
            picker_frame,
            text="选择要锁定的游戏窗口。锁定后即使切到其他画面，也会继续按这个窗口句柄采集。",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        listbox = tk.Listbox(picker_frame, height=14, exportselection=False)
        listbox.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(picker_frame, orient=tk.VERTICAL, command=listbox.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        listbox.configure(yscrollcommand=scrollbar.set)

        for item in self.window_infos:
            listbox.insert(tk.END, item.label)
        selected_index = self.selected_window_index.get()
        if 0 <= selected_index < len(self.window_infos):
            listbox.selection_set(selected_index)
            listbox.activate(selected_index)

        button_row = ttk.Frame(picker_frame)
        button_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        def refresh_picker() -> None:
            self.refresh_windows()
            listbox.delete(0, tk.END)
            for item in self.window_infos:
                listbox.insert(tk.END, item.label)
            if self.window_infos:
                listbox.selection_clear(0, tk.END)
                listbox.selection_set(0)
                listbox.activate(0)
                self.selected_window_index.set(0)

        def lock_from_picker() -> None:
            selection = listbox.curselection()
            if not selection:
                messagebox.showwarning("未选择窗口", "请先在列表中选择一个窗口。")
                return
            index = selection[0]
            if index >= len(self.window_infos):
                messagebox.showwarning("窗口列表已变化", "请刷新后重新选择窗口。")
                return
            self.selected_window_index.set(index)
            self.lock_window(self.window_infos[index])
            picker.destroy()

        listbox.bind("<Double-Button-1>", lambda _: lock_from_picker())
        ttk.Button(button_row, text="刷新列表", command=refresh_picker).pack(side=tk.LEFT)
        ttk.Button(button_row, text="锁定选中窗口", command=lock_from_picker).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(button_row, text="取消", command=picker.destroy).pack(
            side=tk.RIGHT
        )

    def _current_config(self) -> AppConfig:
        try:
            roi = Roi(
                x=int(self.roi_x_var.get()),
                y=int(self.roi_y_var.get()),
                width=int(self.roi_width_var.get()),
                height=int(self.roi_height_var.get()),
            )
            interval_seconds = max(0.2, float(self.interval_var.get()))
        except ValueError as exc:
            raise ValueError("ROI 和采集间隔必须是有效数字。") from exc

        return AppConfig(
            roi=roi,
            roi_mode=self.roi_mode_var.get(),
            bottom_card_roi=self.config.bottom_card_roi,
            interval_seconds=interval_seconds,
            count_repeated_frames=self.count_repeated_frames_var.get(),
            selected_hwnd=self.config.selected_hwnd,
            target_keyword=self.target_keyword_var.get().strip(),
            auto_detect_enabled=self.auto_detect_var.get(),
            auto_lock_detected=self.auto_lock_var.get(),
        )

    def apply_bottom_card_preset(self) -> None:
        self.roi_mode_var.set(ROI_MODE_BOTTOM_CARD)
        self.config.bottom_card_roi = RelativeRoi()
        if not self.config.selected_hwnd or not is_window_alive(self.config.selected_hwnd):
            self.status_var.set("已启用下方卡牌区比例识别。锁定窗口后会自动按窗口大小换算 ROI。")
            return
        try:
            left, top, right, bottom = win32gui.GetClientRect(self.config.selected_hwnd)
            width, height = right - left, bottom - top
            roi = relative_roi_to_absolute(
                np.zeros((height, width, 3), dtype=np.uint8),
                self.config.bottom_card_roi,
            )
            self.roi_x_var.set(str(roi.x))
            self.roi_y_var.set(str(roi.y))
            self.roi_width_var.set(str(roi.width))
            self.roi_height_var.set(str(roi.height))
            self.status_var.set(
                f"已启用下方卡牌区比例识别。当前窗口换算 ROI=({roi.x}, {roi.y}, {roi.width}, {roi.height})"
            )
        except Exception as exc:
            self.status_var.set(f"已启用下方卡牌区比例识别，但暂时无法读取窗口大小: {exc}")

    def lock_window(self, window_info: WindowInfo) -> None:
        self.config.selected_hwnd = window_info.hwnd
        self.config = self._current_config()
        self.config.selected_hwnd = window_info.hwnd
        save_config(self.config)
        self.locked_window_var.set(
            f"已锁定: {window_info.title} [PID {window_info.pid}]"
        )
        self.summary_lock_var.set(window_info.title)
        if self.roi_mode_var.get() == ROI_MODE_BOTTOM_CARD:
            self.apply_bottom_card_preset()
        self.status_var.set("窗口已锁定，接下来可以测试 OCR 或开始监控。")
        self.show_overlay_button()

    def _target_window_rect(self) -> tuple[int, int, int, int] | None:
        hwnd = self.config.selected_hwnd
        if not hwnd or not is_window_alive(hwnd):
            return None
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    def show_overlay_button(self) -> None:
        if self.overlay_button and self.overlay_button.winfo_exists():
            self._position_overlay_button()
            return

        button_window = tk.Toplevel(self.root)
        button_window.title("已用卡牌")
        button_window.overrideredirect(True)
        button_window.attributes("-topmost", True)
        button_window.attributes("-toolwindow", True)
        button_window.configure(bg="#2b2118")

        button = tk.Button(
            button_window,
            text="牌",
            width=3,
            height=1,
            command=self.toggle_used_cards_window,
            bg="#f1c66a",
            fg="#2b2118",
            activebackground="#ffe08a",
            activeforeground="#2b2118",
            relief=tk.RAISED,
            bd=2,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        button.pack(ipadx=4, ipady=2)
        self.overlay_button = button_window
        self._position_overlay_button()

    def _position_overlay_button(self) -> None:
        if not self.overlay_button or not self.overlay_button.winfo_exists():
            return
        rect = self._target_window_rect()
        if rect is None:
            self.overlay_button.withdraw()
            return
        left, top, right, _ = rect
        self.overlay_button.deiconify()
        width, height = 46, 34
        x = max(left + 8, right - width - 18)
        y = top + 42
        self.overlay_button.geometry(f"{width}x{height}+{x}+{y}")

    def _update_overlay_button(self) -> None:
        if self.config.selected_hwnd:
            self.show_overlay_button()
            if self.used_cards_window and self.used_cards_window.winfo_exists():
                self._position_used_cards_window()
        self.root.after(700, self._update_overlay_button)

    def toggle_used_cards_window(self) -> None:
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.used_cards_window.destroy()
            self.used_cards_window = None
            return
        self.show_used_cards_window()

    def show_used_cards_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("已使用卡牌")
        window.attributes("-topmost", True)
        window.attributes("-toolwindow", True)
        window.configure(bg="#2b2118")
        window.protocol("WM_DELETE_WINDOW", self.close_used_cards_window)
        self.used_cards_window = window
        self._position_used_cards_window()
        self.refresh_used_cards_window()

    def close_used_cards_window(self) -> None:
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.used_cards_window.destroy()
        self.used_cards_window = None

    def _position_used_cards_window(self) -> None:
        if not self.used_cards_window or not self.used_cards_window.winfo_exists():
            return
        rect = self._target_window_rect()
        if rect is None:
            return
        left, top, right, bottom = rect
        game_width = right - left
        game_height = bottom - top
        width = min(720, max(430, game_width // 3))
        height = min(620, max(360, game_height - 110))
        x = right - width - 22
        y = top + 82
        self.used_cards_window.geometry(f"{width}x{height}+{x}+{y}")

    def refresh_used_cards_window(self) -> None:
        window = self.used_cards_window
        if not window or not window.winfo_exists():
            return
        for child in window.winfo_children():
            child.destroy()

        header = tk.Frame(window, bg="#2b2118")
        header.pack(fill=tk.X, padx=10, pady=(10, 6))
        total = sum(self.counter.values())
        tk.Label(
            header,
            text=f"已使用卡牌  {total}",
            bg="#2b2118",
            fg="#f7e7bf",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side=tk.LEFT)
        tk.Button(
            header,
            text="×",
            command=self.close_used_cards_window,
            bg="#5a3826",
            fg="#f7e7bf",
            activebackground="#7a4b2f",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            width=3,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.RIGHT)

        canvas = tk.Canvas(window, bg="#2b2118", highlightthickness=0)
        scrollbar = ttk.Scrollbar(window, orient=tk.VERTICAL, command=canvas.yview)
        body = tk.Frame(canvas, bg="#2b2118")
        body.bind(
            "<Configure>",
            lambda _: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 10))

        if not self.counter:
            tk.Label(
                body,
                text="还没有识别到已使用卡牌",
                bg="#2b2118",
                fg="#d7c8a5",
                font=("Microsoft YaHei UI", 11),
            ).pack(anchor="w", padx=8, pady=18)
            return

        for card_name, count in self.counter.most_common():
            self._add_used_card_row(body, card_name, count)

    def _add_used_card_row(self, parent: tk.Widget, card_name: str, count: int) -> None:
        info = self.card_catalog.get(card_name, CardInfo(name=card_name, image_path=None))
        row = tk.Frame(parent, bg="#3a2a1e", highlightbackground="#b47a33", highlightthickness=1)
        row.pack(fill=tk.X, padx=2, pady=5)

        image_label = tk.Label(row, bg="#3a2a1e")
        image_label.pack(side=tk.LEFT, padx=8, pady=8)
        photo = self._get_card_photo(card_name, info.image_path)
        if photo:
            image_label.configure(image=photo)
            image_label.image = photo
        else:
            image_label.configure(
                text="无图",
                fg="#f7e7bf",
                width=10,
                height=5,
                font=("Microsoft YaHei UI", 10),
            )

        text_frame = tk.Frame(row, bg="#3a2a1e")
        text_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8), pady=8)

        title = tk.Frame(text_frame, bg="#3a2a1e")
        title.pack(fill=tk.X)
        tk.Label(
            title,
            text=card_name,
            bg="#3a2a1e",
            fg="#fff1c6",
            anchor="w",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            title,
            text=f"×{count}",
            bg="#c88a2c",
            fg="#24180f",
            padx=8,
            pady=1,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.RIGHT)

        meta = info.card_type or "卡牌"
        tk.Label(
            text_frame,
            text=meta,
            bg="#3a2a1e",
            fg="#d7c8a5",
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        ).pack(fill=tk.X, pady=(2, 0))

        if info.description:
            tk.Label(
                text_frame,
                text=info.description,
                bg="#3a2a1e",
                fg="#f4e6c2",
                anchor="w",
                justify=tk.LEFT,
                wraplength=430,
                font=("Microsoft YaHei UI", 9),
            ).pack(fill=tk.X, pady=(4, 0))

    def _get_card_photo(self, card_name: str, image_path: Path | None) -> ImageTk.PhotoImage | None:
        if card_name in self.card_photo_cache:
            return self.card_photo_cache[card_name]
        if not image_path or not image_path.exists():
            return None
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((92, 130), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.card_photo_cache[card_name] = photo
        return photo

    def capture_once(self) -> None:
        if not self.config.selected_hwnd:
            messagebox.showwarning("未锁定窗口", "请先锁定目标窗口。")
            return
        try:
            self.config = self._current_config()
            frame = capture_window_client(self.config.selected_hwnd)
            active_roi = resolve_roi(self.config, frame)
            roi_frame, roi_bounds = clamp_roi(frame, active_roi)
            text, _ = self.ocr_engine.read_text(prepare_ocr_image(roi_frame))
            if self.config.roi_mode == ROI_MODE_BOTTOM_CARD:
                text = clean_card_area_text(text)
            matched_items = extract_card_name_items(text, self.card_names)
            matched_text = "\n".join(matched_items) or "未匹配到项目词表中的卡牌名称"
            self.text_output.delete("1.0", tk.END)
            self.text_output.insert(
                tk.END,
                (
                    f"{text}\n\n匹配到的卡牌名称：\n{matched_text}"
                    if text
                    else "这次没有识别到文本。请调整 ROI，或确认目标区域中有清晰中文。"
                ),
            )
            self.summary_ocr_var.set(truncate_text(text) if text else "未识别到文本")
            self.status_var.set(
                f"单次识别完成。窗口大小 {frame.shape[1]}x{frame.shape[0]}，ROI={roi_bounds}，匹配到 {sum(matched_items.values())} 个卡牌名称"
            )
            if text:
                self.last_text = text
        except Exception as exc:
            messagebox.showerror("单次识别失败", str(exc))

    def preview_roi(self) -> None:
        if not self.config.selected_hwnd:
            messagebox.showwarning("未锁定窗口", "请先锁定目标窗口。")
            return
        try:
            self.config = self._current_config()
            frame = capture_window_client(self.config.selected_hwnd)
            active_roi = resolve_roi(self.config, frame)
            _, roi_bounds = clamp_roi(frame, active_roi)
        except Exception as exc:
            messagebox.showerror("预览失败", str(exc))
            return

        x, y, width, height = roi_bounds
        preview = frame.copy()
        cv2.rectangle(preview, (x, y), (x + width, y + height), (255, 32, 32), 4)
        cv2.putText(
            preview,
            f"ROI {x},{y},{width},{height}",
            (max(8, x), max(28, y - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 32, 32),
            2,
            cv2.LINE_AA,
        )

        max_width, max_height = 980, 680
        scale = min(max_width / preview.shape[1], max_height / preview.shape[0], 1.0)
        if scale < 1.0:
            preview = cv2.resize(
                preview,
                (round(preview.shape[1] * scale), round(preview.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )

        window = tk.Toplevel(self.root)
        window.title("识别范围预览")
        window.transient(self.root)

        image = Image.fromarray(preview)
        photo = ImageTk.PhotoImage(image)
        label = ttk.Label(window, image=photo)
        label.image = photo
        label.pack(padx=10, pady=(10, 6))

        mode_text = "下方卡牌区(按比例)" if self.config.roi_mode == ROI_MODE_BOTTOM_CARD else "手动 ROI"
        ratio_text = (
            f"x={x / frame.shape[1]:.3f}, y={y / frame.shape[0]:.3f}, "
            f"w={width / frame.shape[1]:.3f}, h={height / frame.shape[0]:.3f}"
        )
        ttk.Label(
            window,
            text=f"模式: {mode_text} | 窗口: {frame.shape[1]}x{frame.shape[0]} | ROI: {roi_bounds} | 比例: {ratio_text}",
        ).pack(padx=10, pady=(0, 10))
        self.status_var.set(f"已打开识别范围预览。当前 ROI={roi_bounds}")

    def start_monitoring(self) -> None:
        if self.worker and self.worker.is_alive():
            self.status_var.set("监控已经在运行。")
            return
        if not self.config.selected_hwnd:
            messagebox.showwarning("未锁定窗口", "请先锁定目标窗口。")
            return

        try:
            self.config = self._current_config()
            save_config(self.config)
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self.stop_event = threading.Event()
        self.worker = CaptureWorker(
            hwnd=self.config.selected_hwnd,
            config=self.config,
            ocr_engine=self.ocr_engine,
            card_names=self.card_names,
            events=self.events,
            stop_event=self.stop_event,
        )
        self.worker.start()
        self.summary_monitor_var.set("运行中")
        self.status_var.set("后台监控已启动。即使切换到其他窗口，也会继续按句柄抓图。")

    def stop_monitoring(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        self.summary_monitor_var.set("已停止")
        self.status_var.set("后台监控已停止。")

    def on_close(self) -> None:
        self.stop_monitoring()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.5)
        if self.overlay_button and self.overlay_button.winfo_exists():
            self.overlay_button.destroy()
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.used_cards_window.destroy()
        save_config(self.config)
        self.root.destroy()

    def _poll_target_application(self) -> None:
        if not self.root.winfo_exists():
            return

        keyword = self.target_keyword_var.get().strip()
        if not self.auto_detect_var.get():
            self.detected_status_var.set("自动检测已关闭")
            self.root.after(1500, self._poll_target_application)
            return

        if not keyword:
            self.detected_status_var.set("请输入目标关键词")
            self.root.after(1500, self._poll_target_application)
            return

        self.config.target_keyword = keyword
        self.config.auto_detect_enabled = self.auto_detect_var.get()
        self.config.auto_lock_detected = self.auto_lock_var.get()

        matched = find_target_window(keyword)
        if matched:
            self.detected_status_var.set(
                f"已检测到目标程序: {matched.title} [PID {matched.pid}]"
            )
            self.summary_target_var.set(matched.title)
            if self.auto_lock_var.get() and self.config.selected_hwnd != matched.hwnd:
                self.lock_window(matched)
        else:
            self.detected_status_var.set(f"未检测到目标程序: {keyword}")
            self.summary_target_var.set("未检测到目标程序")

        self.root.after(1500, self._poll_target_application)

    def reset_counts(self) -> None:
        self.counter.clear()
        self.records.clear()
        self.last_text = ""
        self.summary_count_var.set("累计 0 次卡牌名称")
        self.summary_ocr_var.set("暂无识别结果")
        if self.worker and self.worker.is_alive():
            self.worker.counter.clear()
            self.worker.records.clear()
            self.worker.last_counted_signature = ""
            self.worker.current_seen_signature = ""
        self.text_output.delete("1.0", tk.END)
        self.counter_output.delete("1.0", tk.END)
        self.refresh_used_cards_window()
        self.status_var.set("统计结果已清空。")

    def export_results(self) -> None:
        if not self.counter and not self.records:
            messagebox.showinfo("没有可导出的结果", "请先运行至少一次成功识别。")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        counts_path = OUTPUT_DIR / f"counts_{timestamp}.csv"
        records_path = OUTPUT_DIR / f"records_{timestamp}.json"

        with counts_path.open("w", newline="", encoding="utf-8-sig") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(["card_name", "count"])
            for text_item, count in self.counter.most_common():
                writer.writerow([text_item, count])

        payload = [asdict(item) for item in self.records]
        records_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.status_var.set(f"已导出到 {counts_path.name} 和 {records_path.name}")

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(200, self._drain_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "error":
            self.status_var.set(f"监控出错: {event['message']}")
            return
        if event["type"] == "capture_error":
            self.counter = Counter(event.get("counter", {}))
            total_count = sum(self.counter.values())
            self.summary_count_var.set(f"累计 {total_count} 次卡牌名称")
            self.summary_monitor_var.set("重试中")
            if self.last_text:
                self.summary_ocr_var.set(truncate_text(self.last_text))
                self.text_output.delete("1.0", tk.END)
                self.text_output.insert(
                    tk.END,
                    f"当前抓图失败，已保留上一次成功识别结果：\n\n{self.last_text}",
                )
            else:
                self.summary_ocr_var.set("等待可抓取画面")
                self.text_output.delete("1.0", tk.END)
                self.text_output.insert(tk.END, "当前抓图失败，正在等待游戏窗口恢复可抓取画面。")
            self.status_var.set(
                f"后台抓图暂不可用，已连续重试 {event.get('failure_count', 1)} 次："
                f"{event['message']}"
            )
            return

        self.counter = Counter(event["counter"])
        if self.worker:
            self.records = list(self.worker.records)

        text = event["text"]
        roi_bounds = event["roi_bounds"]
        frame_shape = event["frame_shape"]
        duplicate = event["duplicate"]
        items_counted = event["items_counted"]
        if text:
            self.last_text = text
        self.summary_monitor_var.set("运行中")

        self.text_output.delete("1.0", tk.END)
        self.text_output.insert(
            tk.END,
            text or "当前帧没有识别到文本。请调整 ROI，或确认窗口没有被最小化。",
        )

        lines = []
        for text_item, count in self.counter.most_common():
            lines.append(f"{text_item}: {count}")
        self.counter_output.delete("1.0", tk.END)
        self.counter_output.insert(tk.END, "\n".join(lines) or "还没有识别到项目词表中的卡牌名称。")
        total_count = sum(self.counter.values())
        self.summary_count_var.set(f"累计 {total_count} 次卡牌名称")
        self.summary_ocr_var.set(truncate_text(text) if text else "未识别到文本")

        status = (
            f"最近窗口尺寸 {frame_shape[1]}x{frame_shape[0]}，ROI={roi_bounds}，"
            f"本次新增 {sum(items_counted.values())} 次卡牌名称，累计 {total_count} 次，"
            f"明细 {items_counted if items_counted else '{}'}"
        )
        if duplicate:
            status += "，这张卡牌画面仍在持续显示，已跳过重复累计。"
        self.status_var.set(status)
        if items_counted or (self.used_cards_window and self.used_cards_window.winfo_exists()):
            self.refresh_used_cards_window()


def truncate_text(value: str, limit: int = 28) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    root.iconname("Gremlins OCR Counter")
    app = GremlinsAssistantApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
