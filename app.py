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
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
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
MISFORTUNE_MAP_FILENAMES = (
    "misfortune_card_map.csv",
    "misfortune_card_map.json",
)
LOCATION_GROUP_MAP_FILENAMES = (
    "card_location_group_map.csv",
    "card_location_group_map.json",
)
# The local client initializes the baseline card-to-cell mapping in DM.Init.
# Keep a fallback only for future cards that are absent from our exported map.
GAME_CELL_GROUPS = (
    "警察",
    "演讲",
    "赌博",
    "收入",
    "贿赂",
    "厄运",
    "天界",
    "银行",
    "办公楼",
    "工厂",
    "废料场",
    "市场",
    "监狱",
    "地狱",
    "赌场",
    "法院",
    "藏宝阁",
)
DEFAULT_LOCATION_GROUP = "待核对站点"
# Some localized strings are not part of the numbered card block. Keep these
# aliases here so rebuilding the generated catalog does not remove them.
SUPPLEMENTAL_CARD_ENTRIES = (
    {
        "names": ("伪证",),
        "image_path": None,
        "description": "选择任意玩家。该玩家给你{1}*。如果该玩家的^比你的高，则该玩家被逮捕。",
        "flavor": "有人说，镇长在人力资源部门开设了一个部门，专门负责聘请一些足智多谋的证人，而且在伪证方面必须很在行。",
        "card_type": "补充卡牌文本",
    },
    {
        "names": ("有毒废料", "有毒废物"),
        "image_path": None,
        "description": "游戏资源文本：Toxic waste。",
        "flavor": "法律规定所有腐蚀性液体等有毒垃圾都必须倒入地狱。",
        "card_type": "物品/资源文本",
    },
)
USER32 = ctypes.windll.user32
SHCORE = getattr(ctypes.windll, "shcore", None)
DEFAULT_TARGET_KEYWORD = "Gremlins Inc|Gremlins, Inc|GremlinsInc"
ROI_MODE_MANUAL = "manual"
ROI_MODE_BOTTOM_CARD = "bottom_card"
# Baseline: 2560x1440 game client.  The red CARD OCR box is converted
# proportionally for every window size.
DEFAULT_CARD_ROI = {
    "x": 640,
    "y": 1210,
    "width": 1331,
    "height": 230,
}
DEFAULT_BOTTOM_CARD_RELATIVE_ROI = {
    "x": 0.25,
    "y": 0.84,
    "width": 0.52,
    "height": 0.16,
}
# The two deck counters sit together in the right-side game toolbar: the
# misfortune count is above the normal-card count.
DEFAULT_DECK_COUNTER_RELATIVE_ROI = {
    "x": 0.73,
    "y": 0.20,
    "width": 0.055,
    "height": 0.15,
}
# The central panel is normally uncovered during a live match. It provides
# a guard against menu, encyclopedia, settings, and result dialogs.
DEFAULT_GAME_STATE_RELATIVE_ROI = {
    "x": 0.22,
    "y": 0.10,
    "width": 0.56,
    "height": 0.66,
}
GAME_STATE_CONFIRMATIONS = 2
BLOCKING_SCREEN_PHRASES = (
    "单人游戏",
    "多人游戏",
    "自定义赛局",
    "训练赛局",
    "加载赛局",
    "开始挑战",
    "游戏规则",
    "小魔怪百科",
    "制作名单",
    "退出游戏",
    "暂停游戏",
    "游戏设置",
    "比赛结束",
    "重新连接",
)
NORMAL_DECK_SIZE = 189
MISFORTUNE_DECK_SIZE = 40
DECK_COUNTER_CONFIRMATIONS = 2
CARD_TILE_WIDTH = 96
CARD_TILE_HEIGHT = 172
CARD_TILE_GUTTER = 6
DEFAULT_CAPTURE_INTERVAL_SECONDS = 0.01
BOTTOM_CARD_NOISE_PHRASES = (
    "使用卡牌时会发生什么",
    "选择表现",
    "请选择",
)


@dataclass(slots=True)
class Roi:
    x: int = DEFAULT_CARD_ROI["x"]
    y: int = DEFAULT_CARD_ROI["y"]
    width: int = DEFAULT_CARD_ROI["width"]
    height: int = DEFAULT_CARD_ROI["height"]


@dataclass(slots=True)
class RelativeRoi:
    x: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["x"]
    y: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["y"]
    width: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["width"]
    height: float = DEFAULT_BOTTOM_CARD_RELATIVE_ROI["height"]


@dataclass(slots=True)
class AppConfig:
    roi: Roi = field(default_factory=Roi)
    roi_mode: str = ROI_MODE_BOTTOM_CARD
    bottom_card_roi: RelativeRoi = field(default_factory=RelativeRoi)
    interval_seconds: float = DEFAULT_CAPTURE_INTERVAL_SECONDS
    count_repeated_frames: bool = False
    selected_hwnd: int | None = None
    target_keyword: str = DEFAULT_TARGET_KEYWORD
    auto_detect_enabled: bool = True
    auto_lock_detected: bool = True
    auto_reset_on_deck_recycle: bool = False
    important_cards: list[str] = field(default_factory=list)


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
    internal_key: str = ""
    location_groups: tuple[str, ...] = (DEFAULT_LOCATION_GROUP,)


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        return AppConfig()
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw_important_cards = payload.get("important_cards", [])
        if not isinstance(raw_important_cards, list):
            raw_important_cards = []
        roi_data = payload.get("roi", {})
        roi = Roi(
            x=int(roi_data.get("x", DEFAULT_CARD_ROI["x"])),
            y=int(roi_data.get("y", DEFAULT_CARD_ROI["y"])),
            width=int(roi_data.get("width", DEFAULT_CARD_ROI["width"])),
            height=int(roi_data.get("height", DEFAULT_CARD_ROI["height"])),
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
            roi_mode=str(payload.get("roi_mode", ROI_MODE_BOTTOM_CARD)),
            bottom_card_roi=bottom_card_roi,
            interval_seconds=float(
                payload.get("interval_seconds", DEFAULT_CAPTURE_INTERVAL_SECONDS)
            ),
            count_repeated_frames=bool(payload.get("count_repeated_frames", False)),
            selected_hwnd=(
                int(payload["selected_hwnd"]) if payload.get("selected_hwnd") else None
            ),
            target_keyword=str(payload.get("target_keyword", DEFAULT_TARGET_KEYWORD)).replace(
                "Gremlins_lnc", "Gremlins Inc"
            ),
            auto_detect_enabled=bool(payload.get("auto_detect_enabled", True)),
            auto_lock_detected=bool(payload.get("auto_lock_detected", True)),
            auto_reset_on_deck_recycle=bool(
                payload.get("auto_reset_on_deck_recycle", False)
            ),
            important_cards=list(
                dict.fromkeys(
                    name.strip()
                    for name in raw_important_cards
                    if isinstance(name, str) and name.strip()
                )
            ),
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


def resolve_deck_counter_roi(image: np.ndarray) -> Roi:
    return relative_roi_to_absolute(
        image,
        RelativeRoi(**DEFAULT_DECK_COUNTER_RELATIVE_ROI),
    )


def resolve_game_state_roi(image: np.ndarray) -> Roi:
    return relative_roi_to_absolute(
        image,
        RelativeRoi(**DEFAULT_GAME_STATE_RELATIVE_ROI),
    )


def prepare_ocr_image(image: np.ndarray) -> np.ndarray:
    scaled = cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(scaled, cv2.COLOR_RGB2LAB)
    lightness, channel_a, channel_b = cv2.split(lab)
    lightness = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(lightness)
    enhanced = cv2.cvtColor(
        cv2.merge((lightness, channel_a, channel_b)), cv2.COLOR_LAB2RGB
    )
    gray = cv2.cvtColor(enhanced, cv2.COLOR_RGB2GRAY)
    sharpened = cv2.addWeighted(gray, 1.6, cv2.GaussianBlur(gray, (0, 0), 1.2), -0.6, 0)
    return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2RGB)


