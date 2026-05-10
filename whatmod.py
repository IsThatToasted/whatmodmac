"""
WhatMod Pro Expanded - Whatnot moderator quick-message + notes tool

Major features:
- Multiple message banks: M1, M2, M3, M4, Announcements, Commands, Givey Messages, Shoes
- Two view modes for each message tab:
    1. Edit View: full title/body editor for setup
    2. Short Card View: compact one-click buttons for live moderating
- Dedicated note tabs: General Notes, Givey Notes, Unique Occurrence
- Notes support timestamps, quick templates, copy, clear, and persistent autosave
- Browser launch/reconnect controls for Whatnot chat
- Enter-to-send toggle; when off, Send copies message instead
- Search/filter for quick message cards
- Dedicated Shoes tab with men's, women's, and converted M/W size cards
- Persistent JSON config with migration support from older versions
- Full startup splash loading with prebuilt/cached views
- Custom hotkeys for message tabs and shoe quick-send cards
- License key acceptance and update-settings foundation

Install:
    pip install customtkinter playwright pyperclip
    playwright install chromium

Run:
    python whatmod_sneaker_prod_ready_paired.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import shutil
import subprocess
import re
import sys
import time
import webbrowser
import traceback
import threading
import urllib.request
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
except ImportError:
    BrowserContext = Page = Playwright = None  # type: ignore
    sync_playwright = None  # type: ignore


APP_NAME = "WhatMod Pro Sneaker Edition - Fully Loaded"
APP_DIR = Path.home() / ".whatmod"
CONFIG_FILE = APP_DIR / "messages.json"
HOTKEY_FILE = APP_DIR / "hotkeys.json"
LICENSE_FILE = APP_DIR / "license.json"
UPDATE_FILE = APP_DIR / "update_settings.json"
SHOE_CONFIG_FILE = APP_DIR / "shoes.json"
TAB_NAMES_FILE = APP_DIR / "tab_names.json"
ADMIN_LICENSE_SECRET_FILE = Path.home() / ".whatmod_admin" / "admin_secret.key"
CLIENT_LICENSE_SECRET_FILE = APP_DIR / "license_secret.key"
LOCAL_LICENSE_SECRET_FILE = Path(__file__).resolve().parent / "whatmod_license_secret.key"
PRODUCT_ID = "whatmod"
APP_VERSION = "1.6.3-mac"
LEGACY_SAVE_FILE = Path("messages.json")
PROFILE_DIR = APP_DIR / "whatnot_profile"
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
WHATNOT_URL = "https://www.whatnot.com/"
PURCHASE_LICENSE_URL = "https://buy.stripe.com/test_14A5kEbQne0i7cIgxddUY00"
DEFAULT_LICENSE_STATUS_URL = "https://raw.githubusercontent.com/IsThatToasted/whatmod/main/license_status.json"
DEFAULT_UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/IsThatToasted/whatmod/main/updates/download/manifest.json"
LICENSE_CHECK_INTERVAL_MS = 10 * 60 * 1000


def normalize_license_status_url(url: str) -> str:
    """Accept normal GitHub blob URLs or raw URLs and return a direct JSON URL."""
    cleaned = (url or "").strip()
    if not cleaned:
        return DEFAULT_LICENSE_STATUS_URL
    # User-friendly support for: https://github.com/owner/repo/blob/branch/path/file.json
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", cleaned, flags=re.IGNORECASE)
    if match:
        owner, repo, branch, path = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    # Support copied raw file links that use github.com/raw/... style.
    cleaned = cleaned.replace("https://github.com/", "https://raw.githubusercontent.com/") if "/raw/" in cleaned and "raw.githubusercontent.com" not in cleaned else cleaned
    cleaned = cleaned.replace("/raw/", "/") if "raw.githubusercontent.com" in cleaned else cleaned
    return cleaned


def normalize_update_manifest_url(url: str) -> str:
    """Accept GitHub Pages, GitHub blob, or raw URLs and return a direct manifest JSON URL."""
    cleaned = (url or "").strip()
    if not cleaned:
        return DEFAULT_UPDATE_MANIFEST_URL
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", cleaned, flags=re.IGNORECASE)
    if match:
        owner, repo, branch, path = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    if "/raw/" in cleaned and "raw.githubusercontent.com" not in cleaned:
        cleaned = cleaned.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/raw/", "/")
    return cleaned


def parse_version_tuple(value: str) -> tuple[int, ...]:
    """Small semver-ish parser used for update comparisons."""
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def add_announce_prefix(message: str) -> str:
    """Prefix outgoing chat messages for moderator announce mode without double-prefixing."""
    msg = (message or "").strip()
    if not msg:
        return msg
    return msg if msg.lower().startswith("/announce") else f"/announce {msg}"


def owner_from_license_payload(payload: Optional[Dict[str, object]]) -> str:
    """Return a friendly local owner label from the signed license payload.

    Public revoke/status JSON intentionally does not expose customer names or
    emails, so the client stores this locally from the activated license key.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("owner", "customer", "name", "email"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    return ""

CHAT_INPUT_SELECTORS = [
    'input[data-testid="chat-input"]',
    'textarea[data-testid="chat-input"]',
    'input[placeholder*="chat" i]',
    'textarea[placeholder*="chat" i]',
    '[contenteditable="true"][role="textbox"]',
]

MESSAGE_TABS = ["M1", "M2", "M3", "M4", "Announcements", "Commands", "Givey Messages"]
NOTE_TABS = ["General Notes", "Givey Notes", "Unique Occurrence"]
MESSAGES_PER_TAB = 14


def messages_per_tab(tab_name: str) -> int:
    return MESSAGES_PER_TAB


def find_bundled_asset(filename: str) -> Optional[Path]:
    """Find an asset next to the script/exe, in cwd, or inside PyInstaller bundle."""
    candidates = [
        Path(__file__).resolve().parent / filename,
        Path(sys.argv[0]).resolve().parent / filename,
        Path.cwd() / filename,
    ]
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(getattr(sys, "_MEIPASS")) / filename)
    return next((path for path in candidates if path.exists()), None)


def runtime_base_dir() -> Path:
    """Return the safest read-only resource directory for script, PyInstaller, or macOS .app runs."""
    if hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def configure_playwright_runtime() -> None:
    """Help bundled macOS builds find the Playwright browser payload if it was included."""
    bundled_browsers = runtime_base_dir() / "ms-playwright"
    if bundled_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))


def installed_chrome_channel() -> Optional[str]:
    """Use Chrome channel only where it is normally reliable; bundled Chromium is preferred on macOS."""
    if IS_WINDOWS:
        return "chrome"
    if IS_MAC:
        chrome_app = Path("/Applications/Google Chrome.app")
        return "chrome" if chrome_app.exists() and not (runtime_base_dir() / "ms-playwright").exists() else None
    return "chrome" if shutil.which("google-chrome") or shutil.which("google-chrome-stable") else None


def open_path_in_finder(path: Path) -> None:
    """Open a path in Finder/Explorer/file manager without crashing the app if unavailable."""
    try:
        if IS_MAC:
            subprocess.Popen(["open", str(path)])
        elif IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _format_shoe_size(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value).rstrip("0").rstrip(".")


def _size_range(start: float, stop: float) -> list[float]:
    count = int(round((stop - start) * 2)) + 1
    return [round(start + i * 0.5, 1) for i in range(count)]


def build_shoe_messages() -> List[Dict[str, str]]:
    """Prebuilt shoe-size cards for one-click Whatnot chat posting.

    Standard US conversion: women's size is usually men's + 1.5.
    Example: 10M is roughly 11.5W.
    """
    messages: List[Dict[str, str]] = []

    for size in _size_range(3.5, 18):
        label = _format_shoe_size(size)
        messages.append({"title": f"Men {label}M", "body": f":check: Size {label}M"})

    for size in _size_range(5, 15):
        label = _format_shoe_size(size)
        messages.append({"title": f"Women {label}W", "body": f":check: Size {label}W"})

    for men_size in _size_range(3.5, 13.5):
        women_size = men_size + 1.5
        m_label = _format_shoe_size(men_size)
        w_label = _format_shoe_size(women_size)
        messages.append({"title": f"{m_label}M / {w_label}W", "body": f":check: Size {m_label}M {w_label}W"})

    return messages