def prepare_high_contrast_ocr_image(image: np.ndarray) -> np.ndarray:
    """Fallback for card titles whose color is close to the card background."""
    scaled = cv2.resize(image, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(scaled, cv2.COLOR_RGB2GRAY)
    gray = cv2.createCLAHE(clipLimit=3.2, tileGridSize=(8, 8)).apply(gray)
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        35,
        5,
    )
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)


def make_roi_visual_signature(image: np.ndarray) -> np.ndarray:
    """Create a small grayscale preview for the inexpensive unchanged-frame check."""
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    return cv2.resize(gray, (48, 30), interpolation=cv2.INTER_AREA)


def is_visually_unchanged(previous: np.ndarray | None, current: np.ndarray) -> bool:
    if previous is None or previous.shape != current.shape:
        return False
    difference = cv2.absdiff(previous, current)
    # Ignore tiny rendering noise while still reacting to a changed card title/artwork.
    return float(difference.mean()) < 1.2 and float(np.mean(difference > 12)) < 0.02


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


def extract_deck_counts(lines: list[str]) -> dict[str, int] | None:
    """Read the top-to-bottom misfortune and normal deck counters from OCR."""
    values = []
    for line in lines:
        match = re.fullmatch(r"\s*(\d{1,3})\s*", line)
        if match:
            values.append(int(match.group(1)))
    if len(values) != 2:
        return None
    misfortune, normal = values
    if not (0 <= misfortune <= MISFORTUNE_DECK_SIZE):
        return None
    if not (0 <= normal <= NORMAL_DECK_SIZE):
        return None
    return {"misfortune": misfortune, "normal": normal}


def normalize_card_match_text(raw_text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", raw_text).lower()


def is_blocking_game_screen(text: str) -> bool:
    normalized = normalize_card_match_text(text)
    return any(
        normalize_card_match_text(phrase) in normalized
        for phrase in BLOCKING_SCREEN_PHRASES
    )


def iter_card_map_paths(filenames: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    for base_dir in (APP_DIR, RESOURCE_DIR):
        for filename in filenames:
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


def load_location_group_map() -> dict[str, tuple[str, ...]]:
    """Load locally verified location groups without coupling them to image data."""
    groups_by_key: dict[str, tuple[str, ...]] = {}
    for path in iter_card_map_paths(LOCATION_GROUP_MAP_FILENAMES):
        if not path.exists():
            continue
        for row in read_card_map_rows(path):
            internal_key = (row.get("internal_key") or "").strip()
            raw_groups = (row.get("location_groups") or "").strip()
            groups = tuple(
                dict.fromkeys(
                    group.strip() for group in raw_groups.split("|") if group.strip()
                )
            )
            if internal_key and groups:
                groups_by_key[internal_key] = groups
        if groups_by_key:
            break
    return groups_by_key


def load_card_catalog(
    filenames: tuple[str, ...] = CARD_MAP_FILENAMES,
    include_supplemental_entries: bool = False,
) -> dict[str, CardInfo]:
    catalog: dict[str, CardInfo] = {}
    location_groups_by_key = load_location_group_map()
    for path in iter_card_map_paths(filenames):
        if not path.exists():
            continue
        catalog = {}
        for row in read_card_map_rows(path):
            names = [(row.get("zh_name") or "").strip()]
            extra_names = (row.get("zh_extra_texts") or "").strip()
            if extra_names:
                for alias in re.split(r"[|,;，；\n\r]+", extra_names):
                    alias = alias.strip()
                    if alias:
                        names.append(alias)
            image_path = resolve_resource_path((row.get("image_path") or "").strip())
            name = names[0] if names[0] else (row.get("en_name") or "").strip() or (row.get("internal_key") or "").strip()
            info = CardInfo(
                name=name,
                image_path=image_path,
                description=(row.get("zh_description") or "").strip(),
                flavor=(row.get("zh_flavor") or "").strip(),
                card_type=(row.get("card_type_zh") or "").strip(),
                internal_key=(row.get("internal_key") or "").strip(),
                location_groups=location_groups_by_key.get(
                    (row.get("internal_key") or "").strip(),
                    (DEFAULT_LOCATION_GROUP,),
                ),
            )
            for alias in names:
                if not alias or alias in catalog:
                    continue
                catalog[alias] = info
        if catalog:
            break

    if include_supplemental_entries:
        for entry in SUPPLEMENTAL_CARD_ENTRIES:
            image_path = resolve_resource_path(entry["image_path"] or "")
            info = CardInfo(
                name=entry["names"][0],
                image_path=image_path,
                description=entry["description"],
                flavor=entry["flavor"],
                card_type=entry["card_type"],
            )
            for name in entry["names"]:
                catalog.setdefault(name, info)
    return catalog


def load_card_names() -> list[str]:
    return sorted(
        load_card_catalog(include_supplemental_entries=True),
        key=lambda item: (-len(item), item),
    )


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


def read_card_text(
    ocr_engine: OcrEngine,
    image: np.ndarray,
    card_names: list[str],
    clean_card_area: bool = False,
) -> tuple[str, Counter[str]]:
    text, _ = ocr_engine.read_text(prepare_ocr_image(image))
    if clean_card_area:
        text = clean_card_area_text(text)
    matched_items = extract_card_name_items(text, card_names)
    if matched_items:
        return text, matched_items

    fallback_text, _ = ocr_engine.read_text(prepare_high_contrast_ocr_image(image))
    if clean_card_area:
        fallback_text = clean_card_area_text(fallback_text)
    fallback_items = extract_card_name_items(fallback_text, card_names)
    return (fallback_text, fallback_items) if fallback_items else (text, matched_items)


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        hwnd: int,
        config: AppConfig,
        ocr_engine: OcrEngine,
        card_names: list[str],
        misfortune_card_names: set[str],
        events: queue.Queue[dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.hwnd = hwnd
        self.config = config
        self.ocr_engine = ocr_engine
        self.card_names = card_names
        self.misfortune_card_names = misfortune_card_names
        self.events = events
        self.stop_event = stop_event
        self.last_counted_signature = ""
        self.current_seen_signature = ""
        self.counter: Counter[str] = Counter()
        self.records: list[CaptureRecord] = []
        self.failure_count = 0
        self.previous_roi_signature: np.ndarray | None = None
        self.previous_deck_signature: np.ndarray | None = None
        # The two decks recycle independently, so each counter needs its own
        # stable-value check and its own previously confirmed value.
        self.deck_candidates: dict[str, int | None] = {
            "normal": None,
            "misfortune": None,
        }
        self.deck_candidate_samples: dict[str, int] = {
            "normal": 0,
            "misfortune": 0,
        }
        self.deck_counts: dict[str, int | None] = {
            "normal": None,
            "misfortune": None,
        }
        self.previous_game_state_signature: np.ndarray | None = None
        self.game_state_stable_samples = 0
        self.game_state_generation = 0
        self.checked_game_state_generation = -1
        self.gameplay_state_known = False
        self.game_screen_blocked = True
        self.resume_card_baseline_pending = True

    def _clear_counter_scopes(self, scopes: tuple[str, ...]) -> None:
        for name in list(self.counter):
            is_misfortune = name in self.misfortune_card_names
            if ("misfortune" in scopes and is_misfortune) or (
                "normal" in scopes and not is_misfortune
            ):
                del self.counter[name]
        self.last_counted_signature = ""
        self.current_seen_signature = ""

    def _observe_deck_counts(self, counts: dict[str, int]) -> dict[str, Any] | None:
        updated_scopes: list[str] = []
        reset_scopes: list[str] = []
        for scope in ("normal", "misfortune"):
            observed = counts[scope]
            if observed == self.deck_candidates[scope]:
                self.deck_candidate_samples[scope] += 1
            else:
                self.deck_candidates[scope] = observed
                self.deck_candidate_samples[scope] = 1

            if self.deck_candidate_samples[scope] < DECK_COUNTER_CONFIRMATIONS:
                continue

            previous = self.deck_counts[scope]
            if observed == previous:
                continue
            self.deck_counts[scope] = observed
            updated_scopes.append(scope)
            if previous is not None and observed > previous:
                reset_scopes.append(scope)

        if not updated_scopes:
            return None
        reset_scope_tuple = (
            tuple(reset_scopes) if self.config.auto_reset_on_deck_recycle else ()
        )
        if reset_scope_tuple:
            self._clear_counter_scopes(reset_scope_tuple)
        return {
            "type": "deck_update",
            "deck_counts": {
                scope: value
                for scope, value in self.deck_counts.items()
                if value is not None
            },
            "updated_scopes": tuple(updated_scopes),
            "reset_scopes": reset_scope_tuple,
            "counter": dict(self.counter),
        }

    def _capture_deck_update(self, frame: np.ndarray) -> dict[str, Any] | None:
        deck_frame, _ = clamp_roi(frame, resolve_deck_counter_roi(frame))
        signature = make_roi_visual_signature(deck_frame)
        unchanged = is_visually_unchanged(self.previous_deck_signature, signature)
        self.previous_deck_signature = signature
        if unchanged and all(
            samples >= DECK_COUNTER_CONFIRMATIONS
            for samples in self.deck_candidate_samples.values()
        ):
            return None
        _, lines = self.ocr_engine.read_text(prepare_ocr_image(deck_frame))
        counts = extract_deck_counts(lines)
        return self._observe_deck_counts(counts) if counts else None

    def _capture_game_state(self, frame: np.ndarray) -> dict[str, Any] | None:
        state_frame, _ = clamp_roi(frame, resolve_game_state_roi(frame))
        signature = make_roi_visual_signature(state_frame)
        if is_visually_unchanged(self.previous_game_state_signature, signature):
            self.game_state_stable_samples += 1
        else:
            self.previous_game_state_signature = signature
            self.game_state_stable_samples = 1
            self.game_state_generation += 1

        if (
            self.game_state_stable_samples < GAME_STATE_CONFIRMATIONS
            or self.checked_game_state_generation == self.game_state_generation
        ):
            return None

        self.checked_game_state_generation = self.game_state_generation
        text, _ = self.ocr_engine.read_text(prepare_ocr_image(state_frame))
        blocked = is_blocking_game_screen(text)
        previous_blocked = self.game_screen_blocked
        previous_known = self.gameplay_state_known
        self.gameplay_state_known = True
        self.game_screen_blocked = blocked

        if blocked:
            self.previous_roi_signature = None
            self.previous_deck_signature = None
            self.deck_candidate_samples = {"normal": 0, "misfortune": 0}
        elif not previous_known or previous_blocked:
            # Do not count the first visible card after a dialog closes.
            self.resume_card_baseline_pending = True

        if not previous_known or blocked != previous_blocked:
            return {
                "type": "game_state",
                "blocked": blocked,
                "text": text,
            }
        return None

    def run(self) -> None:
        while not self.stop_event.is_set():
            started_at = time.perf_counter()
            try:
                frame = capture_window_client(self.hwnd)
                state_event = self._capture_game_state(frame)
                if state_event:
                    self.events.put(state_event)
                if not self.gameplay_state_known or self.game_screen_blocked:
                    elapsed = time.perf_counter() - started_at
                    if self.stop_event.wait(
                        max(0.01, self.config.interval_seconds - elapsed)
                    ):
                        break
                    continue

                deck_event = self._capture_deck_update(frame)
                if deck_event:
                    self.events.put(deck_event)
                active_roi = resolve_roi(self.config, frame)
                roi_frame, roi_bounds = clamp_roi(frame, active_roi)
                roi_signature = make_roi_visual_signature(roi_frame)
                if self.resume_card_baseline_pending:
                    self.previous_roi_signature = roi_signature
                    self.resume_card_baseline_pending = False
                    self.failure_count = 0
                    elapsed = time.perf_counter() - started_at
                    if self.stop_event.wait(
                        max(0.01, self.config.interval_seconds - elapsed)
                    ):
                        break
                    continue
                force_card_ocr = bool(deck_event and deck_event["reset_scopes"])
                if (
                    not force_card_ocr
                    and is_visually_unchanged(self.previous_roi_signature, roi_signature)
                ):
                    self.failure_count = 0
                    elapsed = time.perf_counter() - started_at
                    if self.stop_event.wait(
                        max(0.01, self.config.interval_seconds - elapsed)
                    ):
                        break
                    continue
                self.previous_roi_signature = roi_signature
                text, matched_items = read_card_text(
                    self.ocr_engine,
                    roi_frame,
                    self.card_names,
                    self.config.roi_mode == ROI_MODE_BOTTOM_CARD,
                )
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
            sleep_for = max(0.01, self.config.interval_seconds - elapsed)
            if self.stop_event.wait(sleep_for):
                break


class GremlinsAssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Gremlins Window OCR Counter")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        initial_width = min(1120, max(720, screen_width - 60))
        initial_height = min(820, max(600, screen_height - 100))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(720, 600)

        self.config = load_config()
        OUTPUT_DIR.mkdir(exist_ok=True)

        self.window_infos: list[WindowInfo] = []
        self.window_labels = tk.StringVar(value=[])
        self.selected_window_index = tk.IntVar(value=0)
        self.interval_var = tk.StringVar(value=str(DEFAULT_CAPTURE_INTERVAL_SECONDS))
        self.roi_mode_var = tk.StringVar(value=self.config.roi_mode)
        self.target_keyword_var = tk.StringVar(value=self.config.target_keyword)
        self.roi_x_var = tk.StringVar(value=str(self.config.roi.x))
        self.roi_y_var = tk.StringVar(value=str(self.config.roi.y))
        self.roi_width_var = tk.StringVar(value=str(self.config.roi.width))
        self.roi_height_var = tk.StringVar(value=str(self.config.roi.height))
        self.count_repeated_frames_var = tk.BooleanVar(
            value=self.config.count_repeated_frames
        )
        self.auto_detect_var = tk.BooleanVar(value=True)
        self.auto_lock_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="准备就绪")
        self.locked_window_var = tk.StringVar(value="尚未锁定窗口")
        self.detected_status_var = tk.StringVar(value="自动检测未开始")
        self.summary_target_var = tk.StringVar(value="未检测到目标程序")
        self.summary_lock_var = tk.StringVar(value="未锁定")
        self.summary_monitor_var = tk.StringVar(value="未启动")
        self.summary_ocr_var = tk.StringVar(value="暂无识别结果")
        self.summary_count_var = tk.StringVar(value="累计 0 次卡牌名称")
        self.card_summary_vars = {
            "all": tk.StringVar(value="卡牌使用情况"),
            "normal": tk.StringVar(value="普通卡牌使用情况"),
            "misfortune": tk.StringVar(value="厄运卡牌使用情况"),
        }
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker: CaptureWorker | None = None
        self.ocr_engine = OcrEngine()
        self.card_catalog = load_card_catalog(include_supplemental_entries=True)
        self.misfortune_catalog = load_card_catalog(MISFORTUNE_MAP_FILENAMES)
        self.important_cards = set(self.config.important_cards)
        self.location_group_var = tk.StringVar(value="全部")
        self.card_names = sorted(
            set(self.card_catalog) | set(self.misfortune_catalog),
            key=lambda item: (-len(item), item),
        )
        self.counter: Counter[str] = Counter()
        self.misfortune_counter: Counter[str] = Counter()
        self.latest_triggered_cards: tuple[str, ...] = ()
        self.records: list[CaptureRecord] = []
        self.last_text = ""
        self.card_photo_cache: dict[tuple[str, bool, int, int], ImageTk.PhotoImage] = {}
        self.card_notebook: ttk.Notebook | None = None
        self.card_panels: dict[str, dict[str, Any]] = {}
        self.main_panes: ttk.PanedWindow | None = None
        self.card_pane_positioned = False
        self.main_canvas: tk.Canvas | None = None

        self._build_ui()
        self.refresh_card_panel()
        self.refresh_windows(initial=True)
        self.root.after(200, self._drain_events)
        self.root.after(1000, self._poll_target_application)
        self.root.after(1200, self.dock_to_target_window)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        cards_frame = ttk.LabelFrame(outer, text="卡牌使用情况", padding=8)
        cards_frame.grid(row=1, column=0, sticky="nsew")
        cards_frame.columnconfigure(0, weight=1)
        cards_frame.rowconfigure(0, weight=1)

        self.card_notebook = ttk.Notebook(cards_frame)
        self.card_notebook.grid(row=0, column=0, sticky="nsew")
        self._create_card_panel("normal", "普通卡牌")
        self._create_card_panel("misfortune", "厄运卡牌")
        # Keep recognition text available to the monitoring workflow without
        # exposing OCR internals in the user-facing interface.
        self.text_output = tk.Text(self.root)

        action_bar = ttk.Frame(outer, padding=(12, 8))
        action_bar.grid(row=0, column=0, sticky="ew")
        for column in range(2):
            action_bar.columnconfigure(column, weight=1, uniform="actions")
        action_buttons = (
            ("开始监控", self.start_monitoring),
            ("停止监控", self.stop_monitoring),
            ("清空本局统计", self.reset_counts),
            ("管理重点卡牌", self.open_important_cards_manager),
        )
        for index, (label, command) in enumerate(action_buttons):
            ttk.Button(action_bar, text=label, command=command).grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=3,
                pady=3,
            )

        status_bar = ttk.Label(
            outer, textvariable=self.status_var, relief=tk.SUNKEN, anchor="w"
        )
        status_bar.grid(row=2, column=0, sticky="ew")

    def _prioritize_card_panel(self) -> None:
        if self.card_pane_positioned or not self.main_panes:
            return
        pane_height = self.main_panes.winfo_height()
        if pane_height < 2:
            self.root.after(50, self._prioritize_card_panel)
            return
        card_height = min(430, max(320, round(pane_height * 0.55)))
        self.main_panes.sashpos(0, max(200, pane_height - card_height))
        self.card_pane_positioned = True

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
            interval_seconds = max(0.01, float(self.interval_var.get()))
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
            auto_reset_on_deck_recycle=self.config.auto_reset_on_deck_recycle,
            important_cards=sorted(self.important_cards),
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
        self.dock_to_target_window()

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

    def dock_to_target_window(self) -> None:
        """Place the main helper beside the windowed game, never over its content."""
        rect = self._target_window_rect()
        if rect is None:
            return
        left, top, right, bottom = rect
        game_width = right - left
        game_height = bottom - top
        virtual_left = self.root.winfo_vrootx()
        virtual_top = self.root.winfo_vrooty()
        virtual_right = virtual_left + self.root.winfo_vrootwidth()
        virtual_bottom = virtual_top + self.root.winfo_vrootheight()
        width = min(560, max(440, game_width // 3))
        height = min(max(600, game_height), max(600, virtual_bottom - virtual_top - 40))
        x = right + 12
        if x + width > virtual_right:
            x = max(virtual_left, left - width - 12)
        y = max(virtual_top, min(top, virtual_bottom - height))
        self.root.attributes("-topmost", False)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

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
        self._make_topmost_without_activation(button_window)
        self.root.after(60, self._restore_game_foreground)

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

    def _make_topmost_without_activation(self, window: tk.Toplevel) -> None:
        """Keep an overlay above the game without stealing its keyboard focus."""
        window.update_idletasks()
        USER32.SetWindowPos(
            window.winfo_id(),
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )

    def _restore_game_foreground(self) -> None:
        hwnd = self.config.selected_hwnd
        if hwnd and is_window_alive(hwnd):
            try:
                USER32.SetForegroundWindow(hwnd)
            except Exception:
                pass

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
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.used_cards_window.deiconify()
            self.used_cards_window.lift()
            self.refresh_used_cards_window()
            return

        window = tk.Toplevel(self.root)
        window.title("卡牌使用情况")
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
        width = min(900, max(560, game_width // 2))
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
        cards = self._catalog_cards()
        counts = self._canonical_card_counts()
        used_card_count = sum(count > 0 for count in counts.values())
        total_count = sum(counts.values())
        tk.Label(
            header,
            text=f"卡牌使用情况  已使用 {used_card_count}/{len(cards)} 张  累计 {total_count} 次",
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
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(body_window, width=event.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 10))

        for column in range(4):
            body.columnconfigure(column, weight=1, uniform="cards")
        for index, info in enumerate(cards):
            self._add_card_status_tile(
                body,
                info,
                counts.get(info.name, 0),
                row=index // 4,
                column=index % 4,
            )

    def _update_overlay_button(self) -> None:
        if self.config.selected_hwnd:
            self.show_overlay_button()
            if self.used_cards_window and self.used_cards_window.winfo_exists():
                self._position_used_cards_window()
        self.root.after(700, self._update_overlay_button)

    def toggle_used_cards_window(self) -> None:
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.close_used_cards_window()
            return
        self.show_used_cards_window()

    def show_used_cards_window(self) -> None:
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.refresh_used_cards_window()
            self._make_topmost_without_activation(self.used_cards_window)
            self.root.after(60, self._restore_game_foreground)
            return

        window = tk.Toplevel(self.root)
        window.title("卡牌使用情况")
        window.attributes("-topmost", True)
        window.attributes("-toolwindow", True)
        window.configure(bg="#2b2118")
        window.protocol("WM_DELETE_WINDOW", self.close_used_cards_window)
        self.used_cards_window = window
        self._position_used_cards_window()
        self.refresh_used_cards_window()
        self._make_topmost_without_activation(window)
        self.root.after(60, self._restore_game_foreground)

    def close_used_cards_window(self) -> None:
        if self.used_cards_window and self.used_cards_window.winfo_exists():
            self.used_cards_window.destroy()
        self.used_cards_window = None

    def refresh_used_cards_window(self) -> None:
        window = self.used_cards_window
        if not window or not window.winfo_exists():
            return
        for child in window.winfo_children():
            child.destroy()

        header = tk.Frame(window, bg="#2b2118")
        header.pack(fill=tk.X, padx=10, pady=(10, 6))
        tk.Label(
            header,
            text="卡牌使用情况",
            bg="#2b2118",
            fg="#f7e7bf",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side=tk.LEFT)
        tk.Button(
            header,
            text="X",
            command=self.close_used_cards_window,
            bg="#5a3826",
            fg="#f7e7bf",
            activebackground="#7a4b2f",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            width=3,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.RIGHT)

        canvas = tk.Canvas(window, bg="#2b2118", highlightthickness=0)
        scrollbar = ttk.Scrollbar(window, orient=tk.VERTICAL, command=canvas.yview)
        body = tk.Frame(canvas, bg="#2b2118")
        body.bind(
            "<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(body_window, width=event.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 10))

        for section, title in (("normal", "普通卡牌"), ("misfortune", "厄运卡牌")):
            counts = self._canonical_card_counts(section)
            cards = [
                info
                for info in self._catalog_cards(section)
                if counts.get(info.name, 0) > 0 or info.name in self.important_cards
            ]
            section_frame = tk.Frame(body, bg="#2b2118")
            section_frame.pack(fill=tk.X, pady=(0, 12))
            tk.Label(
                section_frame,
                text=f"{title}  已使用 {sum(count > 0 for count in counts.values())} 张",
                bg="#2b2118",
                fg="#f1c66a",
                anchor="w",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
            for column in range(4):
                section_frame.columnconfigure(column, weight=1, uniform=section)
            if not cards:
                tk.Label(
                    section_frame,
                    text="暂无已使用或重点卡牌",
                    bg="#2b2118",
                    fg="#b8b8b8",
                    anchor="w",
                ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4)
                continue
            for index, info in enumerate(cards):
                self._add_card_status_tile(
                    section_frame,
                    info,
                    counts.get(info.name, 0),
                    row=index // 4 + 1,
                    column=index % 4,
                    compact=True,
                )

    def _create_card_panel(self, scope: str, tab_title: str) -> None:
        if not self.card_notebook:
            return
        tab = ttk.Frame(self.card_notebook)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.card_notebook.add(tab, text=tab_title)

        header = ttk.Frame(tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, textvariable=self.card_summary_vars[scope]).grid(
            row=0, column=0, sticky="w"
        )
        if scope == "normal":
            ttk.Label(header, text="站点分组").grid(
                row=0, column=1, sticky="e", padx=(8, 4)
            )
            location_groups = ("全部", *self._available_location_groups())
            location_picker = ttk.Combobox(
                header,
                textvariable=self.location_group_var,
                values=location_groups,
                state="readonly",
                width=14,
            )
            location_picker.grid(row=0, column=2, sticky="e")
            location_picker.bind(
                "<<ComboboxSelected>>", lambda _: self._change_location_group()
            )
        if scope == "all":
            ttk.Button(
                header,
                text="管理重点卡牌",
                command=self.open_important_cards_manager,
            ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(
            header,
            text="回到顶部",
            command=lambda: self.scroll_card_panel_to_top(scope),
        ).grid(row=0, column=3, sticky="e", padx=(8, 0))

        canvas = tk.Canvas(tab, bg="#2b2118", highlightthickness=0, height=180)
        scrollbar = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=canvas.yview)
        canvas.bind(
            "<Configure>",
            lambda event, panel_scope=scope: self._on_cards_canvas_configure(
                panel_scope, event
            ),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(6, 0))
        self.card_panels[scope] = {
            "canvas": canvas,
            "columns": 0,
            "refresh_pending": False,
            "tile_widgets": {},
            "tile_counts": {},
            "display_order": (),
        }

    def focus_card_panel(self) -> None:
        self.root.deiconify()
        self.root.lift()
        if self.card_notebook:
            self.card_notebook.select(0)
        self.root.after_idle(lambda: self.scroll_card_panel_to_top("normal"))

    def scroll_card_panel_to_top(self, scope: str = "normal") -> None:
        panel = self.card_panels.get(scope)
        if panel:
            panel["canvas"].yview_moveto(0)

    def _available_location_groups(self) -> tuple[str, ...]:
        visible_cards = self._catalog_cards("normal")
        has_unclassified_card = any(
            DEFAULT_LOCATION_GROUP in info.location_groups for info in visible_cards
        )
        return (
            (*GAME_CELL_GROUPS, DEFAULT_LOCATION_GROUP)
            if has_unclassified_card
            else GAME_CELL_GROUPS
        )

    def _change_location_group(self) -> None:
        self.scroll_card_panel_to_top("normal")
        self.refresh_card_panel("normal")

    def _card_grid_column_count(self, width: int) -> int:
        return max(4, min(11, width // 104))

    def _on_cards_canvas_configure(self, scope: str, event: tk.Event) -> None:
        panel = self.card_panels.get(scope)
        if not panel:
            return
        columns = self._card_grid_column_count(event.width)
        if columns != panel["columns"] and not panel["refresh_pending"]:
            panel["refresh_pending"] = True
            self.root.after_idle(lambda: self.refresh_card_panel(scope))

    def refresh_card_panel(self, scope: str | None = None) -> None:
        if scope is None:
            for panel_scope in self.card_panels:
                self.refresh_card_panel(panel_scope)
            return
        panel = self.card_panels.get(scope)
        if not panel:
            return
        panel["refresh_pending"] = False
        cards = self._catalog_cards(scope)
        counts = self._canonical_card_counts(scope)
        used_card_count = sum(count > 0 for count in counts.values())
        total_count = sum(counts.values())
        location_suffix = ""
        if scope == "normal" and self.location_group_var.get() != "全部":
            location_suffix = f" | {self.location_group_var.get()}"
        important_count = sum(info.name in self.important_cards for info in cards)
        self.card_summary_vars[scope].set(
            f"重点 {important_count} 张 | 已使用 {used_card_count}/{len(cards)} 张，"
            f"累计出现 {total_count} 次{location_suffix}"
        )

        width = max(panel["canvas"].winfo_width(), 416)
        columns = self._card_grid_column_count(width)
        card_names = {info.name for info in cards}
        display_order = tuple(info.name for info in cards)
        tile_widgets: dict[str, dict[str, Any]] = panel["tile_widgets"]
        tile_counts: dict[str, int] = panel["tile_counts"]
        needs_rebuild = (
            columns != panel["columns"]
            or set(tile_widgets) != card_names
            or panel["display_order"] != display_order
        )
        canvas: tk.Canvas = panel["canvas"]
        if needs_rebuild:
            canvas.delete("all")
            tile_widgets.clear()
            tile_counts.clear()
            panel["columns"] = columns
            panel["display_order"] = display_order
            grid_width = columns * (CARD_TILE_WIDTH + CARD_TILE_GUTTER)
            left_offset = max(0, (width - grid_width) // 2)
            for index, info in enumerate(cards):
                state = self._draw_card_status_tile(
                    canvas,
                    info,
                    counts.get(info.name, 0),
                    x=left_offset
                    + (index % columns) * (CARD_TILE_WIDTH + CARD_TILE_GUTTER),
                    y=(index // columns) * (CARD_TILE_HEIGHT + CARD_TILE_GUTTER),
                )
                if state:
                    tile_widgets[info.name] = state
                    tile_counts[info.name] = counts.get(info.name, 0)
        else:
            for info in cards:
                count = counts.get(info.name, 0)
                # Important-card toggles do not change the usage count or the
                # display order in every case, so redraw the state on every
                # refresh instead of only when the count changes.
                self._apply_canvas_card_state(tile_widgets[info.name], count)
                tile_counts[info.name] = count
        bbox = canvas.bbox("all")
        canvas.configure(scrollregion=(0, 0, width, max(1, bbox[3] if bbox else 1)))

    def _draw_card_status_tile(
        self,
        canvas: tk.Canvas,
        info: CardInfo,
        count: int,
        x: int,
        y: int,
    ) -> dict[str, Any]:
        x += CARD_TILE_GUTTER // 2
        y += CARD_TILE_GUTTER // 2
        state = {
            "canvas": canvas,
            "info": info,
            "background": None,
            "tile": canvas.create_rectangle(
                x,
                y,
                x + CARD_TILE_WIDTH,
                y + CARD_TILE_HEIGHT,
                width=1,
            ),
            "image_label": canvas.create_image(
                x + CARD_TILE_WIDTH // 2,
                y + 6,
                anchor="n",
            ),
            "placeholder": canvas.create_text(
                x + CARD_TILE_WIDTH // 2,
                y + 48,
                anchor="center",
                width=CARD_TILE_WIDTH - 8,
                font=("Microsoft YaHei UI", 8),
            ),
            "name_label": canvas.create_text(
                x + CARD_TILE_WIDTH // 2,
                y + 100,
                anchor="n",
                width=CARD_TILE_WIDTH - 6,
                justify="center",
                font=("Microsoft YaHei UI", 9, "bold"),
            ),
            "count_label": canvas.create_text(
                x + CARD_TILE_WIDTH // 2,
                y + 151,
                anchor="n",
                width=CARD_TILE_WIDTH - 6,
                justify="center",
                font=("Microsoft YaHei UI", 8),
            ),
            "image_size": (70, 88),
            "photo": None,
        }
        self._apply_canvas_card_state(state, count)
        for item_name in (
            "tile",
            "image_label",
            "placeholder",
            "name_label",
            "count_label",
        ):
            canvas.tag_bind(
                state[item_name],
                "<Button-1>",
                lambda _event, card_name=info.name: self._toggle_important_card(
                    card_name
                ),
            )
        return state

    def _toggle_important_card(self, card_name: str) -> None:
        if card_name in self.important_cards:
            self.important_cards.remove(card_name)
        else:
            self.important_cards.add(card_name)
        self.config.important_cards = sorted(self.important_cards)
        save_config(self.config)
        self.refresh_card_panel()

    def _apply_canvas_card_state(self, state: dict[str, Any], count: int) -> None:
        info: CardInfo = state["info"]
        used = count > 0
        important = info.name in self.important_cards
        background = "#5a1b22" if important else "#3a2a1e" if used else "#454545"
        border = "#ff4b43" if important else "#c88a2c" if used else "#696969"
        text_color = "#fff4d6" if important or used else "#d0d0d0"
        canvas: tk.Canvas = state["canvas"]
        canvas.itemconfigure(
            state["tile"], fill=background, outline=border, width=4 if important else 1
        )
        canvas.itemconfigure(
            state["name_label"],
            text=f"重点\n{info.name}" if important else info.name,
            fill=text_color,
        )
        canvas.itemconfigure(
            state["count_label"],
            text=f"出现次数: {count}",
            fill="#ffd166" if important else "#f1c66a" if used else "#b8b8b8",
        )
        photo = self._get_card_photo(
            info.name, info.image_path, used or important, state["image_size"]
        )
        state["photo"] = photo
        if photo:
            canvas.itemconfigure(state["image_label"], image=photo)
            canvas.itemconfigure(state["placeholder"], text="")
        else:
            canvas.itemconfigure(state["image_label"], image="")
            canvas.itemconfigure(state["placeholder"], text="暂无图片", fill=text_color)

    def _catalog_cards(self, scope: str) -> list[CardInfo]:
        """Return each display card once; OCR aliases share its primary card entry."""
        cards_by_name: dict[str, CardInfo] = {}
        catalogs = (
            (self.card_catalog, self.misfortune_catalog)
            if scope == "all"
            else (self.card_catalog,) if scope == "normal" else (self.misfortune_catalog,)
        )
        for catalog in catalogs:
            for info in catalog.values():
                cards_by_name.setdefault(info.name, info)
        cards = list(cards_by_name.values())
        if scope == "normal" and self.location_group_var.get() != "全部":
            selected_group = self.location_group_var.get()
            cards = [
                info for info in cards if selected_group in info.location_groups
            ]
        counts = self._canonical_card_counts(scope)
        return sorted(
            cards,
            key=lambda info: (
                0
                if info.name in self.important_cards
                else 1
                if info.name in self.latest_triggered_cards
                else 2
                if counts[info.name] > 0
                else 3,
                -counts[info.name],
                info.name,
            ),
        )

    def _canonical_display_name(self, matched_name: str) -> str:
        info = self.card_catalog.get(matched_name) or self.misfortune_catalog.get(
            matched_name
        )
        return info.name if info else matched_name

    def _canonical_card_counts(self, scope: str) -> Counter[str]:
        if scope == "all":
            return self._canonical_card_counts("normal") + self._canonical_card_counts(
                "misfortune"
            )
        counts: Counter[str] = Counter()
        catalog = self.card_catalog if scope == "normal" else self.misfortune_catalog
        source_counts = self.counter if scope == "normal" else self.misfortune_counter
        for matched_name, count in source_counts.items():
            info = catalog.get(matched_name)
            counts[info.name if info else matched_name] += count
        return counts

    def open_important_cards_manager(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("管理重点卡牌")
        window.transient(self.root)
        window.geometry("500x620")
        window.minsize(380, 420)

        outer = ttk.Frame(window, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text="重点卡牌").grid(row=0, column=0, sticky="w", pady=(0, 8))

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        choices = ttk.Frame(canvas)
        choices.bind(
            "<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        choices_window = canvas.create_window((0, 0), window=choices, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(choices_window, width=event.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(8, 0))

        card_names = sorted(info.name for info in self._catalog_cards("all"))
        selected = {
            name: tk.BooleanVar(value=name in self.important_cards) for name in card_names
        }
        for row, name in enumerate(card_names):
            ttk.Checkbutton(choices, text=name, variable=selected[name]).grid(
                row=row, column=0, sticky="w", padx=4, pady=2
            )

        buttons = ttk.Frame(outer)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=tk.RIGHT)

        def save_important_cards() -> None:
            self.important_cards = {name for name, value in selected.items() if value.get()}
            self.config.important_cards = sorted(self.important_cards)
            save_config(self.config)
            for panel in self.card_panels.values():
                panel["display_order"] = ()
            self.refresh_card_panel()
            window.destroy()

        ttk.Button(buttons, text="保存", command=save_important_cards).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

    def _add_card_status_tile(
        self,
        parent: tk.Widget,
        info: CardInfo,
        count: int,
        row: int,
        column: int,
        compact: bool = False,
    ) -> dict[str, Any] | None:
        used = count > 0
        background = "#3a2a1e" if used else "#454545"
        border = "#c88a2c" if used else "#696969"
        text_color = "#fff1c6" if used else "#d0d0d0"
        tile_width, tile_height = (
            (CARD_TILE_WIDTH, CARD_TILE_HEIGHT) if compact else (132, 220)
        )
        image_width, image_height = (70, 88) if compact else (112, 148)
        name_wrap = 90 if compact else 120
        name_font = ("Microsoft YaHei UI", 9, "bold") if compact else (
            "Microsoft YaHei UI",
            10,
            "bold",
        )
        count_font = ("Microsoft YaHei UI", 8) if compact else ("Microsoft YaHei UI", 9)
        slot = tk.Frame(
            parent,
            bg="#2b2118",
            width=tile_width + CARD_TILE_GUTTER,
            height=tile_height + CARD_TILE_GUTTER,
        )
        slot.grid(row=row, column=column)
        slot.grid_propagate(False)

        tile = tk.Frame(
            slot,
            bg=background,
            highlightbackground=border,
            highlightthickness=1,
            width=tile_width,
            height=tile_height,
        )
        tile.place(x=CARD_TILE_GUTTER // 2, y=CARD_TILE_GUTTER // 2)
        tile.pack_propagate(False)

        image_holder = tk.Frame(
            tile, bg=background, width=image_width, height=image_height
        )
        image_holder.pack(padx=5, pady=(5, 2))
        image_holder.pack_propagate(False)
        image_label = tk.Label(image_holder, bg=background)
        image_label.place(relx=0.5, rely=0.5, anchor="center")
        name_label = tk.Label(
            tile,
            text=info.name,
            bg=background,
            fg=text_color,
            anchor="center",
            wraplength=name_wrap,
            height=2 if compact else 0,
            font=name_font,
        )
        name_label.pack(fill=tk.X, padx=3, pady=(1, 0))
        count_label = tk.Label(
            tile,
            bg=background,
            anchor="center",
            height=1,
            font=count_font,
        )
        count_label.pack(fill=tk.X, padx=3, pady=(1, 3))
        state = {
            "info": info,
            "slot": slot,
            "tile": tile,
            "image_holder": image_holder,
            "image_label": image_label,
            "name_label": name_label,
            "count_label": count_label,
            "image_size": (image_width, image_height),
        }
        self._apply_card_tile_state(state, count)
        return state

    def _apply_card_tile_state(self, state: dict[str, Any], count: int) -> None:
        info: CardInfo = state["info"]
        used = count > 0
        important = info.name in self.important_cards
        background = "#5a1b22" if important else "#3a2a1e" if used else "#454545"
        border = "#ff4b43" if important else "#c88a2c" if used else "#696969"
        text_color = "#fff4d6" if important or used else "#d0d0d0"
        state["tile"].configure(
            bg=background,
            highlightbackground=border,
            highlightthickness=3 if important else 1,
        )
        state["image_holder"].configure(bg=background)
        state["name_label"].configure(
            text=f"重点\n{info.name}" if important else info.name,
            bg=background,
            fg=text_color,
        )
        state["count_label"].configure(
            text=f"出现次数：{count}",
            bg=background,
            fg="#ffd166" if important else "#f1c66a" if used else "#b8b8b8",
        )
        photo = self._get_card_photo(
            info.name, info.image_path, used or important, state["image_size"]
        )
        image_label: tk.Label = state["image_label"]
        if photo:
            image_label.configure(image=photo, text="", bg=background)
            image_label.image = photo
        else:
            image_label.configure(
                image="",
                text="暂无图片",
                bg=background,
                fg=text_color,
                font=("Microsoft YaHei UI", 9),
            )

    def _get_card_photo(
        self,
        card_name: str,
        image_path: Path | None,
        used: bool,
        max_size: tuple[int, int] = (112, 148),
    ) -> ImageTk.PhotoImage | None:
        cache_key = (card_name, used, *max_size)
        if cache_key in self.card_photo_cache:
            return self.card_photo_cache[cache_key]
        if not image_path or not image_path.exists():
            return None
        with Image.open(image_path) as source:
            image = source.convert("RGB")
        if not used:
            image = image.convert("L").convert("RGB")
        image.thumbnail(max_size, Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.card_photo_cache[cache_key] = photo
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
            text, matched_items = read_card_text(
                self.ocr_engine,
                roi_frame,
                self.card_names,
                self.config.roi_mode == ROI_MODE_BOTTOM_CARD,
            )
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
            _, deck_counter_bounds = clamp_roi(
                frame, resolve_deck_counter_roi(frame)
            )
            _, game_state_bounds = clamp_roi(frame, resolve_game_state_roi(frame))
        except Exception as exc:
            messagebox.showerror("预览失败", str(exc))
            return

        preview = frame.copy()

        def draw_preview_box(
            bounds: tuple[int, int, int, int], label: str, color: tuple[int, int, int]
        ) -> None:
            x, y, width, height = bounds
            cv2.rectangle(preview, (x, y), (x + width, y + height), color, 4)
            label_y = y - 10 if y >= 35 else y + height + 28
            cv2.putText(
                preview,
                label,
                (max(8, x), label_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                color,
                2,
                cv2.LINE_AA,
            )

        draw_preview_box(roi_bounds, "CARD OCR", (255, 32, 32))
        draw_preview_box(deck_counter_bounds, "DECK COUNTS", (32, 220, 220))
        draw_preview_box(game_state_bounds, "SCREEN GUARD", (80, 220, 100))

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

        x, y, width, height = roi_bounds
        mode_text = "下方卡牌区(按比例)" if self.config.roi_mode == ROI_MODE_BOTTOM_CARD else "手动 ROI"
        ratio_text = (
            f"x={x / frame.shape[1]:.3f}, y={y / frame.shape[0]:.3f}, "
            f"w={width / frame.shape[1]:.3f}, h={height / frame.shape[0]:.3f}"
        )
        ttk.Label(
            window,
            text=(
                f"红框：卡牌名称识别范围 | 青框：普通卡/厄运卡牌堆数量范围 | "
                f"绿框：菜单/遮挡画面检测范围\n"
                f"模式: {mode_text} | 窗口: {frame.shape[1]}x{frame.shape[0]} | "
                f"卡牌 ROI: {roi_bounds} | 比例: {ratio_text}"
            ),
            justify=tk.LEFT,
        ).pack(padx=10, pady=(0, 10))
        self.status_var.set(
            f"已打开识别范围预览。卡牌 ROI={roi_bounds}，牌堆数量 ROI={deck_counter_bounds}，"
            f"遮挡检测 ROI={game_state_bounds}"
        )

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
            misfortune_card_names=set(self.misfortune_catalog),
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
        self.misfortune_counter.clear()
        self.latest_triggered_cards = ()
        self.records.clear()
        self.last_text = ""
        self.summary_count_var.set("普通 0 次，厄运 0 次")
        self.summary_ocr_var.set("暂无识别结果")
        if self.worker and self.worker.is_alive():
            self.worker.counter.clear()
            self.worker.records.clear()
            self.worker.last_counted_signature = ""
            self.worker.current_seen_signature = ""
            self.worker.previous_roi_signature = None
        self.text_output.delete("1.0", tk.END)
        self.refresh_card_panel()
        self.status_var.set("统计结果已清空。")

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(200, self._drain_events)

    def _set_separated_counters(self, raw_counts: dict[str, int]) -> None:
        self.counter.clear()
        self.misfortune_counter.clear()
        for name, count in raw_counts.items():
            if name in self.misfortune_catalog:
                self.misfortune_counter[name] = count
            elif name in self.card_catalog:
                self.counter[name] = count

    def _summary_count_text(self) -> str:
        return (
            f"普通 {sum(self.counter.values())} 次，"
            f"厄运 {sum(self.misfortune_counter.values())} 次"
        )

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "error":
            self.status_var.set(f"监控出错: {event['message']}")
            return
        if event["type"] == "game_state":
            if event["blocked"]:
                self.summary_monitor_var.set("暂停（遮挡画面）")
                self.status_var.set("检测到菜单或遮挡画面，已暂停卡牌与牌堆统计。")
            else:
                self.summary_monitor_var.set("运行中")
                self.status_var.set("已恢复正常对局画面，正在重新建立识别基准。")
            return
        if event["type"] == "deck_update":
            self._set_separated_counters(event.get("counter", {}))
            if self.worker:
                self.records = list(self.worker.records)
            self.summary_count_var.set(self._summary_count_text())

            deck_counts = event.get("deck_counts", {})
            reset_scopes = event.get("reset_scopes", ())
            if reset_scopes:
                labels = {
                    "normal": "普通卡牌",
                    "misfortune": "厄运卡牌",
                }
                reset_text = "、".join(labels[scope] for scope in reset_scopes)
                self.status_var.set(f"{reset_text}牌堆已轮回，对应使用次数已重新开始统计。")
            else:
                normal = deck_counts.get("normal", "?")
                misfortune = deck_counts.get("misfortune", "?")
                self.status_var.set(
                    f"牌堆剩余数量已更新：普通 {normal}，厄运 {misfortune}。"
                )
            self.refresh_card_panel()
            return
        if event["type"] == "capture_error":
            self._set_separated_counters(event.get("counter", {}))
            self.summary_count_var.set(self._summary_count_text())
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

        self._set_separated_counters(event["counter"])
        if self.worker:
            self.records = list(self.worker.records)

        text = event["text"]
        roi_bounds = event["roi_bounds"]
        frame_shape = event["frame_shape"]
        duplicate = event["duplicate"]
        items_counted = event["items_counted"]
        if items_counted:
            self.latest_triggered_cards = tuple(
                dict.fromkeys(
                    self._canonical_display_name(name) for name in items_counted
                )
            )
        if text:
            self.last_text = text
        self.summary_monitor_var.set("运行中")

        self.text_output.delete("1.0", tk.END)
        self.text_output.insert(
            tk.END,
            text or "当前帧没有识别到文本。请调整 ROI，或确认窗口没有被最小化。",
        )

        total_count = sum(self.counter.values()) + sum(self.misfortune_counter.values())
        self.summary_count_var.set(self._summary_count_text())
        self.summary_ocr_var.set(truncate_text(text) if text else "未识别到文本")

        status = (
            f"最近窗口尺寸 {frame_shape[1]}x{frame_shape[0]}，ROI={roi_bounds}，"
            f"本次新增 {sum(items_counted.values())} 次卡牌名称，累计 {total_count} 次，"
            f"明细 {items_counted if items_counted else '{}'}"
        )
        if duplicate:
            status += "，这张卡牌画面仍在持续显示，已跳过重复累计。"
        self.status_var.set(status)
        if items_counted:
            self.refresh_card_panel()


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