DEFAULT_TAB_MESSAGES: Dict[str, List[Dict[str, str]]] = {
    "M1": [
        {"title": "Respect", "body": "Please keep chat respectful."},
        {"title": "No spam", "body": "No spam please."},
        {"title": "Welcome", "body": "Welcome everyone!"},
        {"title": "Bid responsibly", "body": "Please bid responsibly and only bid if you intend to purchase."},
        {"title": "Shipping", "body": "Shipping details are listed on the show page."},
        {"title": "Questions", "body": "Drop questions in chat and we will answer them in order."},
    ],
    "M2": [
        {"title": "Follow seller", "body": "Follow the seller so you do not miss future shows."},
        {"title": "Bookmark", "body": "Bookmark the show and hang out with us."},
        {"title": "Read description", "body": "Please read the item description before bidding."},
        {"title": "Stay on topic", "body": "Please keep chat on topic so the seller can keep the show moving."},
    ],
    "M3": [
        {"title": "Warning", "body": "Please stop. Continued spam or harassment may result in removal."},
        {"title": "Final warning", "body": "Final warning: keep chat respectful and on topic."},
        {"title": "No harassment", "body": "Harassment or personal attacks are not allowed here."},
    ],
    "M4": [
        {"title": "BRB", "body": "The seller will be right back. Thanks for hanging out!"},
        {"title": "Recap", "body": "Quick recap is coming up now."},
        {"title": "Closing", "body": "Thanks everyone for joining tonight!"},
    ],
    "Announcements": [
        {"title": "Show start", "body": "The show is starting now. Welcome in!"},
        {"title": "Next item", "body": "Next item is coming up now."},
        {"title": "Last call", "body": "Last call on this item before we move on."},
        {"title": "Pinned info", "body": "Important info is pinned. Please check it before asking repeat questions."},
    ],
    "Commands": [
        {"title": "Rules", "body": "!rules"},
        {"title": "Shipping", "body": "!shipping"},
        {"title": "Giveaway", "body": "!giveaway"},
        {"title": "Support", "body": "!support"},
    ],
    "Givey Messages": [
        {"title": "Givey rules", "body": "Giveaway rules are shown on screen. Good luck!"},
        {"title": "Givey closing", "body": "Giveaway is closing soon. Make sure you are entered if eligible."},
        {"title": "Congrats", "body": "Congrats to the winner! Please follow the seller's instructions."},
        {"title": "Eligibility", "body": "Please make sure you meet the giveaway eligibility requirements before entering."},
    ],
}

DEFAULT_NOTES: Dict[str, str] = {
    "General Notes": "",
    "Givey Notes": "",
    "Unique Occurrence": "",
}


SHOE_SUB_TABS = ["Men Sizes", "Women Sizes", "M/W Conversion", "Status Buttons", "Shoe Notes"]
MEN_SIZE_START = 3.5
MEN_SIZE_END = 18.0
WOMEN_SIZE_START = 5.0
WOMEN_SIZE_END = 16.0

SHOE_STATUS_MESSAGES = [
    ("Available", ":check: Available"),
    ("Sold", ":x: Sold"),
    ("Missing Size", ":warning: Missing size"),
    ("Run True", "Fits true to size."),
    ("Runs Small", "Runs small. Consider going up half a size."),
    ("Runs Big", "Runs big. Consider going down half a size."),
    ("DS", "Condition: DS / brand new."),
    ("VNDS", "Condition: VNDS / very lightly worn."),
    ("Used", "Condition: Used. Please check all photos/on-screen details."),
    ("No Box", "No original box included."),
    ("OG Box", "Original box included."),
    ("Ask Condition", "Please ask condition questions before bidding."),
    ("Check SKU", "Please verify SKU, size, and condition before bidding."),
    ("Bid Carefully", "Please bid carefully. All bids are binding."),
    ("Shipping", "Shipping will be handled through Whatnot after purchase."),
]


@dataclass
class MessageSlot:
    title: str = ""
    body: str = ""


class ConfigStore:
    @staticmethod
    def default_tabs() -> Dict[str, List[MessageSlot]]:
        tabs: Dict[str, List[MessageSlot]] = {}
        for tab in MESSAGE_TABS:
            items = DEFAULT_TAB_MESSAGES.get(tab, [])
            slots = [MessageSlot(item.get("title", ""), item.get("body", "")) for item in items]
            target_count = messages_per_tab(tab)
            while len(slots) < target_count:
                slots.append(MessageSlot(title=f"Message {len(slots) + 1}", body=""))
            tabs[tab] = slots[:target_count]
        return tabs

    @staticmethod
    def default_notes() -> Dict[str, str]:
        return dict(DEFAULT_NOTES)

    @classmethod
    def load(cls) -> tuple[Dict[str, List[MessageSlot]], Dict[str, str], str]:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_SAVE_FILE
        tabs = cls.default_tabs()
        notes = cls.default_notes()
        view_mode = "Edit"

        if not path.exists():
            return tabs, notes, view_mode

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return tabs, notes, view_mode

        if isinstance(raw, list):
            migrated = [MessageSlot(f"Legacy {idx}", str(body)) for idx, body in enumerate(raw[:MESSAGES_PER_TAB], 1)]
            while len(migrated) < MESSAGES_PER_TAB:
                migrated.append(MessageSlot(f"Message {len(migrated) + 1}", ""))
            tabs["M1"] = migrated
            return tabs, notes, view_mode

        if not isinstance(raw, dict):
            return tabs, notes, view_mode

        source_tabs = raw.get("tabs", raw)
        if isinstance(source_tabs, dict):
            for tab in MESSAGE_TABS:
                incoming = source_tabs.get(tab, [])
                slots: List[MessageSlot] = []
                if isinstance(incoming, list):
                    for i, item in enumerate(incoming[:messages_per_tab(tab)], 1):
                        if isinstance(item, dict):
                            slots.append(MessageSlot(str(item.get("title", f"Message {i}")), str(item.get("body", ""))))
                        else:
                            slots.append(MessageSlot(f"Message {i}", str(item)))
                target_count = messages_per_tab(tab)
                while len(slots) < target_count:
                    slots.append(MessageSlot(f"Message {len(slots) + 1}", ""))
                tabs[tab] = slots[:target_count]

        raw_notes = raw.get("notes", {})
        if isinstance(raw_notes, dict):
            for note_tab in NOTE_TABS:
                notes[note_tab] = str(raw_notes.get(note_tab, notes[note_tab]))

        raw_view = raw.get("view_mode", view_mode)
        if raw_view in {"Edit", "Short Cards"}:
            view_mode = raw_view

        return tabs, notes, view_mode

    @staticmethod
    def save(tabs: Dict[str, List[MessageSlot]], notes: Dict[str, str], view_mode: str) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 4,
            "saved_at": int(time.time()),
            "view_mode": view_mode,
            "tabs": {tab: [asdict(slot) for slot in tabs.get(tab, [])] for tab in MESSAGE_TABS},
            "notes": {tab: notes.get(tab, "") for tab in NOTE_TABS},
        }
        CONFIG_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class ShoeConfigStore:
    """Persistent editable shoe message data for Shoes edit/card views."""

    @staticmethod
    def default_data() -> Dict[str, List[Dict[str, str]]]:
        men = [{"title": f"Size {_format_shoe_size(s)}M", "body": f":check: Size {_format_shoe_size(s)}M"} for s in _size_range(MEN_SIZE_START, MEN_SIZE_END)]
        women = [{"title": f"Size {_format_shoe_size(s)}W", "body": f":check: Size {_format_shoe_size(s)}W"} for s in _size_range(WOMEN_SIZE_START, WOMEN_SIZE_END)]
        conversion = [
            {
                "title": f"{_format_shoe_size(m)}M / {_format_shoe_size(m + 1.5)}W",
                "body": f":check: Size {_format_shoe_size(m)}M {_format_shoe_size(m + 1.5)}W",
            }
            for m in _size_range(MEN_SIZE_START, MEN_SIZE_END)
        ]
        statuses = [{"title": title, "body": body} for title, body in SHOE_STATUS_MESSAGES]
        return {
            "Men Sizes": men,
            "Women Sizes": women,
            "M/W Conversion": conversion,
            "Status Buttons": statuses,
        }

    @classmethod
    def load(cls) -> Dict[str, List[Dict[str, str]]]:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        data = cls.default_data()
        if not SHOE_CONFIG_FILE.exists():
            return data
        try:
            raw = json.loads(SHOE_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return data
        if not isinstance(raw, dict):
            return data
        for key, defaults in data.items():
            incoming = raw.get(key, [])
            merged: List[Dict[str, str]] = []
            if isinstance(incoming, list):
                for item in incoming[:len(defaults)]:
                    if isinstance(item, dict):
                        merged.append({"title": str(item.get("title", "")), "body": str(item.get("body", ""))})
            while len(merged) < len(defaults):
                merged.append(defaults[len(merged)])
            data[key] = merged[:len(defaults)]
        return data

    @staticmethod
    def save(data: Dict[str, List[Dict[str, str]]]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SHOE_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class WhatnotBrowser:
    def __init__(self, status_callback: Callable[[str], None]):
        self.status = status_callback
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    @property
    def is_ready(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    def launch(self) -> None:
        if sync_playwright is None:
            raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium")
        if self.is_ready:
            self.status("Browser already connected.")
            return
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        configure_playwright_runtime()
        browser_label = "Chrome" if installed_chrome_channel() else "Chromium"
        self.status(f"Launching {browser_label}...")
        self.playwright = sync_playwright().start()
        launch_kwargs = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
            "args": ["--disable-features=DialMediaRouteProvider"],
        }
        channel = installed_chrome_channel()
        if channel:
            launch_kwargs["channel"] = channel
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as first_error:
            # On macOS, installed Chrome channel discovery can be brittle. Retry with bundled Chromium before failing.
            launch_kwargs.pop("channel", None)
            try:
                self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            except Exception:
                raise first_error
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(WHATNOT_URL, wait_until="domcontentloaded")
        self.status(f"{browser_label} opened. Log in, open a live show, then click a message card.")

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            if self.playwright:
                self.playwright.stop()
            self.playwright = None
            self.status("Browser closed.")

    def _chat_input(self):
        if not self.is_ready:
            raise RuntimeError("Browser is not open. Click Launch / Reconnect Browser first.")
        assert self.page is not None
        for selector in CHAT_INPUT_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                locator.wait_for(state="visible", timeout=1200)
                return locator
            except Exception:
                continue
        raise RuntimeError("Could not find chat input. Open a live show with chat visible and try again.")

    def send_message(self, message: str) -> None:
        msg = message.strip()
        if not msg:
            raise ValueError("Message is empty.")
        chat_input = self._chat_input()
        chat_input.click()
        chat_input.fill(msg)
        chat_input.press("Enter")
        self.status(f"Sent: {msg[:90]}{'...' if len(msg) > 90 else ''}")



# ------------------------- PySide6 UI Port -------------------------
# This section replaces the original CustomTkinter rendering layer with Qt widgets.
try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QKeySequence, QShortcut, QPixmap, QColor, QPalette
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
        QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
        QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
        QTabWidget, QTextEdit, QVBoxLayout, QWidget, QInputDialog, QProgressDialog, QSplashScreen
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PySide6 is required. Install with: pip install PySide6") from exc



WHATMOD_QSS = """
QMainWindow, QWidget { background: #121018; color: #F5F2FF; font-family: Segoe UI, Arial; font-size: 13px; }
QLabel#appTitle { font-size: 26px; font-weight: 800; color: #FFFFFF; }
QLabel#subtitle { color: #CFC7E8; }
QFrame#headerBar { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #171322, stop:1 #24143A); border-bottom: 1px solid #34234D; }
QFrame#contentPanel { background: #1B1724; border: 1px solid #2B2238; border-radius: 16px; }
QLineEdit, QTextEdit, QComboBox { background: #201B2B; color: #FFFFFF; border: 1px solid #615678; border-radius: 8px; padding: 7px; selection-background-color: #8F3FFC; }
QTextEdit { padding: 6px; }
QPushButton { background: #7D34F6; color: white; border: 0; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #9859FF; }
QPushButton:pressed { background: #5E23C7; }
QPushButton#goldButton { background: #7B4B10; }
QPushButton#goldButton:hover { background: #9A621B; }
QPushButton#softButton { background: #30283D; border: 1px solid #514366; }
QPushButton#softButton:hover { background: #3A304B; }
QCheckBox { spacing: 7px; color: #F5F2FF; }
QCheckBox::indicator { width: 34px; height: 18px; border-radius: 9px; background: #50495E; }
QCheckBox::indicator:checked { background: #8F3FFC; }
QTabWidget::pane { border: 0; background: #1B1724; border-radius: 16px; padding: 8px; }
QTabBar::tab { background: #30283D; color: white; border-radius: 12px; padding: 7px 14px; margin: 2px; }
QTabBar::tab:selected { background: #8F3FFC; color: white; }
QTabBar::tab:hover { background: #49385F; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: #17131F; width: 12px; margin: 2px; border-radius: 6px; }
QScrollBar::handle:vertical { background: #6A6078; border-radius: 6px; min-height: 40px; }
QGroupBox { background: #211C2B; border: 1px solid #3B314D; border-radius: 12px; margin-top: 10px; padding: 12px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #FFFFFF; }
QFrame#messageCard { background: #211C2B; border: 1px solid #423654; border-radius: 16px; }
QFrame#messageCard:hover { border: 1px solid #8F3FFC; background: #251F31; }
QFrame#settingsCard { background: #211C2B; border: 1px solid #3B314D; border-radius: 14px; }
"""

class HotkeyStore:
    @staticmethod
    def load() -> Dict[str, Dict[str, object]]:
        try:
            raw = json.loads(HOTKEY_FILE.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def save(hotkeys: Dict[str, Dict[str, object]]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        HOTKEY_FILE.write_text(json.dumps(hotkeys, indent=2), encoding="utf-8")



class TabNameStore:
    @staticmethod
    def defaults() -> Dict[str, str]:
        names: Dict[str, str] = {key: key for key in MESSAGE_TABS}
        names.update({"Shoes": "Shoes", "Settings": "Settings"})
        for key in NOTE_TABS:
            names[key] = key
        for key in ["All Shoes", "Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons", "Shoe Notes"]:
            names[key] = key
        return names

    @staticmethod
    def load() -> Dict[str, str]:
        names = TabNameStore.defaults()
        try:
            raw = json.loads(TAB_NAMES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in names and str(value).strip():
                        names[key] = str(value).strip()
        except Exception:
            pass
        return names

    @staticmethod
    def save(names: Dict[str, str]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        clean = {k: str(v).strip() for k, v in names.items() if str(v).strip()}
        TAB_NAMES_FILE.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")

class LicenseManager:
    @staticmethod
    def load() -> Dict[str, object]:
        try:
            raw = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _b64url_decode(text: str) -> bytes:
        pad = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode((text + pad).encode("utf-8"))

    @staticmethod
    def _candidate_secrets() -> List[bytes]:
        secrets_found: List[bytes] = []
        for path in (CLIENT_LICENSE_SECRET_FILE, ADMIN_LICENSE_SECRET_FILE, LOCAL_LICENSE_SECRET_FILE):
            try:
                raw = path.read_text(encoding="utf-8").strip()
                if raw:
                    secret = LicenseManager._b64url_decode(raw)
                    if secret not in secrets_found:
                        secrets_found.append(secret)
            except Exception:
                continue
        return secrets_found

    @staticmethod
    def _clean_wmod_key(key: str) -> str:
        compact = re.sub(r"\s+", "", key.strip())
        compact = re.sub(r"^WMOD-", "", compact, flags=re.IGNORECASE)
        return compact.replace("-", "")

    @staticmethod
    def decode_license_key(key: str) -> Dict[str, object]:
        k = re.sub(r"\s+", "", key.strip())
        upper = k.upper()
        if upper == "WHATMOD-DEMO-2026":
            return {"pid": PRODUCT_ID, "lid": "DEMO", "plan": "demo", "exp": None}
        if re.fullmatch(r"WHATMOD-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", upper):
            return {"pid": PRODUCT_ID, "lid": upper, "plan": "manual", "exp": None}
        if not upper.startswith("WMOD-"):
            raise ValueError("Invalid format. Paste the full WMOD key from License Admin.")
        raw = LicenseManager._clean_wmod_key(k)
        if "." not in raw:
            raise ValueError("Invalid WMOD license format. Paste the complete copied key.")
        body64, sig64 = raw.split(".", 1)
        body = LicenseManager._b64url_decode(body64)
        actual = LicenseManager._b64url_decode(sig64)
        candidate_secrets = LicenseManager._candidate_secrets()
        if not candidate_secrets:
            raise ValueError("Missing local license verifier. Open License Admin once, then generate/copy the key again.")
        if not any(hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).digest()[:18], actual) for secret in candidate_secrets):
            raise ValueError("License signature failed. Open License Admin once so it syncs the verifier, then generate a fresh key.")
        payload = json.loads(body.decode("utf-8"))
        if payload.get("pid") != PRODUCT_ID:
            raise ValueError("License is for a different product.")
        exp = payload.get("exp")
        if exp and int(exp) < int(time.time()):
            raise ValueError("License is expired.")
        return payload

    @staticmethod
    def save(key: str, owner: str = "", payload: Optional[Dict[str, object]] = None) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(json.dumps({
            "key": key.strip(), "owner": owner.strip() or owner_from_license_payload(payload),
            "payload": payload or {}, "activated_at": int(time.time()),
            "remote_status": "active", "last_remote_check": 0,
        }, indent=2), encoding="utf-8")


def copy_text(text: str) -> bool:
    if not text:
        return False
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            pass
    QApplication.clipboard().setText(text)
    return True


class MessageCardWidget(QFrame):
    def __init__(self, title: str, body: str, send_cb, copy_cb, key_cb=None):
        super().__init__()
        # Avoid QFrame.StyledPanel here because some PySide6/Python builds can
        # raise a low-level SystemError when converting the enum. Styling gives
        # the same card rendering without touching the fragile enum path.
        self.setObjectName("messageCard")
        self.setStyleSheet("""
            QFrame#messageCard {
                border: 1px solid #3a3a3a;
                border-radius: 10px;
                background: #262626;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.setMinimumHeight(128)
        self.setMaximumHeight(165)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lab = QLabel(title or "Message")
        lab.setStyleSheet("font-weight: 700; font-size: 14px;")
        layout.addWidget(lab)
        prev = (body or "Empty message").replace("\n", " ")
        if len(prev) > 110:
            prev = prev[:107] + "..."
        p = QLabel(prev)
        p.setWordWrap(True)
        layout.addWidget(p)
        row = QHBoxLayout()
        b_send = QPushButton("Send")
        b_send.clicked.connect(lambda: send_cb(body))
        row.addWidget(b_send)
        b_copy = QPushButton("Copy")
        b_copy.clicked.connect(lambda: copy_cb(body))
        row.addWidget(b_copy)
        if key_cb:
            b_key = QPushButton("Assign Key")
            b_key.clicked.connect(lambda _checked=False: key_cb())
            row.addWidget(b_key)
        layout.addLayout(row)


class KeyCaptureDialog(QDialog):
    """Small modal dialog that records the next key or key-combo pressed."""
    def __init__(self, parent, target_label: str):
        super().__init__(parent)
        self.setWindowTitle("Assign Hotkey")
        self.sequence = ""
        self.setModal(True)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        title = QLabel(f"Press the shortcut for {target_label}")
        title.setStyleSheet("font-weight: 700; font-size: 15px;")
        layout.addWidget(title)
        self.preview = QLabel("Waiting for key press…")
        self.preview.setStyleSheet("padding: 18px; border: 1px solid #615678; border-radius: 10px; background: #201B2B;")
        layout.addWidget(self.preview)
        hint = QLabel("Examples: F8, Ctrl+1, Ctrl+Alt+S. Press Esc to cancel.")
        hint.setStyleSheet("color: #CFC7E8;")
        layout.addWidget(hint)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("softButton")
        cancel.clicked.connect(self.reject)
        clear = QPushButton("Clear")
        clear.setObjectName("softButton")
        clear.clicked.connect(self._clear)
        ok = QPushButton("Use Shortcut")
        ok.clicked.connect(self._accept_if_ready)
        row.addWidget(cancel)
        row.addWidget(clear)
        row.addWidget(ok)
        layout.addLayout(row)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        QTimer.singleShot(0, self.setFocus)

    def _clear(self):
        self.sequence = ""
        self.preview.setText("Waiting for key press…")

    def _accept_if_ready(self):
        if self.sequence:
            self.accept()

    def keyPressEvent(self, event):
        key = event.key()
        modifier_keys = {
            int(Qt.Key.Key_Control), int(Qt.Key.Key_Shift), int(Qt.Key.Key_Alt), int(Qt.Key.Key_Meta),
            int(Qt.Key.Key_AltGr), int(Qt.Key.Key_CapsLock), int(Qt.Key.Key_NumLock), int(Qt.Key.Key_ScrollLock),
        }
        key_int = int(key)
        if key_int == int(Qt.Key.Key_Escape):
            self.reject()
            return
        if key_int in (int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter)) and self.sequence:
            self.accept()
            return
        if key_int in modifier_keys:
            return

        mods = event.modifiers()
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("Meta")

        key_text = QKeySequence(key_int).toString(QKeySequence.SequenceFormat.NativeText).strip()
        if not key_text:
            key_text = event.text().upper().strip()
        if not key_text:
            return

        # Avoid duplicate text such as Ctrl+Ctrl when Qt reports a modifier as the key.
        if key_text.lower() not in {p.lower() for p in parts}:
            parts.append(key_text)
        seq = "+".join(parts).strip("+")
        if seq:
            self.sequence = seq
            self.preview.setText(seq)
            QTimer.singleShot(180, self.accept)



class WhatModQtApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 820)
        self.setMinimumSize(1020, 680)
        self.tabs_data, self.notes_data, self.view_mode = ConfigStore.load()
        self.shoe_data = ShoeConfigStore.load()
        self.hotkeys = HotkeyStore.load()
        self.browser = WhatnotBrowser(self.set_status)
        self.enter_to_send = True
        self.shortcuts: List[QShortcut] = []
        self.message_editors: Dict[str, List[tuple[QLineEdit, QTextEdit]]] = {}
        self.note_editors: Dict[str, QTextEdit] = {}
        self.search = QLineEdit()
        self.mode_combo = QComboBox()
        self.main_tabs = QTabWidget()
        self.status = QLabel("Ready.")
        self.license_info = LicenseManager.load()
        self.tab_labels = TabNameStore.load()
        self.tab_name_edits: Dict[str, QLineEdit] = {}
        self._rendering_tab = False
        self._build_ui()
        self.register_hotkeys()
        QTimer.singleShot(250, self.refresh_current_tab)

    def tab_label(self, key: str) -> str:
        return self.tab_labels.get(key, key)

    def hotkey_tab_label(self, tab: str) -> str:
        if tab.startswith("Shoes - "):
            shoe_key = tab.replace("Shoes - ", "", 1)
            return f"{self.tab_label('Shoes')} - {self.tab_label(shoe_key)}"
        return self.tab_label(tab)

    def add_main_tab(self, widget: QWidget, key: str) -> None:
        widget.setProperty("whatmod_key", key)
        self.main_tabs.addTab(widget, self.tab_label(key))

    def _tab_key_at(self, index: int) -> str:
        widget = self.main_tabs.widget(index)
        if widget is not None:
            key = widget.property("whatmod_key")
            if key:
                return str(key)
        return self.main_tabs.tabText(index)

    def apply_tab_labels(self) -> None:
        for i in range(self.main_tabs.count()):
            key = self._tab_key_at(i)
            self.main_tabs.setTabText(i, self.tab_label(str(key)))
        if hasattr(self, "hk_tab"):
            current = self.hk_tab.currentData() or self.hk_tab.currentText()
            self.populate_hotkey_target_combo(str(current))
        if hasattr(self, "hotkey_list_host"):
            self.refresh_hotkey_list()

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)

        header = QFrame()
        header.setObjectName("headerBar")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(22, 14, 22, 14)
        header_layout.setSpacing(10)

        top = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("Edit banks first, then use compact short cards for one-click moderation.")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box, 1)

        actions = QVBoxLayout()
        row1 = QHBoxLayout()
        for text, fn, obj in [
            ("Launch / Reconnect Browser", self.launch_browser, ""),
            ("Save All", self.save_all, ""),
            ("Reset Defaults", self.reset_defaults, "goldButton"),
        ]:
            btn = QPushButton(text)
            if obj:
                btn.setObjectName(obj)
            btn.clicked.connect(fn)
            row1.addWidget(btn)
        actions.addLayout(row1)
        row2 = QHBoxLayout()
        self.enter_box = QCheckBox("Enter-to-send")
        self.enter_box.setChecked(True)
        self.enter_box.toggled.connect(lambda v: setattr(self, 'enter_to_send', bool(v)))
        row2.addWidget(self.enter_box)
        self.topmost_box = QCheckBox("Always on top")
        self.topmost_box.toggled.connect(self.toggle_topmost)
        row2.addWidget(self.topmost_box)
        actions.addLayout(row2)
        top.addLayout(actions)
        header_layout.addLayout(top)

        tools = QHBoxLayout()
        tools.addWidget(QLabel("Message View:"))
        self.mode_combo.addItems(["Edit", "Short Cards"])
        self.mode_combo.setCurrentText(self.view_mode)
        self.mode_combo.currentTextChanged.connect(self.change_mode)
        tools.addWidget(self.mode_combo)
        self.search.setPlaceholderText("Search / filter quick cards...")
        self.search.textChanged.connect(self.refresh_current_tab)
        tools.addWidget(self.search, 1)
        apply_btn = QPushButton("Apply Search")
        apply_btn.clicked.connect(self.refresh_current_tab)
        tools.addWidget(apply_btn)
        header_layout.addLayout(tools)
        outer.addWidget(header)

        body = QFrame()
        body.setObjectName("contentPanel")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(8)
        self.main_tabs.currentChanged.connect(lambda _idx: self.refresh_current_tab())
        body_layout.addWidget(self.main_tabs, 1)
        outer.addWidget(body, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(18, 6, 18, 6)
        footer.addWidget(self.status, 1)
        outer.addLayout(footer)

        for name in MESSAGE_TABS:
            self.add_main_tab(QWidget(), name)
        self.add_main_tab(QWidget(), "Shoes")
        for name in NOTE_TABS:
            self.add_main_tab(self._build_single_note_tab(name), name)
        self.add_main_tab(self._build_settings_tab(), "Settings")

    def _build_single_note_tab(self, name: str):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        editor = QTextEdit()
        editor.setPlainText(self.notes_data.get(name, ""))
        self.note_editors[name] = editor
        def insert_block(text, ed=editor):
            prefix = "\n\n" if ed.toPlainText().strip() else ""
            ed.insertPlainText(prefix + text)
        for txt, cb in [
            ("Timestamp", lambda _=False, ed=editor: ed.insertPlainText(time.strftime("[%Y-%m-%d %I:%M %p] "))),
            ("Incident Template", lambda _=False, fn=insert_block: fn(time.strftime("[%Y-%m-%d %I:%M %p]")+"\nUser:\nIssue:\nAction taken:\nSeller notified:\nFollow-up needed:")),
            ("Givey Template", lambda _=False, fn=insert_block: fn(time.strftime("[%Y-%m-%d %I:%M %p]")+"\nGivey item:\nWinner:\nEligibility checked:\nIssue/notes:\nFollow-up:")),
            ("Copy Notes", lambda _=False, ed=editor: self.copy_to_clipboard(ed.toPlainText(), False)),
            ("Clear", lambda _=False, ed=editor: ed.clear()),
        ]:
            b = QPushButton(txt)
            if txt == "Clear":
                b.setObjectName("goldButton")
            b.clicked.connect(cb)
            row.addWidget(b)
        v.addLayout(row)
        v.addWidget(editor, 1)
        return w

    def _build_settings_tab(self):
        tabs = QTabWidget()
        tabs.addTab(self._build_hotkey_tab(), "Hotkeys")
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel("License & Updates")
        title.setStyleSheet("font-size: 22px; font-weight: 800;")
        v.addWidget(title)
        card = QFrame(); card.setObjectName("settingsCard")
        form = QGridLayout(card)
        form.addWidget(QLabel("License"), 0, 0)
        key_preview = str(self.license_info.get('key', '') or 'No license saved')
        if len(key_preview) > 90:
            key_preview = key_preview[:87] + "..."
        form.addWidget(QLabel(key_preview), 0, 1)
        act = QPushButton("Activate / Change")
        act.clicked.connect(self.show_license_dialog)
        form.addWidget(act, 0, 2)
        form.addWidget(QLabel(f"Current Version: {APP_VERSION}"), 1, 0)
        upd = QPushButton("Check Updates")
        upd.clicked.connect(self.check_for_updates)
        form.addWidget(upd, 1, 2)
        v.addWidget(card)
        v.addStretch(1)
        tabs.addTab(w, "License / Updates")
        tabs.addTab(self._build_tab_names_tab(), "Tab Names")
        return tabs

    def _build_tab_names_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel("Rename the visible app tabs. This does not change saved data or hotkey targets.")
        info.setStyleSheet("font-weight: 700;")
        v.addWidget(info)
        scroll, cont, layout = self._scroll_widget()
        self.tab_name_edits = {}
        groups = [
            ("Main Message Tabs", MESSAGE_TABS),
            ("Utility Tabs", ["Shoes", *NOTE_TABS, "Settings"]),
            ("Shoe Sub-Tabs", ["All Shoes", "Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons", "Shoe Notes"]),
        ]
        for title, keys in groups:
            box = QGroupBox(title)
            grid = QGridLayout(box)
            for r, key in enumerate(keys):
                grid.addWidget(QLabel(key), r, 0)
                edit = QLineEdit(self.tab_label(key))
                self.tab_name_edits[key] = edit
                grid.addWidget(edit, r, 1)
            layout.addWidget(box)
        layout.addStretch(1)
        v.addWidget(scroll, 1)
        row = QHBoxLayout()
        save = QPushButton("Save Tab Names")
        save.clicked.connect(self.save_tab_names)
        reset = QPushButton("Reset Tab Names")
        reset.setObjectName("goldButton")
        reset.clicked.connect(self.reset_tab_names)
        row.addStretch(1); row.addWidget(save); row.addWidget(reset)
        v.addLayout(row)
        return w

    def save_tab_names(self):
        for key, edit in self.tab_name_edits.items():
            value = edit.text().strip() or key
            self.tab_labels[key] = value
        TabNameStore.save(self.tab_labels)
        self.apply_tab_labels()
        self.set_status("Tab names saved.")

    def reset_tab_names(self):
        self.tab_labels = TabNameStore.defaults()
        for key, edit in self.tab_name_edits.items():
            edit.setText(self.tab_label(key))
        TabNameStore.save(self.tab_labels)
        self.apply_tab_labels()
        self.set_status("Tab names reset.")

    def reset_defaults(self):
        if QMessageBox.question(self, "Reset Defaults", "Reset message banks and shoe buttons to defaults? Saved notes stay untouched.") == QMessageBox.Yes:
            self.tabs_data = ConfigStore.default_tabs()
            self.shoe_data = ShoeConfigStore.default_data()
            self.message_editors.clear()
            self.refresh_current_tab()
            self.set_status("Defaults restored. Click Save All to keep them.")

    def _scroll_widget(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        cont = QWidget(); layout = QVBoxLayout(cont)
        scroll.setWidget(cont)
        return scroll, cont, layout

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            elif item.layout(): self.clear_layout(item.layout())

    def change_mode(self, mode: str):
        self.collect_all(); self.view_mode = mode; self.refresh_current_tab()

    def _current_tab_name(self) -> str:
        idx = self.main_tabs.currentIndex()
        return self._tab_key_at(idx)

    def refresh_current_tab(self):
        if getattr(self, "_rendering_tab", False):
            return
        name = self._current_tab_name()
        if name in MESSAGE_TABS:
            self._render_message_tab(name)
        elif name == "Shoes":
            self._render_shoes_tab()

    def _replace_tab_widget(self, name: str, widget: QWidget):
        idx = next(i for i in range(self.main_tabs.count()) if self._tab_key_at(i) == name)
        current_idx = self.main_tabs.currentIndex()
        self._rendering_tab = True
        try:
            old = self.main_tabs.widget(idx)
            self.main_tabs.removeTab(idx)
            if old is not None:
                old.deleteLater()
            widget.setProperty("whatmod_key", name)
            self.main_tabs.insertTab(idx, widget, self.tab_label(name))
            self.main_tabs.setCurrentIndex(idx if current_idx == idx else current_idx)
        finally:
            self._rendering_tab = False

    def _render_message_tab(self, tab_name: str):
        self.collect_tab(tab_name)
        scroll, cont, layout = self._scroll_widget()
        q = self.search.text().strip().lower()
        self.message_editors[tab_name] = []
        if self.view_mode == "Edit":
            for i, slot in enumerate(self.tabs_data.get(tab_name, []), 1):
                box = QGroupBox(f"{i:02d}"); grid = QGridLayout(box)
                title = QLineEdit(slot.title); body = QTextEdit(); body.setPlainText(slot.body); body.setFixedHeight(72)
                grid.addWidget(QLabel("Title"), 0, 0); grid.addWidget(title, 0, 1, 1, 4)
                grid.addWidget(QLabel("Body"), 1, 0); grid.addWidget(body, 1, 1, 1, 4)
                for col, (txt, cb) in enumerate([
                    ("Send", lambda _=False, b=body: self.send_message(b.toPlainText())),
                    ("Copy", lambda _=False, b=body: self.copy_to_clipboard(b.toPlainText())),
                    ("Duplicate", lambda _=False, ix=i-1, t=tab_name: self.duplicate_message(t, ix)),
                    ("Assign Key", lambda _=False, ix=i-1, t=tab_name: self.assign_hotkey_dialog(t, ix)),
                    ("Clear", lambda _=False, te=title, be=body, ix=i: (te.setText(f"Message {ix}"), be.clear())),
                ]):
                    btn = QPushButton(txt); btn.clicked.connect(cb); grid.addWidget(btn, 2, col)
                layout.addWidget(box); self.message_editors[tab_name].append((title, body))
        else:
            gridw = QWidget(); grid = QGridLayout(gridw); layout.addWidget(gridw)
            items = [(i,s) for i,s in enumerate(self.tabs_data.get(tab_name, [])) if not q or q in (s.title+' '+s.body).lower()]
            for n, (i, slot) in enumerate(items):
                card = MessageCardWidget(slot.title or f"Message {i+1}", slot.body, self.send_message, self.copy_to_clipboard, lambda ix=i,t=tab_name: self.assign_hotkey_dialog(t, ix))
                grid.addWidget(card, n//3, n%3)
            if not items: layout.addWidget(QLabel("No matching messages."))
        self._replace_tab_widget(tab_name, scroll)
        self.set_status(f"{tab_name} ready.")

    def _build_notes_tabs(self):
        tabs = QTabWidget()
        for name in NOTE_TABS:
            w = QWidget(); v = QVBoxLayout(w); row = QHBoxLayout()
            editor = QTextEdit(); editor.setPlainText(self.notes_data.get(name, "")); self.note_editors[name] = editor
            def insert_text(text, ed=editor): ed.insertPlainText(("\n\n" if ed.toPlainText().strip() else "") + text)
            for txt, cb in [
                ("Timestamp", lambda _=False, ed=editor: ed.insertPlainText(time.strftime("[%Y-%m-%d %I:%M %p] "))),
                ("Incident Template", lambda _=False, fn=insert_text: fn(time.strftime("[%Y-%m-%d %I:%M %p]")+"\nUser:\nIssue:\nAction taken:\nSeller notified:\nFollow-up needed:")),
                ("Givey Template", lambda _=False, fn=insert_text: fn(time.strftime("[%Y-%m-%d %I:%M %p]")+"\nGivey item:\nWinner:\nEligibility checked:\nIssue/notes:\nFollow-up:")),
                ("Copy Notes", lambda _=False, ed=editor: self.copy_to_clipboard(ed.toPlainText(), False)),
                ("Clear", lambda _=False, ed=editor: ed.clear()),
            ]:
                b = QPushButton(txt); b.clicked.connect(cb); row.addWidget(b)
            v.addLayout(row); v.addWidget(editor, 1); tabs.addTab(w, name)
        return tabs

    def youth_shoe_cards(self) -> List[Dict[str, str]]:
        """Youth/grade-school style quick cards for the combined All Shoes view."""
        cards: List[Dict[str, str]] = []
        for size in _size_range(1, 13):
            label = _format_shoe_size(size)
            cards.append({"title": f"Youth {label}Y", "body": f":check: Size {label}Y"})
        return cards

    def all_shoe_cards(self) -> List[Dict[str, str]]:
        cards: List[Dict[str, str]] = []
        for key in ["Men Sizes", "Women Sizes"]:
            for item in self.shoe_data.get(key, []):
                cards.append({"title": str(item.get("title", "")), "body": str(item.get("body", ""))})
        cards.extend(self.youth_shoe_cards())
        return cards

    def shoe_tab_items(self, key: str) -> List[Dict[str, str]]:
        if key == "All Shoes":
            return self.all_shoe_cards()
        if key == "Youth Sizes":
            return self.youth_shoe_cards()
        return self.shoe_data.get(key, [])

    def _render_shoes_tab(self):
        self.collect_all()
        w = QWidget(); v = QVBoxLayout(w)
        header = QLabel("Shoes Command Center · quick-send sizes, conversions, statuses, and notes")
        header.setStyleSheet("font-weight:700; font-size:18px;"); v.addWidget(header)
        sub = QTabWidget(); v.addWidget(sub, 1)
        q = self.search.text().strip().lower()
        shoe_tabs = ["All Shoes", "Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons"]
        for key in shoe_tabs:
            scroll, cont, layout = self._scroll_widget()
            raw_items = self.shoe_tab_items(key)
            items = [(i, d) for i, d in enumerate(raw_items) if not q or q in (d.get('title','') + ' ' + d.get('body','')).lower()]
            if self.view_mode == "Edit" and key not in {"All Shoes", "Youth Sizes"}:
                for i, d in items:
                    box = QGroupBox(f"{d.get('title','Message')}"); grid = QGridLayout(box)
                    title = QLineEdit(d.get('title','')); body = QTextEdit(); body.setPlainText(d.get('body','')); body.setFixedHeight(72)
                    grid.addWidget(QLabel("Title"),0,0); grid.addWidget(title,0,1,1,4); grid.addWidget(QLabel("Body"),1,0); grid.addWidget(body,1,1,1,4)
                    def save_item(_=False, k=key, ix=i, te=title, be=body):
                        self.shoe_data[k][ix] = {"title": te.text().strip() or f"Message {ix+1}", "body": be.toPlainText().strip()}; ShoeConfigStore.save(self.shoe_data); self.set_status("Saved shoe message.")
                    for col, (txt, cb) in enumerate([("Send", lambda _=False,b=body: self.send_message(b.toPlainText())), ("Copy", lambda _=False,b=body: self.copy_to_clipboard(b.toPlainText())), ("Save", save_item), ("Assign Key", lambda _=False,k=key,ix=i: self.assign_hotkey_dialog('Shoes - '+k, ix))]):
                        btn=QPushButton(txt); btn.clicked.connect(cb); grid.addWidget(btn,2,col)
                    layout.addWidget(box)
            else:
                if self.view_mode == "Edit" and key in {"All Shoes", "Youth Sizes"}:
                    note = QLabel("This combined/generated tab is quick-send only. Edit Men/Women/Conversion/Status tabs to change saved shoe cards.")
                    note.setStyleSheet("color: #CFC7E8; padding: 6px;")
                    layout.addWidget(note)
                gridw=QWidget(); grid=QGridLayout(gridw); grid.setAlignment(Qt.AlignmentFlag.AlignTop); layout.addWidget(gridw)
                for n,(i,d) in enumerate(items):
                    card=MessageCardWidget(d.get('title',''), d.get('body',''), self.send_message, self.copy_to_clipboard, lambda ix=i,k=key: self.assign_hotkey_dialog('Shoes - '+k, ix))
                    grid.addWidget(card,n//4,n%4)
                grid.setRowStretch((len(items)+3)//4, 1)
                if not items:
                    layout.addWidget(QLabel("No matching shoe messages."))
            sub.addTab(scroll, self.tab_label(key))
        notes = QTextEdit(); notes.setPlainText(self.notes_data.get("Shoe Notes", "")); self.note_editors["Shoe Notes"] = notes; sub.addTab(notes, self.tab_label("Shoe Notes"))
        self._replace_tab_widget("Shoes", w)

    def _build_hotkey_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel("Assign shortcuts from message/shoe cards. The list below is grouped by tab and can be edited or deleted directly.")
        info.setStyleSheet("font-weight: 700;")
        v.addWidget(info)

        row = QHBoxLayout()
        self.hk_tab = QComboBox()
        self.hk_index = QComboBox()
        row.addWidget(self.hk_tab, 2)
        row.addWidget(self.hk_index, 3)
        self.hk_tab.currentIndexChanged.connect(self._refresh_hk_index)
        self.populate_hotkey_target_combo()
        self._refresh_hk_index()
        assign = QPushButton("Assign Shortcut")
        assign.clicked.connect(lambda: self.assign_hotkey_dialog(str(self.hk_tab.currentData() or self.hk_tab.currentText()), self.hk_index.currentIndex()))
        row.addWidget(assign, 2)
        clear = QPushButton("Clear Selected Target")
        clear.clicked.connect(self.clear_selected_hotkey)
        row.addWidget(clear, 2)
        v.addLayout(row)

        self.hotkey_list_host = QWidget()
        self.hotkey_list_layout = QVBoxLayout(self.hotkey_list_host)
        self.hotkey_list_layout.setContentsMargins(0, 0, 0, 0)
        self.hotkey_scroll = QScrollArea()
        self.hotkey_scroll.setWidgetResizable(True)
        self.hotkey_scroll.setWidget(self.hotkey_list_host)
        v.addWidget(self.hotkey_scroll, 1)
        QTimer.singleShot(0, self.refresh_hotkey_list)
        return w

    def hotkey_target_keys(self) -> List[str]:
        return MESSAGE_TABS + [
            "Shoes - All Shoes", "Shoes - Men Sizes", "Shoes - Women Sizes", "Shoes - Youth Sizes",
            "Shoes - M/W Conversion", "Shoes - Status Buttons",
        ]

    def populate_hotkey_target_combo(self, current: str = ""):
        if not hasattr(self, "hk_tab") or not hasattr(self, "hk_index"):
            return
        current = current or str(self.hk_tab.currentData() or "")
        self.hk_tab.blockSignals(True)
        self.hk_tab.clear()
        for key in self.hotkey_target_keys():
            self.hk_tab.addItem(self.hotkey_tab_label(key), key)
        if current:
            for i in range(self.hk_tab.count()):
                if self.hk_tab.itemData(i) == current:
                    self.hk_tab.setCurrentIndex(i)
                    break
        self.hk_tab.blockSignals(False)
        self._refresh_hk_index()

    def _refresh_hk_index(self):
        if not hasattr(self, 'hk_tab') or not hasattr(self, 'hk_index'):
            return
        tab = str(self.hk_tab.currentData() or self.hk_tab.currentText())
        if tab.startswith('Shoes - '):
            items = [title or f"Shoe Card {i+1}" for i, (title, _body) in enumerate(self.shoe_hotkey_cards(tab))]
        else:
            arr = self.tabs_data.get(tab, [])
            items = [(slot.title.strip() or f"Message {i+1}") for i, slot in enumerate(arr)]
        self.hk_index.clear()
        self.hk_index.addItems(items)

    def shoe_hotkey_cards(self, tab: str) -> List[tuple[str, str]]:
        key = tab.replace('Shoes - ', '')
        return [(x.get('title',''), x.get('body','')) for x in self.shoe_tab_items(key)]

    def hotkey_title(self, tab: str, index: int) -> str:
        if tab.startswith('Shoes - '):
            cards = self.shoe_hotkey_cards(tab)
            return cards[index][0] if 0 <= index < len(cards) else f"{self.hotkey_tab_label(tab)} {index+1}"
        arr = self.tabs_data.get(tab, [])
        return arr[index].title if 0 <= index < len(arr) and arr[index].title.strip() else f"Message {index+1}"

    def clear_hotkey_list_layout(self):
        if not hasattr(self, "hotkey_list_layout"):
            return
        while self.hotkey_list_layout.count():
            item = self.hotkey_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())

    def refresh_hotkey_list(self):
        if not hasattr(self, "hotkey_list_layout"):
            return
        self.clear_hotkey_list_layout()
        grouped: Dict[str, List[tuple[str, Dict[str, object]]]] = {}
        for seq, payload in sorted(self.hotkeys.items(), key=lambda item: (str(item[1].get('tab','')), str(item[0]))):
            grouped.setdefault(str(payload.get('tab', '')), []).append((seq, payload))

        if not grouped:
            empty = QLabel("No hotkeys assigned yet. Pick a tab/card above or use Assign Key on any card.")
            empty.setObjectName("emptyHotkeyLabel")
            self.hotkey_list_layout.addWidget(empty)
            self.hotkey_list_layout.addStretch(1)
            return

        for tab in self.hotkey_target_keys():
            rows = grouped.get(tab, [])
            if not rows:
                continue
            box = QGroupBox(self.hotkey_tab_label(tab))
            grid = QGridLayout(box)
            grid.setColumnStretch(1, 1)
            grid.addWidget(QLabel("Shortcut"), 0, 0)
            grid.addWidget(QLabel("Card title"), 0, 1)
            grid.addWidget(QLabel("Actions"), 0, 2, 1, 2)
            for r, (seq, payload) in enumerate(rows, 1):
                ix = int(payload.get('index', 0))
                shortcut = QLabel(seq)
                shortcut.setObjectName("shortcutPill")
                title = QLabel(self.hotkey_title(tab, ix))
                title.setWordWrap(True)
                edit = QPushButton("Edit")
                edit.clicked.connect(lambda _=False, s=seq: self.edit_hotkey(s))
                delete = QPushButton("Delete")
                delete.setObjectName("goldButton")
                delete.clicked.connect(lambda _=False, s=seq: self.delete_hotkey(s))
                grid.addWidget(shortcut, r, 0)
                grid.addWidget(title, r, 1)
                grid.addWidget(edit, r, 2)
                grid.addWidget(delete, r, 3)
            self.hotkey_list_layout.addWidget(box)
        self.hotkey_list_layout.addStretch(1)

    def edit_hotkey(self, seq: str):
        payload = self.hotkeys.get(seq)
        if not isinstance(payload, dict):
            return
        tab = str(payload.get('tab', ''))
        index = int(payload.get('index', 0))
        title = self.hotkey_title(tab, index)
        dlg = KeyCaptureDialog(self, f"{self.hotkey_tab_label(tab)}: {title}")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.sequence.strip():
            new_seq = dlg.sequence.strip()
            if new_seq != seq and seq in self.hotkeys:
                del self.hotkeys[seq]
            self.hotkeys[new_seq] = {"tab": tab, "index": index}
            self.save_hotkeys(); self.register_hotkeys(); self.refresh_hotkey_list()
            self.set_status(f"Updated shortcut for {title}.")

    def delete_hotkey(self, seq: str):
        if seq in self.hotkeys:
            del self.hotkeys[seq]
            self.save_hotkeys(); self.register_hotkeys(); self.refresh_hotkey_list()
            self.set_status(f"Deleted hotkey {seq}.")

    def assign_hotkey_dialog(self, tab: str, index: int):
        title = self.hotkey_title(tab, index)
        dlg = KeyCaptureDialog(self, f"{self.hotkey_tab_label(tab)}: {title}")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.sequence.strip():
            seq = dlg.sequence.strip()
            self.hotkeys[seq] = {"tab": tab, "index": index}
            self.save_hotkeys(); self.register_hotkeys(); self.refresh_hotkey_list(); self.set_status(f"Assigned {seq} to {title}.")

    def clear_selected_hotkey(self):
        tab = str(self.hk_tab.currentData() or self.hk_tab.currentText())
        ix = self.hk_index.currentIndex()
        removed = 0
        for seq, p in list(self.hotkeys.items()):
            if p.get('tab') == tab and int(p.get('index', -1)) == ix:
                del self.hotkeys[seq]
                removed += 1
        self.save_hotkeys(); self.register_hotkeys(); self.refresh_hotkey_list()
        self.set_status(f"Cleared {removed} hotkey(s) for selected target.")

    def register_hotkeys(self):
        for s in self.shortcuts: s.setEnabled(False); s.deleteLater()
        self.shortcuts=[]
        for seq,payload in self.hotkeys.items():
            try:
                sc=QShortcut(QKeySequence(seq), self); sc.activated.connect(lambda p=payload: self.handle_hotkey(p)); self.shortcuts.append(sc)
            except Exception: pass

    def handle_hotkey(self, payload: Dict[str, object]):
        msg=self.resolve_hotkey_message(str(payload.get('tab','')), int(payload.get('index',0)))
        if msg: self.send_message(msg)

    def resolve_hotkey_message(self, tab: str, index: int) -> str:
        if tab.startswith('Shoes - '):
            cards=self.shoe_hotkey_cards(tab); return cards[index][1] if 0 <= index < len(cards) else ""
        arr=self.tabs_data.get(tab, []); return arr[index].body if 0 <= index < len(arr) else ""

    def save_hotkeys(self): HotkeyStore.save(self.hotkeys)

    def collect_tab(self, tab_name: str):
        editors = self.message_editors.get(tab_name)
        if editors:
            self.tabs_data[tab_name] = [MessageSlot(t.text().strip(), b.toPlainText().strip()) for t,b in editors]

    def collect_all(self):
        for t in MESSAGE_TABS: self.collect_tab(t)
        for k,e in self.note_editors.items(): self.notes_data[k] = e.toPlainText()

    def save_all(self):
        self.collect_all(); ConfigStore.save(self.tabs_data, self.notes_data, self.view_mode); ShoeConfigStore.save(self.shoe_data); self.set_status("Saved all WhatMod data.")

    def duplicate_message(self, tab: str, index: int):
        self.collect_tab(tab)
        arr=self.tabs_data.get(tab, [])
        if 0 <= index < len(arr):
            arr[index]=MessageSlot(arr[index].title+" Copy", arr[index].body); self.refresh_current_tab(); self.set_status("Duplicated message in place.")

    def format_outgoing_message(self, message: str) -> str:
        tab = self._current_tab_name()
        return add_announce_prefix(message) if tab == "Announcements" else message.strip()

    def send_message(self, message: str):
        msg=self.format_outgoing_message(message)
        if not msg: return
        if not self.enter_to_send:
            self.copy_to_clipboard(msg, False); return
        try:
            self.browser.send_message(msg)
        except Exception as e:
            self.copy_to_clipboard(msg, False); QMessageBox.warning(self, "Browser not ready", f"Copied instead.\n\n{e}")

    def copy_to_clipboard(self, text: str, apply_announce: bool=True):
        msg=self.format_outgoing_message(text) if apply_announce else text.strip()
        if copy_text(msg): self.set_status(f"Copied: {msg[:90]}")

    def launch_browser(self):
        try: self.browser.launch()
        except Exception as e: QMessageBox.critical(self, "Browser Error", str(e))

    def show_license_dialog(self):
        current=LicenseManager.load(); key, ok = QInputDialog.getMultiLineText(self, "Activate License", "Paste license key:", str(current.get('key','')))
        if ok and key.strip():
            try:
                payload=LicenseManager.decode_license_key(key); LicenseManager.save(key, owner_from_license_payload(payload), payload); self.set_status("License activated."); QMessageBox.information(self,"License","License activated.")
            except Exception as e: QMessageBox.critical(self,"License Error",str(e))

    def _fetch_json_url(self, url: str, timeout: int = 12) -> Dict[str, object]:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def check_for_updates(self):
        try:
            settings={}
            if UPDATE_FILE.exists(): settings=json.loads(UPDATE_FILE.read_text(encoding='utf-8'))
            url=normalize_update_manifest_url(str(settings.get('manifest_url', DEFAULT_UPDATE_MANIFEST_URL)))
            manifest=self._fetch_json_url(url); channels=manifest.get('channels',{}) if isinstance(manifest,dict) else {}
            latest=channels.get('stable') if isinstance(channels,dict) else None
            if not isinstance(latest,dict): raise ValueError('No stable update channel in manifest.')
            new=str(latest.get('version','0'))
            if parse_version_tuple(new) > parse_version_tuple(APP_VERSION):
                QMessageBox.information(self,"Update Available", f"Version {new} is available.\n\nDownload URL:\n{latest.get('download_url','')}")
            else: QMessageBox.information(self,"Updates", "You are up to date.")
        except Exception as e: QMessageBox.warning(self,"Updates",str(e))

    def toggle_topmost(self, checked: bool):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked); self.show()

    def set_status(self, text: str): self.status.setText(text)

    def closeEvent(self, event):
        self.save_all()
        try: self.browser.close()
        except Exception: pass
        event.accept()


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    configure_playwright_runtime()
    if IS_MAC:
        os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(WHATMOD_QSS)

    splash = None
    splash_path = find_bundled_asset("splash.png") or find_bundled_asset("splash(2).png")
    if splash_path:
        pix = QPixmap(str(splash_path))
        if not pix.isNull():
            splash = QSplashScreen(pix)
            splash.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            splash.show()
            app.processEvents()

    win = WhatModQtApp()

    def show_main():
        if splash is not None:
            splash.close()
        win.show()
        win.raise_()
        win.activateWindow()

    if splash is not None:
        QTimer.singleShot(3000, show_main)
    else:
        show_main()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
