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
import re
import sys
import time
import webbrowser
import traceback
import threading
import queue
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


APP_NAME = "WhatMod Pro"
APP_DIR = Path.home() / ".whatmod"
CONFIG_FILE = APP_DIR / "messages.json"
HOTKEY_FILE = APP_DIR / "hotkeys.json"
LICENSE_FILE = APP_DIR / "license.json"
UPDATE_FILE = APP_DIR / "update_settings.json"
SHOE_CONFIG_FILE = APP_DIR / "shoes.json"
TAB_NAMES_FILE = APP_DIR / "tab_names.json"
APP_SETTINGS_FILE = APP_DIR / "app_settings.json"
AUTO_MESSAGES_FILE = APP_DIR / "auto_messages.json"
ADMIN_LICENSE_SECRET_FILE = Path.home() / ".whatmod_admin" / "admin_secret.key"
CLIENT_LICENSE_SECRET_FILE = APP_DIR / "license_secret.key"
LOCAL_LICENSE_SECRET_FILE = Path(__file__).resolve().parent / "whatmod_license_secret.key"
PRODUCT_ID = "whatmod"
APP_VERSION = "1.7.0"
LEGACY_SAVE_FILE = Path("messages.json")
PROFILE_DIR = APP_DIR / "whatnot_profile"
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


def get_app_icon_path() -> Optional[Path]:
    """Find the application icon in dev mode or inside a PyInstaller bundle."""
    return find_bundled_asset("assets/icon.ico") or find_bundled_asset("icon.ico")


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
                # If the tab exists in saved data, respect the user's exact card list.
                # This allows cards to be truly added or deleted instead of forcing the
                # older fixed 14-slot layout back on the next launch.
                if tab not in source_tabs:
                    continue
                incoming = source_tabs.get(tab, [])
                slots: List[MessageSlot] = []
                if isinstance(incoming, list):
                    for i, item in enumerate(incoming, 1):
                        if isinstance(item, dict):
                            slots.append(MessageSlot(str(item.get("title", f"Message {i}")), str(item.get("body", ""))))
                        else:
                            slots.append(MessageSlot(f"Message {i}", str(item)))
                tabs[tab] = slots

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
            "Custom": [],
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
            # If a section is saved, respect the exact saved list so users can add
            # and delete editable shoe cards. If a section is missing entirely,
            # keep the built-in defaults for first run/backward compatibility.
            if key not in raw:
                continue
            incoming = raw.get(key, [])
            merged: List[Dict[str, str]] = []
            if isinstance(incoming, list):
                for item in incoming:
                    if isinstance(item, dict):
                        merged.append({"title": str(item.get("title", "")), "body": str(item.get("body", ""))})
            data[key] = [item for item in merged if item.get("title", "").strip() or item.get("body", "").strip()]
        return data

    @staticmethod
    def save(data: Dict[str, List[Dict[str, str]]]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SHOE_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")



class WhatnotBrowser:
    """Thread-owned Playwright controller.

    Playwright's sync API must not be used on the same thread as an active
    asyncio/Qt event loop on some macOS builds. This controller keeps every
    Playwright call on one dedicated worker thread so Launch, reconnect, and
    Send never touch Playwright from the UI thread.
    """

    def __init__(self, status_callback: Callable[[str], None]):
        self.status_callback = status_callback
        self._command_queue: "queue.Queue[tuple]" = queue.Queue()
        self._status_queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._ready = False
        self._ready_lock = threading.Lock()

    def _set_ready(self, value: bool) -> None:
        with self._ready_lock:
            self._ready = bool(value)

    @property
    def is_ready(self) -> bool:
        with self._ready_lock:
            return bool(self._ready)

    def _queue_status(self, message: str) -> None:
        self._status_queue.put(str(message))

    def drain_status_messages(self) -> None:
        latest = None
        while True:
            try:
                latest = self._status_queue.get_nowait()
            except Exception:
                break
        if latest:
            try:
                self.status_callback(latest)
            except Exception:
                pass

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._worker_loop, name="WhatModPlaywrightWorker", daemon=True)
            self._worker.start()

    def _call_worker(self, command: str, *args, timeout: float = 45.0):
        self._ensure_worker()
        result_q: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
        self._command_queue.put((command, args, result_q))
        ok, payload = result_q.get(timeout=timeout)
        self.drain_status_messages()
        if ok:
            return payload
        raise payload

    @staticmethod
    def _is_user_aborted_launch_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(part in text for part in [
            "net::err_aborted",
            "frame was detached",
            "target page, context or browser has been closed",
            "browser has been closed",
            "page closed",
            "context closed",
        ])

    def _worker_loop(self) -> None:
        playwright = None
        context = None
        page = None

        def cleanup() -> None:
            nonlocal playwright, context, page
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if playwright:
                    playwright.stop()
            except Exception:
                pass
            playwright = None
            context = None
            page = None
            self._set_ready(False)

        def alive() -> bool:
            nonlocal page, context, playwright
            try:
                if playwright is None or context is None or page is None:
                    self._set_ready(False)
                    return False
                if page.is_closed():
                    cleanup()
                    return False
                page.evaluate("() => true")
                self._set_ready(True)
                return True
            except Exception:
                cleanup()
                return False

        while True:
            command, args, result_q = self._command_queue.get()
            try:
                if command == "stop_worker":
                    cleanup()
                    result_q.put((True, None))
                    break

                if command == "ping":
                    result_q.put((True, alive()))
                    continue

                if command == "close":
                    cleanup()
                    self._queue_status("Browser closed.")
                    result_q.put((True, None))
                    continue

                if command == "launch":
                    if alive():
                        self._queue_status("Browser already connected.")
                        result_q.put((True, True))
                        continue
                    cleanup()
                    if sync_playwright is None:
                        raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium")
                    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                    self._queue_status("Launching Chrome...")
                    try:
                        playwright = sync_playwright().start()
                        context = playwright.chromium.launch_persistent_context(
                            user_data_dir=str(PROFILE_DIR),
                            channel="chrome",
                            headless=False,
                            viewport={"width": 1280, "height": 900},
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(WHATNOT_URL, wait_until="domcontentloaded")
                        self._set_ready(True)
                        self._queue_status("Chrome opened. Log in, open a live show, then click a message card.")
                        result_q.put((True, True))
                    except Exception as exc:
                        cleanup()
                        if self._is_user_aborted_launch_error(exc):
                            self._queue_status("Browser launch was interrupted. Click Launch / Reconnect Browser to try again.")
                            result_q.put((True, False))
                        else:
                            result_q.put((False, exc))
                    continue

                if command == "send":
                    message = str(args[0]).strip() if args else ""
                    if not message:
                        raise ValueError("Message is empty.")
                    if not alive():
                        raise RuntimeError("Browser is not open. Click Launch / Reconnect Browser first.")
                    assert page is not None
                    chat_input = None
                    for selector in CHAT_INPUT_SELECTORS:
                        try:
                            locator = page.locator(selector).first
                            locator.wait_for(state="visible", timeout=1200)
                            chat_input = locator
                            break
                        except Exception:
                            continue
                    if chat_input is None:
                        raise RuntimeError("Could not find chat input. Open a live show with chat visible and try again.")
                    chat_input.click()
                    chat_input.fill(message)
                    chat_input.press("Enter")
                    self._queue_status(f"Sent: {message[:90]}{'...' if len(message) > 90 else ''}")
                    result_q.put((True, True))
                    continue

                raise RuntimeError(f"Unknown browser command: {command}")
            except Exception as exc:
                result_q.put((False, exc))

        cleanup()

    def refresh_connection_state(self) -> bool:
        try:
            ready = bool(self._call_worker("ping", timeout=2.0))
            self.drain_status_messages()
            return ready
        except Exception:
            self._set_ready(False)
            self.drain_status_messages()
            return False

    def launch(self) -> None:
        self._call_worker("launch", timeout=60.0)
        self.drain_status_messages()

    def close(self) -> None:
        try:
            self._call_worker("close", timeout=8.0)
        except Exception:
            self._set_ready(False)
        self.drain_status_messages()

    def send_message(self, message: str) -> None:
        self._call_worker("send", message, timeout=12.0)
        self.drain_status_messages()



# ------------------------- PySide6 UI Port -------------------------
# This section replaces the original CustomTkinter rendering layer with Qt widgets.
try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QKeySequence, QShortcut, QPixmap, QColor, QPalette, QIcon
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
        QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
        QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
        QTabWidget, QTextEdit, QVBoxLayout, QWidget, QInputDialog, QProgressDialog, QSplashScreen, QSpinBox, QProgressBar
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PySide6 is required. Install with: pip install PySide6") from exc



WHATMOD_QSS = """
QMainWindow, QWidget {
    background: #0F1115;
    color: #ECEFF4;
    font-family: Segoe UI, Arial;
    font-size: 13px;
}
QLabel#appTitle {
    font-size: 24px;
    font-weight: 800;
    color: #F7F7F8;
    letter-spacing: .2px;
}
QLabel#subtitle, QLabel#sectionHint {
    color: #9CA3AF;
}
QFrame#headerBar {
    background: #111318;
    border-bottom: 1px solid #232733;
}
QFrame#contentPanel {
    background: #0F1115;
    border: 0;
}
QFrame#contentShell {
    background: #0F1115;
    border: 0;
}
QFrame#navSidebar {
    background: #090B0F;
    border-right: 1px solid #1D222C;
    min-width: 168px;
    max-width: 168px;
}
QFrame#navSidebar QLabel {
    background: transparent;
}
QLabel#navBrand {
    color: #F8FAFC;
    font-size: 20px;
    font-weight: 900;
    padding: 2px 6px 0 6px;
}
QLabel#navSub {
    color: #7B8495;
    font-size: 11px;
    padding: 0 6px 8px 6px;
}
QFrame#navDivider {
    background: #1E2430;
    border: 0;
    margin: 4px 8px 10px 8px;
}
QLabel#navSection {
    color: #697386;
    font-size: 10px;
    font-weight: 800;
    padding: 10px 8px 3px 8px;
    background: transparent;
}
QPushButton#navButton, QPushButton#navButtonIndented, QPushButton#navGroupButton {
    background: transparent;
    color: #B8C0CC;
    border: 0;
    border-radius: 9px;
    text-align: left;
    font-weight: 700;
    min-height: 26px;
    max-height: 30px;
}
QPushButton#navButton {
    padding: 6px 8px;
}
QPushButton#navButtonIndented {
    color: #A8B1C0;
    padding: 5px 8px 5px 20px;
    font-weight: 650;
}
QPushButton#navGroupButton {
    color: #D7DCE5;
    padding: 6px 8px;
    margin-top: 6px;
}
QPushButton#navButton:hover, QPushButton#navButtonIndented:hover, QPushButton#navGroupButton:hover {
    background: #141922;
    color: #FFFFFF;
}
QPushButton#navButton:checked, QPushButton#navButton[active="true"],
QPushButton#navButtonIndented:checked, QPushButton#navButtonIndented[active="true"] {
    background: #1B2230;
    color: #FFFFFF;
    border: 1px solid #303848;
}
QPushButton#navGroupButton:checked, QPushButton#navGroupButton[active="true"] {
    background: transparent;
    border: 0;
}
QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background: #171A21;
    color: #F3F4F6;
    border: 1px solid #2B303B;
    border-radius: 10px;
    padding: 8px;
    selection-background-color: #5B6EE1;
}
QTextEdit { padding: 8px; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #5B6EE1;
}
QPushButton {
    background: #252A35;
    color: #F3F4F6;
    border: 1px solid #343A46;
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 650;
}
QPushButton:hover { background: #303745; border-color: #475064; }
QPushButton:pressed { background: #1F2430; }
QPushButton#primaryButton, QPushButton#successButton {
    background: #4057D6;
    border: 1px solid #5368E7;
    color: white;
}
QPushButton#primaryButton:hover, QPushButton#successButton:hover { background: #5068E8; }
QPushButton#goldButton, QPushButton#dangerButton {
    background: #3A2026;
    border: 1px solid #6F3542;
    color: #FFE5E9;
}
QPushButton#goldButton:hover, QPushButton#dangerButton:hover { background: #4A2931; }
QPushButton#softButton, QPushButton#ghostButton {
    background: transparent;
    color: #CBD5E1;
    border: 1px solid #2B303B;
}
QPushButton#softButton:hover, QPushButton#ghostButton:hover { background: #171A21; }
QCheckBox { spacing: 8px; color: #E5E7EB; }
QCheckBox::indicator {
    width: 36px;
    height: 20px;
    border-radius: 10px;
    background: #303642;
    border: 1px solid #3F4654;
}
QCheckBox::indicator:checked {
    background: #4057D6;
    border: 1px solid #5368E7;
}
QTabWidget::pane {
    border: 0;
    background: #0F1115;
}
QTabBar::tab {
    background: transparent;
    color: #A7AFBD;
    border-radius: 12px;
    padding: 12px 18px;
    margin: 4px 8px;
    min-width: 132px;
    text-align: left;
}
QTabBar::tab:selected {
    background: #1B202A;
    color: #FFFFFF;
    border: 1px solid #2D3442;
}
QTabBar::tab:hover {
    background: #171A21;
    color: #FFFFFF;
}
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #333B49; border-radius: 6px; min-height: 40px; }
QGroupBox {
    background: #151820;
    border: 1px solid #252B36;
    border-radius: 16px;
    margin-top: 12px;
    padding: 14px;
    font-weight: 750;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #F3F4F6;
}
QFrame#messageCard, QFrame#settingsCard, QFrame#dashboardCard {
    background: #151820;
    border: 1px solid #252B36;
    border-radius: 18px;
}
QFrame#messageCard:hover, QFrame#dashboardCard:hover {
    border: 1px solid #3A4353;
    background: #181C25;
}
QLabel#sectionTitle {
    font-size: 20px;
    font-weight: 850;
    color: #F7F7F8;
}
QLabel#metricNumber {
    font-size: 28px;
    font-weight: 900;
    color: #FFFFFF;
}
QLabel#metricLabel {
    color: #9CA3AF;
    font-size: 12px;
}
QProgressBar {
    background: #171A21;
    border: 1px solid #2B303B;
    border-radius: 8px;
    height: 14px;
    text-align: center;
    color: #D1D5DB;
}
QProgressBar::chunk {
    background: #4057D6;
    border-radius: 7px;
}
"""


@dataclass
class AutoMessage:
    title: str = "Auto Message"
    body: str = ""
    interval_seconds: int = 300
    enabled: bool = False
    next_due: float = 0.0
    last_sent: float = 0.0


class AutoMessageStore:
    """Persistent automated message dashboard data."""

    DEFAULT_MESSAGES = [
        AutoMessage("Welcome Reminder", "Welcome in! Please follow the seller and bookmark the show.", 300, False),
        AutoMessage("Bid Reminder", "Please bid responsibly. All bids are binding.", 600, False),
        AutoMessage("Shipping Reminder", "Shipping is handled through Whatnot after purchase.", 900, False),
    ]

    @classmethod
    def default_messages(cls) -> List[AutoMessage]:
        return [AutoMessage(m.title, m.body, m.interval_seconds, m.enabled, 0.0, 0.0) for m in cls.DEFAULT_MESSAGES]

    @classmethod
    def load(cls) -> List[AutoMessage]:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if not AUTO_MESSAGES_FILE.exists():
            return cls.default_messages()
        try:
            raw = json.loads(AUTO_MESSAGES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return cls.default_messages()
        if not isinstance(raw, list):
            return cls.default_messages()
        messages: List[AutoMessage] = []
        now = time.time()
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", f"Auto Message {i + 1}")).strip() or f"Auto Message {i + 1}"
            body = str(item.get("body", "")).strip()
            try:
                interval = int(item.get("interval_seconds", 300))
            except Exception:
                interval = 300
            interval = max(30, min(interval, 24 * 60 * 60))
            enabled = bool(item.get("enabled", False))
            try:
                next_due = float(item.get("next_due", 0.0) or 0.0)
            except Exception:
                next_due = 0.0
            try:
                last_sent = float(item.get("last_sent", 0.0) or 0.0)
            except Exception:
                last_sent = 0.0
            if enabled and next_due <= 0:
                next_due = now + interval
            messages.append(AutoMessage(title, body, interval, enabled, next_due, last_sent))
        return messages or cls.default_messages()

    @staticmethod
    def save(messages: List[AutoMessage]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = [asdict(m) for m in messages]
        AUTO_MESSAGES_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class AppSettingsStore:
    """Small persistent settings store for app-level UI toggles."""

    DEFAULTS = {
        "enter_to_send": True,
        "announce_mode": False,
        "custom_dash_announce": False,
    }

    @classmethod
    def load(cls) -> Dict[str, object]:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        data = dict(cls.DEFAULTS)
        try:
            raw = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass
        data["enter_to_send"] = bool(data.get("enter_to_send", cls.DEFAULTS["enter_to_send"]))
        data["announce_mode"] = bool(data.get("announce_mode", cls.DEFAULTS["announce_mode"]))
        data["custom_dash_announce"] = bool(data.get("custom_dash_announce", cls.DEFAULTS["custom_dash_announce"]))
        return data

    @staticmethod
    def save(settings: Dict[str, object]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "enter_to_send": bool(settings.get("enter_to_send", True)),
            "announce_mode": bool(settings.get("announce_mode", False)),
            "custom_dash_announce": bool(settings.get("custom_dash_announce", False)),
        }
        APP_SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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
        names["Commands"] = "Custom Dash"
        names.update({"Dashboard": "Dashboard", "Shoes": "Shoes", "Auto": "Automation", "Settings": "Settings"})
        for key in NOTE_TABS:
            names[key] = key
        for key in ["All Shoes", "Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons", "Custom", "Shoe Notes"]:
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
        app_icon = get_app_icon_path()
        if app_icon:
            self.setWindowIcon(QIcon(str(app_icon)))
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 820)
        self.setMinimumSize(1020, 680)
        self.tabs_data, self.notes_data, self.view_mode = ConfigStore.load()
        self.shoe_data = ShoeConfigStore.load()
        self.auto_messages = AutoMessageStore.load()
        self.auto_queue: List[int] = []
        self.auto_running = False
        self.auto_min_gap_seconds = 8
        self.auto_last_send_time = 0.0
        self.auto_status_label: Optional[QLabel] = None
        self.auto_queue_label: Optional[QLabel] = None
        self.auto_message_editors: List[tuple[QLineEdit, QTextEdit, QSpinBox, QCheckBox]] = []
        self.custom_shoe_editors: List[tuple[int, QLineEdit, QTextEdit]] = []
        self.custom_source_combo: Optional[QComboBox] = None
        self.shoe_preferred_subtab: str = ""
        self.hotkeys = HotkeyStore.load()
        self.browser = WhatnotBrowser(self.set_status)
        self.app_settings = AppSettingsStore.load()
        self.enter_to_send = bool(self.app_settings.get("enter_to_send", True))
        self.announce_mode = bool(self.app_settings.get("announce_mode", False))
        self.custom_dash_announce = bool(self.app_settings.get("custom_dash_announce", False))
        self.messages_nav_expanded = False
        self.notes_nav_expanded = False
        self.dashboard_browser_value_label: Optional[QLabel] = None
        self.dashboard_browser_hint_label: Optional[QLabel] = None
        self.custom_dash_source_combo: Optional[QComboBox] = None
        self.custom_dash_announce_box: Optional[QCheckBox] = None
        self.shortcuts: List[QShortcut] = []
        self.message_editors: Dict[str, List[tuple[QLineEdit, QTextEdit]]] = {}
        self.note_editors: Dict[str, QTextEdit] = {}
        self.search = QLineEdit()
        self.mode_combo = QComboBox()
        self.main_tabs = QTabWidget()
        self.nav_buttons: Dict[str, QPushButton] = {}
        self.nav_frame: Optional[QFrame] = None
        self.status = QLabel("Ready.")
        self.license_info = LicenseManager.load()
        self.tab_labels = TabNameStore.load()
        self.tab_name_edits: Dict[str, QLineEdit] = {}
        self._rendering_tab = False
        self.license_overlay: Optional[QWidget] = None
        self._build_ui()
        self.apply_license_gate()
        self.register_hotkeys()
        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(1000)
        self.auto_timer.timeout.connect(self.auto_tick)
        self.auto_timer.start()
        self._is_closing = False
        self._last_saved_snapshot = self._make_state_snapshot()
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(120000)
        self.autosave_timer.timeout.connect(self.periodic_auto_save)
        self.autosave_timer.start()
        self.browser_state_timer = QTimer(self)
        self.browser_state_timer.setInterval(2000)
        self.browser_state_timer.timeout.connect(self.update_browser_status_display)
        self.browser_state_timer.start()
        QTimer.singleShot(250, self.refresh_current_tab)
        QTimer.singleShot(400, self.update_browser_status_display)

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
            label = self.tab_label(str(key))
            self.main_tabs.setTabText(i, label)
        if hasattr(self, "nav_button_layout"):
            self._rebuild_sidebar_buttons()
        if hasattr(self, "hk_tab"):
            current = self.hk_tab.currentData() or self.hk_tab.currentText()
            self.populate_hotkey_target_combo(str(current))
        if hasattr(self, "hotkey_list_host"):
            self.refresh_hotkey_list()

    def _build_sidebar(self) -> QFrame:
        self.nav_frame = QFrame()
        self.nav_frame.setObjectName("navSidebar")
        layout = QVBoxLayout(self.nav_frame)
        layout.setContentsMargins(8, 12, 8, 10)
        layout.setSpacing(2)
        brand = QLabel("WhatMod")
        brand.setObjectName("navBrand")
        layout.addWidget(brand)
        sub = QLabel("Live tools")
        sub.setObjectName("navSub")
        layout.addWidget(sub)
        line = QFrame()
        line.setObjectName("navDivider")
        line.setFixedHeight(1)
        layout.addWidget(line)
        self.nav_button_layout = layout
        layout.addStretch(1)
        return self.nav_frame

    def _clear_sidebar_buttons(self) -> None:
        if not hasattr(self, "nav_button_layout"):
            return
        # Keep brand, subtitle, divider, and the final stretch.
        while self.nav_button_layout.count() > 4:
            item = self.nav_button_layout.takeAt(3)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())
        self.nav_buttons = {}

    def _make_sidebar_button(self, key: str, label: str = "", indent: bool = False) -> QPushButton:
        btn = QPushButton(label or self.tab_label(key))
        btn.setObjectName("navButtonIndented" if indent else "navButton")
        btn.setCheckable(True)
        btn.clicked.connect(lambda _checked=False, k=key: self.main_tabs.setCurrentIndex(self._tab_index_for_key(k)))
        self.nav_buttons[key] = btn
        return btn

    def _make_sidebar_section(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("navSection")
        return label

    def _toggle_message_nav(self) -> None:
        self.messages_nav_expanded = not bool(getattr(self, "messages_nav_expanded", False))
        self._rebuild_sidebar_buttons()

    def _toggle_notes_nav(self) -> None:
        self.notes_nav_expanded = not bool(getattr(self, "notes_nav_expanded", False))
        self._rebuild_sidebar_buttons()

    def _rebuild_sidebar_buttons(self) -> None:
        if not hasattr(self, "nav_button_layout"):
            return
        self._clear_sidebar_buttons()
        insert_at = 3

        def add_widget(widget):
            nonlocal insert_at
            self.nav_button_layout.insertWidget(insert_at, widget)
            insert_at += 1

        add_widget(self._make_sidebar_button("Dashboard", "Dashboard"))
        add_widget(self._make_sidebar_button("Commands", "Custom Dash"))

        toggle = QPushButton("▾  Messages" if getattr(self, "messages_nav_expanded", False) else "▸  Messages")
        toggle.setObjectName("navGroupButton")
        toggle.clicked.connect(self._toggle_message_nav)
        add_widget(toggle)
        if getattr(self, "messages_nav_expanded", False):
            for key in ["M1", "M2", "M3", "M4", "Announcements"]:
                add_widget(self._make_sidebar_button(key, self.tab_label(key), indent=True))

        add_widget(self._make_sidebar_button("Shoes", "Shoes"))
        add_widget(self._make_sidebar_button("Auto", "Automation"))

        notes_toggle = QPushButton("▾  Notes" if getattr(self, "notes_nav_expanded", False) else "▸  Notes")
        notes_toggle.setObjectName("navGroupButton")
        notes_toggle.clicked.connect(self._toggle_notes_nav)
        add_widget(notes_toggle)
        if getattr(self, "notes_nav_expanded", False):
            for key in NOTE_TABS:
                add_widget(self._make_sidebar_button(key, self.tab_label(key), indent=True))

        settings_btn = self._make_sidebar_button("Settings", "Settings")
        # Push Settings to the bottom without filling the sidebar with section labels.
        self.nav_button_layout.insertWidget(max(self.nav_button_layout.count() - 1, 3), settings_btn)
        self._sync_sidebar_selection()

    def _add_sidebar_button(self, key: str, index: int) -> None:
        # Kept for backwards compatibility with older internal calls.
        self._rebuild_sidebar_buttons()

    def _sync_sidebar_selection(self) -> None:
        current = self._current_tab_name() if self.main_tabs.count() else ""
        for key, btn in self.nav_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(key == current)
            btn.setProperty("active", key == current)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.blockSignals(False)

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
        subtitle = QLabel("A calmer command center for live moderation, automation, notes, and quick sends.")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box, 1)

        actions = QVBoxLayout()
        row1 = QHBoxLayout()
        for text, fn, obj in [
            ("Launch / Reconnect Browser", self.launch_browser, "primaryButton"),
            ("Save All", self.save_all, "successButton"),
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
        self.enter_box.setChecked(self.enter_to_send)
        self.enter_box.toggled.connect(self.set_enter_to_send)
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
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._build_sidebar())
        content_shell = QFrame()
        content_shell.setObjectName("contentShell")
        content_layout = QVBoxLayout(content_shell)
        content_layout.setContentsMargins(14, 14, 14, 14)
        content_layout.setSpacing(8)
        self.main_tabs.setDocumentMode(True)
        self.main_tabs.tabBar().hide()
        self.main_tabs.currentChanged.connect(lambda _idx: (self._sync_sidebar_selection(), self.refresh_current_tab()))
        content_layout.addWidget(self.main_tabs, 1)
        body_layout.addWidget(content_shell, 1)
        outer.addWidget(body, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(18, 6, 18, 6)
        footer.addWidget(self.status, 1)
        outer.addLayout(footer)

        self.add_main_tab(self._build_dashboard_tab(), "Dashboard")
        for name in MESSAGE_TABS:
            self.add_main_tab(QWidget(), name)
        self.add_main_tab(QWidget(), "Shoes")
        self.add_main_tab(self._build_auto_tab(), "Auto")
        for name in NOTE_TABS:
            self.add_main_tab(self._build_single_note_tab(name), name)
        self.add_main_tab(self._build_settings_tab(), "Settings")
        self._rebuild_sidebar_buttons()

    def _dashboard_metric_card(self, title: str, value: str, hint: str) -> QFrame:
        card = QFrame()
        card.setObjectName("dashboardCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        value_label = QLabel(value)
        value_label.setObjectName("metricNumber")
        title_label = QLabel(title)
        title_label.setObjectName("metricLabel")
        hint_label = QLabel(hint)
        hint_label.setObjectName("sectionHint")
        hint_label.setWordWrap(True)
        if title == "Browser":
            self.dashboard_browser_value_label = value_label
            self.dashboard_browser_hint_label = hint_label
        layout.addWidget(value_label)
        layout.addWidget(title_label)
        layout.addWidget(hint_label)
        return card

    def update_browser_status_display(self) -> None:
        ready = False
        try:
            ready = bool(self.browser.refresh_connection_state())
        except Exception:
            ready = False
        if getattr(self, "dashboard_browser_value_label", None) is not None:
            self.dashboard_browser_value_label.setText("Online" if ready else "Offline")
            self.dashboard_browser_value_label.setStyleSheet(
                "color: #42D392; font-size: 28px; font-weight: 900;" if ready
                else "color: #FF6B7A; font-size: 28px; font-weight: 900;"
            )
        if getattr(self, "dashboard_browser_hint_label", None) is not None:
            self.dashboard_browser_hint_label.setText("Connected. Open a live show and use cards to send." if ready else "Connect when you are inside a live Whatnot show.")


    def _build_dashboard_tab(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(16)

        hero = QFrame()
        hero.setObjectName("dashboardCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(22, 20, 22, 20)
        hero_layout.setSpacing(10)
        title = QLabel("Live workspace")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Launch Whatnot, choose the workflow you need, and keep the noisy tools tucked away until they matter.")
        subtitle.setObjectName("sectionHint")
        subtitle.setWordWrap(True)
        hero_layout.addWidget(title)
        hero_layout.addWidget(subtitle)
        actions = QHBoxLayout()
        for text, fn, obj in [
            ("Launch Browser", self.launch_browser, "primaryButton"),
            ("Open Custom Dash", lambda: self.main_tabs.setCurrentIndex(self._tab_index_for_key("Commands")), ""),
            ("Open Messages", lambda: self.main_tabs.setCurrentIndex(self._tab_index_for_key("M1")), ""),
            ("Open Automation", lambda: self.main_tabs.setCurrentIndex(self._tab_index_for_key("Auto")), ""),
        ]:
            btn = QPushButton(text)
            if obj:
                btn.setObjectName(obj)
            btn.clicked.connect(fn)
            actions.addWidget(btn)
        actions.addStretch(1)
        hero_layout.addLayout(actions)
        outer.addWidget(hero)

        metrics = QHBoxLayout()
        total_messages = sum(len(self.tabs_data.get(tab, [])) for tab in MESSAGE_TABS if tab != "Commands")
        custom_dash_cards = len(self.tabs_data.get("Commands", []))
        custom_cards = len(self.shoe_data.get("Custom", []))
        auto_enabled = sum(1 for m in self.auto_messages if m.enabled and m.body.strip())
        metrics.addWidget(self._dashboard_metric_card("Custom Dash", str(custom_dash_cards), "Your hand-picked command board for live shows."))
        metrics.addWidget(self._dashboard_metric_card("Messages", str(total_messages), "Saved quick-send cards across your message banks."))
        metrics.addWidget(self._dashboard_metric_card("Automation", f"{auto_enabled} on", "Enabled timed messages ready for live shows."))
        metrics.addWidget(self._dashboard_metric_card("Browser", "Online" if self.browser.is_ready else "Offline", "Connected. Open a live show and use cards to send." if self.browser.is_ready else "Connect when you are inside a live Whatnot show."))
        outer.addLayout(metrics)

        workflow = QFrame()
        workflow.setObjectName("dashboardCard")
        wf_layout = QVBoxLayout(workflow)
        wf_layout.setContentsMargins(20, 18, 20, 18)
        wf_title = QLabel("Suggested workflow")
        wf_title.setObjectName("sectionTitle")
        wf_layout.addWidget(wf_title)
        steps = QLabel("1. Launch browser and log into Whatnot.\n2. Use Messages or Shoes for fast live sends.\n3. Start Automation only after your show is ready.\n4. Keep Notes open for incidents, giveaway info, and follow-ups.")
        steps.setObjectName("sectionHint")
        steps.setWordWrap(True)
        wf_layout.addWidget(steps)
        outer.addWidget(workflow)
        outer.addStretch(1)
        return w

    def _tab_index_for_key(self, key: str) -> int:
        for i in range(self.main_tabs.count()):
            if self._tab_key_at(i) == key:
                return i
        return 0

    def _build_auto_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        header = QLabel("Auto Message Control Center")
        header.setStyleSheet("font-size: 22px; font-weight: 800;")
        v.addWidget(header)
        hint = QLabel("Create timed chat messages, start/stop automation, and manage the send queue. Auto messages never send at the exact same time; due messages are placed into one queue and sent one at a time.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #CFC7E8;")
        v.addWidget(hint)

        controls = QFrame(); controls.setObjectName("settingsCard")
        controls_layout = QGridLayout(controls)
        self.auto_status_label = QLabel("Auto is stopped.")
        self.auto_status_label.setStyleSheet("font-size: 18px; font-weight: 800;")
        self.auto_queue_label = QLabel("Queue: 0")
        controls_layout.addWidget(self.auto_status_label, 0, 0, 1, 3)
        controls_layout.addWidget(self.auto_queue_label, 0, 3)

        start_btn = QPushButton("Start Auto")
        start_btn.clicked.connect(self.start_auto_messages)
        stop_btn = QPushButton("Stop Auto")
        stop_btn.setObjectName("goldButton")
        stop_btn.clicked.connect(self.stop_auto_messages)
        add_btn = QPushButton("Add Message")
        add_btn.clicked.connect(self.add_auto_message)
        save_btn = QPushButton("Save Auto Settings")
        save_btn.clicked.connect(self.save_auto_settings)
        clear_btn = QPushButton("Clear Queue")
        clear_btn.setObjectName("softButton")
        clear_btn.clicked.connect(self.clear_auto_queue)
        send_next_btn = QPushButton("Send Next Now")
        send_next_btn.clicked.connect(self.send_next_auto_now)
        controls_layout.addWidget(start_btn, 1, 0)
        controls_layout.addWidget(stop_btn, 1, 1)
        controls_layout.addWidget(add_btn, 1, 2)
        controls_layout.addWidget(save_btn, 1, 3)
        controls_layout.addWidget(clear_btn, 2, 0)
        controls_layout.addWidget(send_next_btn, 2, 1)
        v.addWidget(controls)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        cont = QWidget(); self.auto_cards_layout = QVBoxLayout(cont)
        scroll.setWidget(cont)
        v.addWidget(scroll, 1)
        self.render_auto_cards()
        self.update_auto_status_labels()
        return w

    def render_auto_cards(self):
        if not hasattr(self, "auto_cards_layout"):
            return
        while self.auto_cards_layout.count():
            item = self.auto_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())
        self.auto_message_editors = []
        if not self.auto_messages:
            self.auto_cards_layout.addWidget(QLabel("No auto messages yet. Click Add Message to create one."))
            self.auto_cards_layout.addStretch(1)
            return
        for i, msg in enumerate(self.auto_messages):
            box = QGroupBox(f"Auto {i + 1}: {msg.title or 'Untitled'}")
            grid = QGridLayout(box)
            enabled = QCheckBox("Enabled")
            enabled.setChecked(bool(msg.enabled))
            title = QLineEdit(msg.title)
            body = QTextEdit(); body.setPlainText(msg.body); body.setFixedHeight(84)
            interval = QSpinBox(); interval.setRange(30, 86400); interval.setSingleStep(30); interval.setValue(max(30, int(msg.interval_seconds)))
            interval.setSuffix(" sec")
            status = QLabel(self.auto_message_status_text(i))
            status.setWordWrap(True)
            grid.addWidget(enabled, 0, 0)
            grid.addWidget(QLabel("Title"), 0, 1)
            grid.addWidget(title, 0, 2, 1, 3)
            grid.addWidget(QLabel("Every"), 1, 0)
            grid.addWidget(interval, 1, 1)
            grid.addWidget(status, 1, 2, 1, 3)
            grid.addWidget(QLabel("Message"), 2, 0)
            grid.addWidget(body, 2, 1, 1, 4)
            save = QPushButton("Save")
            save.clicked.connect(lambda _=False, ix=i: self.save_one_auto_message(ix))
            queue = QPushButton("Queue Now")
            queue.clicked.connect(lambda _=False, ix=i: self.queue_auto_message(ix, manual=True))
            send = QPushButton("Send Now")
            send.clicked.connect(lambda _=False, ix=i: self.send_auto_message_now(ix))
            reset = QPushButton("Reset Timer")
            reset.clicked.connect(lambda _=False, ix=i: self.reset_auto_timer(ix))
            remove = QPushButton("Remove")
            remove.setObjectName("goldButton")
            remove.clicked.connect(lambda _=False, ix=i: self.remove_auto_message(ix))
            grid.addWidget(save, 3, 0)
            grid.addWidget(queue, 3, 1)
            grid.addWidget(send, 3, 2)
            grid.addWidget(reset, 3, 3)
            grid.addWidget(remove, 3, 4)
            self.auto_cards_layout.addWidget(box)
            self.auto_message_editors.append((title, body, interval, enabled))
        self.auto_cards_layout.addStretch(1)

    def collect_auto_editors(self):
        editors = getattr(self, "auto_message_editors", [])
        if not editors:
            return
        for i, (title, body, interval, enabled) in enumerate(editors):
            if i >= len(self.auto_messages):
                continue
            old_interval = int(self.auto_messages[i].interval_seconds)
            was_enabled = bool(self.auto_messages[i].enabled)
            self.auto_messages[i].title = title.text().strip() or f"Auto Message {i + 1}"
            self.auto_messages[i].body = body.toPlainText().strip()
            self.auto_messages[i].interval_seconds = int(interval.value())
            self.auto_messages[i].enabled = bool(enabled.isChecked())
            if self.auto_messages[i].enabled and (not was_enabled or old_interval != self.auto_messages[i].interval_seconds or self.auto_messages[i].next_due <= 0):
                self.auto_messages[i].next_due = time.time() + self.auto_messages[i].interval_seconds
            if not self.auto_messages[i].enabled:
                self.auto_messages[i].next_due = 0.0

    def save_auto_settings(self):
        self.collect_auto_editors()
        AutoMessageStore.save(self.auto_messages)
        self.render_auto_cards()
        self.update_auto_status_labels()
        self.set_status("Auto message settings saved.")

    def add_auto_message(self):
        self.collect_auto_editors()
        self.auto_messages.append(AutoMessage(f"Auto Message {len(self.auto_messages) + 1}", "", 300, False, 0.0, 0.0))
        AutoMessageStore.save(self.auto_messages)
        self.render_auto_cards()
        self.update_auto_status_labels()
        self.set_status("Added auto message.")

    def remove_auto_message(self, index: int):
        self.collect_auto_editors()
        if not (0 <= index < len(self.auto_messages)):
            return
        title = self.auto_messages[index].title
        if QMessageBox.question(self, "Remove Auto Message", f"Remove '{title}'?") != QMessageBox.Yes:
            return
        self.auto_messages.pop(index)
        self.auto_queue = [ix for ix in self.auto_queue if ix != index]
        self.auto_queue = [ix - 1 if ix > index else ix for ix in self.auto_queue]
        AutoMessageStore.save(self.auto_messages)
        self.render_auto_cards()
        self.update_auto_status_labels()
        self.set_status(f"Removed auto message: {title}")

    def start_auto_messages(self):
        self.collect_auto_editors()
        now = time.time()
        enabled_count = 0
        for msg in self.auto_messages:
            if msg.enabled and msg.body.strip():
                enabled_count += 1
                if msg.next_due <= now:
                    msg.next_due = now + max(30, int(msg.interval_seconds))
        AutoMessageStore.save(self.auto_messages)
        self.auto_running = True
        self.render_auto_cards()
        self.update_auto_status_labels()
        self.set_status(f"Auto messages started. {enabled_count} enabled message(s).")

    def stop_auto_messages(self):
        self.auto_running = False
        self.clear_auto_queue(silent=True)
        self.update_auto_status_labels()
        self.set_status("Auto messages stopped and queued messages cleared.")

    def clear_auto_queue(self, silent: bool=False):
        self.auto_queue.clear()
        self.update_auto_status_labels()
        if not silent:
            self.set_status("Auto queue cleared.")

    def queue_auto_message(self, index: int, manual: bool=False):
        self.collect_auto_editors()
        if not (0 <= index < len(self.auto_messages)):
            return
        if not self.auto_messages[index].body.strip():
            self.set_status("Cannot queue an empty auto message.")
            return
        if index not in self.auto_queue:
            self.auto_queue.append(index)
        if manual:
            self.set_status(f"Queued auto message: {self.auto_messages[index].title}")
        self.update_auto_status_labels()

    def send_next_auto_now(self):
        if not self.auto_queue:
            self.set_status("Auto queue is empty.")
            return
        self.process_auto_queue(force=True)

    def send_auto_message_now(self, index: int):
        self.collect_auto_editors()
        if not (0 <= index < len(self.auto_messages)):
            return
        self._send_auto_index(index)
        self.auto_messages[index].next_due = time.time() + max(30, int(self.auto_messages[index].interval_seconds))
        AutoMessageStore.save(self.auto_messages)
        self.render_auto_cards()
        self.update_auto_status_labels()

    def reset_auto_timer(self, index: int):
        self.collect_auto_editors()
        if not (0 <= index < len(self.auto_messages)):
            return
        self.auto_messages[index].next_due = time.time() + max(30, int(self.auto_messages[index].interval_seconds)) if self.auto_messages[index].enabled else 0.0
        AutoMessageStore.save(self.auto_messages)
        self.render_auto_cards()
        self.update_auto_status_labels()
        self.set_status(f"Reset timer for {self.auto_messages[index].title}.")

    def auto_tick(self):
        if not getattr(self, "auto_running", False):
            self.update_auto_status_labels(light=True)
            return
        now = time.time()
        for i, msg in enumerate(self.auto_messages):
            if not msg.enabled or not msg.body.strip():
                continue
            if msg.next_due <= 0:
                msg.next_due = now + max(30, int(msg.interval_seconds))
            if now >= msg.next_due:
                self.queue_auto_message(i)
                # Schedule the next cycle immediately so two due timers do not keep re-queueing.
                msg.next_due = now + max(30, int(msg.interval_seconds))
        self.process_auto_queue()
        self.update_auto_status_labels(light=True)

    def process_auto_queue(self, force: bool=False):
        if not self.auto_queue:
            return
        now = time.time()
        if not force and now - self.auto_last_send_time < self.auto_min_gap_seconds:
            return
        index = self.auto_queue.pop(0)
        if not (0 <= index < len(self.auto_messages)):
            self.update_auto_status_labels()
            return
        self._send_auto_index(index)
        self.auto_messages[index].next_due = time.time() + max(30, int(self.auto_messages[index].interval_seconds))
        AutoMessageStore.save(self.auto_messages)
        self.update_auto_status_labels()

    def _send_auto_index(self, index: int):
        msg = self.auto_messages[index]
        try:
            self.send_message(msg.body)
            msg.last_sent = time.time()
            self.auto_last_send_time = time.time()
            self.set_status(f"Auto sent: {msg.title}")
        except Exception as exc:
            self.set_status(f"Auto send failed: {exc}")

    def auto_message_status_text(self, index: int) -> str:
        if not (0 <= index < len(self.auto_messages)):
            return ""
        msg = self.auto_messages[index]
        parts = []
        if msg.enabled:
            if msg.next_due > 0:
                remaining = max(0, int(msg.next_due - time.time()))
                parts.append(f"Next in {self.format_duration(remaining)}")
            else:
                parts.append("Timer not started")
        else:
            parts.append("Disabled")
        if msg.last_sent > 0:
            parts.append("Last sent " + time.strftime("%I:%M:%S %p", time.localtime(msg.last_sent)))
        if index in self.auto_queue:
            parts.append("Queued")
        return " · ".join(parts)

    def update_auto_status_labels(self, light: bool=False):
        if self.auto_status_label is not None:
            enabled = sum(1 for m in self.auto_messages if m.enabled and m.body.strip())
            self.auto_status_label.setText(("Auto is running" if self.auto_running else "Auto is stopped") + f" · {enabled} enabled")
        if self.auto_queue_label is not None:
            queued_names = [self.auto_messages[ix].title for ix in self.auto_queue if 0 <= ix < len(self.auto_messages)]
            self.auto_queue_label.setText("Queue: " + (", ".join(queued_names) if queued_names else "0"))
        if not light and self._current_tab_name() == "Auto":
            self.render_auto_cards()

    @staticmethod
    def format_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def refresh_auto_dashboard(self):
        self.collect_auto_editors()
        self.render_auto_cards()
        self.update_auto_status_labels()

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
        title = QLabel("General Settings")
        title.setStyleSheet("font-size: 22px; font-weight: 800;")
        v.addWidget(title)
        general_card = QFrame(); general_card.setObjectName("settingsCard")
        general_layout = QVBoxLayout(general_card)
        self.announce_box = QCheckBox("/Announce")
        self.announce_box.setChecked(self.announce_mode)
        self.announce_box.setToolTip("When enabled, WhatMod adds '/announce ' before every sent or copied message.")
        self.announce_box.toggled.connect(self.set_announce_mode)
        general_layout.addWidget(self.announce_box)
        announce_hint = QLabel("When on, every outgoing WhatMod message is prefixed with /announce so Whatnot can pin it.")
        announce_hint.setStyleSheet("color: #CFC7E8;")
        announce_hint.setWordWrap(True)
        general_layout.addWidget(announce_hint)
        v.addWidget(general_card)

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
            ("Utility Tabs", ["Dashboard", "Shoes", "Auto", *NOTE_TABS, "Settings"]),
            ("Shoe Sub-Tabs", ["All Shoes", "Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons", "Custom", "Shoe Notes"]),
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
        if name == "Commands":
            self._render_custom_dash_tab()
        elif name in MESSAGE_TABS:
            self._render_message_tab(name)
        elif name == "Shoes":
            self._render_shoes_tab()
        elif name == "Auto":
            self.refresh_auto_dashboard()

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

    def add_message_card(self, tab: str):
        """Add a new editable message card to a standard message tab."""
        self.collect_tab(tab)
        arr = self.tabs_data.setdefault(tab, [])
        arr.append(MessageSlot(f"Message {len(arr) + 1}", ""))
        self.refresh_current_tab()
        self.set_status(f"Added message card to {self.tab_label(tab)}.")

    def remove_message_card(self, tab: str, index: int):
        """Completely remove one message card from a standard message tab."""
        self.collect_tab(tab)
        arr = self.tabs_data.setdefault(tab, [])
        if not (0 <= index < len(arr)):
            return
        title = arr[index].title.strip() or f"Message {index + 1}"
        if QMessageBox.question(self, "Remove Message Card", f"Remove '{title}' from {self.tab_label(tab)}?") != QMessageBox.Yes:
            return
        arr.pop(index)
        # Clean any hotkeys pointing at this removed/shifted card.
        for seq, payload in list(self.hotkeys.items()):
            if payload.get("tab") != tab:
                continue
            ix = int(payload.get("index", -1))
            if ix == index:
                del self.hotkeys[seq]
            elif ix > index:
                payload["index"] = ix - 1
        self.save_hotkeys()
        self.register_hotkeys()
        self.refresh_current_tab()
        self.set_status(f"Removed message card from {self.tab_label(tab)}.")

    def clear_message_tab(self, tab: str):
        """Remove every card from a standard message tab after confirmation."""
        self.collect_tab(tab)
        arr = self.tabs_data.setdefault(tab, [])
        if not arr:
            self.set_status(f"{self.tab_label(tab)} is already empty.")
            return
        if QMessageBox.question(self, "Clear Message Tab", f"Remove all cards from {self.tab_label(tab)}?") != QMessageBox.Yes:
            return
        self.tabs_data[tab] = []
        for seq, payload in list(self.hotkeys.items()):
            if payload.get("tab") == tab:
                del self.hotkeys[seq]
        self.save_hotkeys()
        self.register_hotkeys()
        self.refresh_current_tab()
        self.set_status(f"Cleared all cards from {self.tab_label(tab)}.")

    def custom_dash_source_items(self) -> List[tuple[str, str, str]]:
        """Return clean import choices for the Custom Dash."""
        items: List[tuple[str, str, str]] = []
        for tab in ["M1", "M2", "M3", "M4", "Announcements", "Givey Messages"]:
            for slot in self.tabs_data.get(tab, []):
                title = (slot.title or "Untitled").strip()
                body = (slot.body or "").strip()
                if body:
                    items.append((f"{self.tab_label(tab)} · {title}", title, body))
        for key in ["Men Sizes", "Women Sizes", "M/W Conversion", "Status Buttons", "Custom"]:
            for card in self.shoe_tab_items(key):
                title = str(card.get("title", "")).strip() or "Shoe Card"
                body = str(card.get("body", "")).strip()
                if body:
                    items.append((f"Shoes · {self.tab_label(key)} · {title}", title, body))
        return items

    def add_custom_dash_card(self):
        self.collect_tab("Commands")
        arr = self.tabs_data.setdefault("Commands", [])
        arr.append(MessageSlot(f"Custom Card {len(arr) + 1}", ""))
        self._render_custom_dash_tab()
        self.set_status("Added Custom Dash card.")

    def import_custom_dash_card(self):
        self.collect_tab("Commands")
        if not self.custom_dash_source_combo:
            return
        data = self.custom_dash_source_combo.currentData()
        if not isinstance(data, dict):
            self.set_status("Choose a card to import first.")
            return
        self.tabs_data.setdefault("Commands", []).append(MessageSlot(str(data.get("title", "Imported Card")), str(data.get("body", ""))))
        self._render_custom_dash_tab()
        self.set_status("Imported card to Custom Dash.")

    def set_custom_dash_announce(self, checked: bool):
        self.custom_dash_announce = bool(checked)
        self.save_app_settings()
        self.set_status("Custom Dash /announce enabled." if self.custom_dash_announce else "Custom Dash /announce disabled.")

    def send_custom_dash_message(self, message: str):
        msg = (message or "").strip()
        if not msg:
            return
        if self.custom_dash_announce and not msg.lower().startswith("/announce"):
            msg = add_announce_prefix(msg)
        self.send_message(msg)

    def copy_custom_dash_message(self, message: str):
        msg = (message or "").strip()
        if self.custom_dash_announce and not msg.lower().startswith("/announce"):
            msg = add_announce_prefix(msg)
        self.copy_to_clipboard(msg, apply_announce=False)

    def _render_custom_dash_tab(self):
        self.collect_tab("Commands")
        scroll, cont, layout = self._scroll_widget()
        q = self.search.text().strip().lower()
        self.message_editors["Commands"] = []

        top_card = QFrame(); top_card.setObjectName("settingsCard")
        top = QGridLayout(top_card)
        title = QLabel("Custom Dash")
        title.setObjectName("sectionTitle")
        hint = QLabel("Build a focused live dashboard. Add blank cards for your own commands, or import from existing messages and shoe cards.")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        add_btn = QPushButton("+ Blank Card")
        add_btn.setObjectName("successButton")
        add_btn.clicked.connect(self.add_custom_dash_card)
        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("dangerButton")
        clear_btn.clicked.connect(lambda _=False: self.clear_message_tab("Commands"))
        self.custom_dash_announce_box = QCheckBox("/announce for this dash")
        self.custom_dash_announce_box.setChecked(bool(self.custom_dash_announce))
        self.custom_dash_announce_box.toggled.connect(self.set_custom_dash_announce)
        top.addWidget(title, 0, 0)
        top.addWidget(QLabel(f"{len(self.tabs_data.get('Commands', []))} card(s)"), 0, 1)
        top.addWidget(self.custom_dash_announce_box, 0, 2)
        top.addWidget(add_btn, 0, 3)
        top.addWidget(clear_btn, 0, 4)
        top.addWidget(hint, 1, 0, 1, 5)

        import_row = QHBoxLayout()
        self.custom_dash_source_combo = QComboBox()
        self.custom_dash_source_combo.addItem("Choose an existing card to import…", None)
        for label, card_title, body in self.custom_dash_source_items():
            self.custom_dash_source_combo.addItem(label, {"title": card_title, "body": body})
        import_btn = QPushButton("Import Selected")
        import_btn.setObjectName("ghostButton")
        import_btn.clicked.connect(self.import_custom_dash_card)
        import_row.addWidget(self.custom_dash_source_combo, 1)
        import_row.addWidget(import_btn)
        top.addLayout(import_row, 2, 0, 1, 5)
        layout.addWidget(top_card)

        arr = self.tabs_data.get("Commands", [])
        if self.view_mode == "Edit":
            if not arr:
                empty = QLabel("Custom Dash is empty. Click + Blank Card or import an existing card.")
                empty.setStyleSheet("color: #9BA3AF; padding: 14px;")
                layout.addWidget(empty)
            for i, slot in enumerate(arr, 1):
                box = QGroupBox(f"{i:02d} · {slot.title or 'Untitled'}")
                grid = QGridLayout(box)
                title_edit = QLineEdit(slot.title)
                body_edit = QTextEdit(); body_edit.setPlainText(slot.body); body_edit.setFixedHeight(86)
                grid.addWidget(QLabel("Title"), 0, 0)
                grid.addWidget(title_edit, 0, 1, 1, 5)
                grid.addWidget(QLabel("Message"), 1, 0)
                grid.addWidget(body_edit, 1, 1, 1, 5)
                actions = [
                    ("Send", lambda _=False, b=body_edit: self.send_custom_dash_message(b.toPlainText()), "primaryButton"),
                    ("Copy", lambda _=False, b=body_edit: self.copy_custom_dash_message(b.toPlainText()), "ghostButton"),
                    ("Duplicate", lambda _=False, ix=i-1: self.duplicate_message("Commands", ix), ""),
                    ("Assign Key", lambda _=False, ix=i-1: self.assign_hotkey_dialog("Commands", ix), "ghostButton"),
                    ("Remove", lambda _=False, ix=i-1: self.remove_message_card("Commands", ix), "dangerButton"),
                ]
                for col, (txt, cb, obj) in enumerate(actions):
                    btn = QPushButton(txt)
                    if obj:
                        btn.setObjectName(obj)
                    btn.clicked.connect(cb)
                    grid.addWidget(btn, 2, col)
                layout.addWidget(box)
                self.message_editors["Commands"].append((title_edit, body_edit))
        else:
            gridw = QWidget(); grid = QGridLayout(gridw); grid.setAlignment(Qt.AlignmentFlag.AlignTop); layout.addWidget(gridw)
            items = [(i, s) for i, s in enumerate(arr) if not q or q in (s.title + ' ' + s.body).lower()]
            for n, (i, slot) in enumerate(items):
                card = MessageCardWidget(slot.title or f"Custom Card {i+1}", slot.body, self.send_custom_dash_message, self.copy_custom_dash_message, lambda ix=i: self.assign_hotkey_dialog("Commands", ix))
                grid.addWidget(card, n//3, n%3)
            grid.setRowStretch((len(items)+2)//3, 1)
            if not items:
                layout.addWidget(QLabel("No matching Custom Dash cards." if arr else "Custom Dash is empty. Switch to Edit view to add cards."))
        self._replace_tab_widget("Commands", scroll)
        self.set_status("Custom Dash ready.")

    def _render_message_tab(self, tab_name: str):
        self.collect_tab(tab_name)
        scroll, cont, layout = self._scroll_widget()
        q = self.search.text().strip().lower()
        self.message_editors[tab_name] = []

        top_card = QFrame(); top_card.setObjectName("settingsCard")
        top_layout = QGridLayout(top_card)
        section = QLabel(f"{self.tab_label(tab_name)} Cards")
        section.setObjectName("sectionTitle")
        count_label = QLabel(f"{len(self.tabs_data.get(tab_name, []))} card(s)")
        count_label.setObjectName("sectionHint")
        add_btn = QPushButton("+ Add Card")
        add_btn.setObjectName("successButton")
        add_btn.clicked.connect(lambda _=False, t=tab_name: self.add_message_card(t))
        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("dangerButton")
        clear_btn.clicked.connect(lambda _=False, t=tab_name: self.clear_message_tab(t))
        top_layout.addWidget(section, 0, 0)
        top_layout.addWidget(count_label, 0, 1)
        top_layout.addWidget(add_btn, 0, 2)
        top_layout.addWidget(clear_btn, 0, 3)
        hint = QLabel("Add, edit, remove, copy, send, or assign hotkeys. Deleted cards are removed from saved data, not just hidden.")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        top_layout.addWidget(hint, 1, 0, 1, 4)
        layout.addWidget(top_card)

        if self.view_mode == "Edit":
            arr = self.tabs_data.get(tab_name, [])
            if not arr:
                empty = QLabel("This tab is empty. Click + Add Card to create a new message.")
                empty.setStyleSheet("color: #CFC7E8; padding: 14px;")
                layout.addWidget(empty)
            for i, slot in enumerate(arr, 1):
                box = QGroupBox(f"{i:02d} · {slot.title or 'Untitled'}"); grid = QGridLayout(box)
                title = QLineEdit(slot.title); body = QTextEdit(); body.setPlainText(slot.body); body.setFixedHeight(72)
                grid.addWidget(QLabel("Title"), 0, 0); grid.addWidget(title, 0, 1, 1, 5)
                grid.addWidget(QLabel("Body"), 1, 0); grid.addWidget(body, 1, 1, 1, 5)
                actions = [
                    ("Send", lambda _=False, b=body: self.send_message(b.toPlainText()), ""),
                    ("Copy", lambda _=False, b=body: self.copy_to_clipboard(b.toPlainText()), "ghostButton"),
                    ("Duplicate", lambda _=False, ix=i-1, t=tab_name: self.duplicate_message(t, ix), ""),
                    ("Assign Key", lambda _=False, ix=i-1, t=tab_name: self.assign_hotkey_dialog(t, ix), "ghostButton"),
                    ("Clear", lambda _=False, te=title, be=body, ix=i: (te.setText(f"Message {ix}"), be.clear()), "ghostButton"),
                    ("Remove", lambda _=False, ix=i-1, t=tab_name: self.remove_message_card(t, ix), "dangerButton"),
                ]
                for col, (txt, cb, obj) in enumerate(actions):
                    btn = QPushButton(txt)
                    if obj:
                        btn.setObjectName(obj)
                    btn.clicked.connect(cb)
                    grid.addWidget(btn, 2, col)
                layout.addWidget(box); self.message_editors[tab_name].append((title, body))
        else:
            gridw = QWidget(); grid = QGridLayout(gridw); grid.setAlignment(Qt.AlignmentFlag.AlignTop); layout.addWidget(gridw)
            items = [(i,s) for i,s in enumerate(self.tabs_data.get(tab_name, [])) if not q or q in (s.title+' '+s.body).lower()]
            for n, (i, slot) in enumerate(items):
                card = MessageCardWidget(slot.title or f"Message {i+1}", slot.body, self.send_message, self.copy_to_clipboard, lambda ix=i,t=tab_name: self.assign_hotkey_dialog(t, ix))
                grid.addWidget(card, n//3, n%3)
            grid.setRowStretch((len(items)+2)//3, 1)
            if not items: layout.addWidget(QLabel("No matching messages." if self.tabs_data.get(tab_name) else "This tab is empty. Switch to Edit view and click + Add Card."))
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

    def shoe_database_cards(self) -> List[Dict[str, str]]:
        """All built-in/generated shoe cards that can be copied into the Custom shoe dashboard."""
        cards: List[Dict[str, str]] = []
        for group in ["Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons"]:
            for item in self.shoe_tab_items(group):
                title = str(item.get("title", "")).strip()
                body = str(item.get("body", "")).strip()
                if title or body:
                    cards.append({"title": title or "Shoe Message", "body": body})
        return cards

    def custom_source_label(self, item: Dict[str, str]) -> str:
        title = str(item.get("title", "")).strip() or "Shoe Message"
        body = str(item.get("body", "")).replace("\n", " ").strip()
        preview = body[:60] + ("..." if len(body) > 60 else "")
        return f"{title} — {preview}" if preview else title

    def populate_custom_source_combo(self) -> None:
        combo = getattr(self, "custom_source_combo", None)
        if combo is None:
            return
        combo.clear()
        for item in self.shoe_database_cards():
            combo.addItem(self.custom_source_label(item), item)

    def collect_custom_shoe_editors(self) -> None:
        editors = getattr(self, "custom_shoe_editors", [])
        if not editors:
            return
        custom: List[Dict[str, str]] = []
        existing = list(self.shoe_data.get("Custom", []))
        by_index: Dict[int, Dict[str, str]] = {}
        for original_index, title, body in editors:
            title_text = title.text().strip() or f"Custom Shoe Message {original_index + 1}"
            body_text = body.toPlainText().strip()
            by_index[int(original_index)] = {"title": title_text, "body": body_text}
        for i, item in enumerate(existing):
            if i in by_index:
                item = by_index[i]
            if str(item.get("title", "")).strip() or str(item.get("body", "")).strip():
                custom.append({"title": str(item.get("title", "")).strip() or f"Custom Shoe Message {i + 1}", "body": str(item.get("body", "")).strip()})
        self.shoe_data["Custom"] = custom

    def add_selected_custom_shoe_card(self) -> None:
        self.collect_custom_shoe_editors()
        combo = getattr(self, "custom_source_combo", None)
        if combo is None or combo.currentIndex() < 0:
            self.set_status("No shoe message selected to add.")
            return
        item = combo.currentData()
        if not isinstance(item, dict):
            self.set_status("No shoe message selected to add.")
            return
        self.shoe_data.setdefault("Custom", []).append({
            "title": str(item.get("title", "Custom Shoe Message")).strip() or "Custom Shoe Message",
            "body": str(item.get("body", "")).strip(),
        })
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab("Added selected shoe message to Custom.")

    def add_blank_custom_shoe_card(self) -> None:
        self.collect_custom_shoe_editors()
        self.shoe_data.setdefault("Custom", []).append({
            "title": f"Custom Shoe Message {len(self.shoe_data.get('Custom', [])) + 1}",
            "body": "",
        })
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab("Added blank custom shoe message.")

    def save_custom_shoe_dashboard(self) -> None:
        self.collect_custom_shoe_editors()
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab("Custom shoe dashboard saved.")

    def remove_custom_shoe_card(self, index: int) -> None:
        self.collect_custom_shoe_editors()
        custom = self.shoe_data.setdefault("Custom", [])
        if not (0 <= index < len(custom)):
            return
        title = str(custom[index].get("title", "Custom shoe message"))
        if QMessageBox.question(self, "Remove Custom Shoe Message", f"Remove '{title}' from the Custom dashboard?") != QMessageBox.Yes:
            return
        custom.pop(index)
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab(f"Removed custom shoe message: {title}")

    def clear_custom_shoe_dashboard(self) -> None:
        self.collect_custom_shoe_editors()
        custom = self.shoe_data.setdefault("Custom", [])
        if not custom:
            self.set_status("Custom shoe dashboard is already empty.")
            return
        if QMessageBox.question(self, "Clear Custom Shoe Dashboard", "Remove every card from the Custom shoe dashboard?") != QMessageBox.Yes:
            return
        self.shoe_data["Custom"] = []
        for seq, payload in list(self.hotkeys.items()):
            if payload.get("tab") == "Shoes - Custom":
                del self.hotkeys[seq]
        self.save_hotkeys()
        self.register_hotkeys()
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab("Custom shoe dashboard cleared.")

    def move_custom_shoe_card(self, index: int, direction: int) -> None:
        self.collect_custom_shoe_editors()
        custom = self.shoe_data.setdefault("Custom", [])
        new_index = index + direction
        if not (0 <= index < len(custom) and 0 <= new_index < len(custom)):
            return
        custom[index], custom[new_index] = custom[new_index], custom[index]
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab("Reordered custom shoe dashboard.")

    def refresh_auto_safe_shoes_tab(self, status: str = "", preferred_subtab: str = "Custom") -> None:
        self.shoe_preferred_subtab = preferred_subtab
        self._render_shoes_tab()
        if status:
            self.set_status(status)

    def _render_custom_shoe_tab(self, layout: QVBoxLayout, q: str) -> None:
        builder = QFrame(); builder.setObjectName("settingsCard")
        builder_layout = QGridLayout(builder)
        title = QLabel("Custom Shoe Dashboard")
        title.setStyleSheet("font-size: 18px; font-weight: 800;")
        help_text = QLabel("Build your own shoe quick-send dashboard by adding cards from the existing shoe database or creating custom messages.")
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #CFC7E8;")
        self.custom_source_combo = QComboBox()
        self.populate_custom_source_combo()
        add_selected = QPushButton("Add Selected Existing Message")
        add_selected.setObjectName("successButton")
        add_selected.clicked.connect(self.add_selected_custom_shoe_card)
        add_blank = QPushButton("Create Blank Custom Message")
        add_blank.clicked.connect(self.add_blank_custom_shoe_card)
        save_all = QPushButton("Save Custom Dashboard")
        save_all.setObjectName("ghostButton")
        save_all.clicked.connect(self.save_custom_shoe_dashboard)
        clear_all = QPushButton("Clear All")
        clear_all.setObjectName("dangerButton")
        clear_all.clicked.connect(self.clear_custom_shoe_dashboard)
        builder_layout.addWidget(title, 0, 0, 1, 4)
        builder_layout.addWidget(help_text, 1, 0, 1, 4)
        builder_layout.addWidget(self.custom_source_combo, 2, 0, 1, 4)
        builder_layout.addWidget(add_selected, 3, 0)
        builder_layout.addWidget(add_blank, 3, 1)
        builder_layout.addWidget(save_all, 3, 2)
        builder_layout.addWidget(clear_all, 3, 3)
        layout.addWidget(builder)

        raw_items = self.shoe_data.setdefault("Custom", [])
        items = [(i, d) for i, d in enumerate(raw_items) if not q or q in (d.get('title','') + ' ' + d.get('body','')).lower()]
        self.custom_shoe_editors = []

        if self.view_mode == "Edit":
            if not raw_items:
                empty = QLabel("No custom shoe messages yet. Add an existing message above or create a blank one.")
                empty.setStyleSheet("color: #CFC7E8; padding: 8px;")
                layout.addWidget(empty)
            for i, d in items:
                box = QGroupBox(f"Custom {i + 1}: {d.get('title','Message')}")
                grid = QGridLayout(box)
                title_edit = QLineEdit(d.get('title',''))
                body_edit = QTextEdit(); body_edit.setPlainText(d.get('body','')); body_edit.setFixedHeight(78)
                grid.addWidget(QLabel("Title"), 0, 0); grid.addWidget(title_edit, 0, 1, 1, 5)
                grid.addWidget(QLabel("Body"), 1, 0); grid.addWidget(body_edit, 1, 1, 1, 5)
                for col, (txt, cb) in enumerate([
                    ("Send", lambda _=False, b=body_edit: self.send_message(b.toPlainText())),
                    ("Copy", lambda _=False, b=body_edit: self.copy_to_clipboard(b.toPlainText())),
                    ("Save", lambda _=False: self.save_custom_shoe_dashboard()),
                    ("Up", lambda _=False, ix=i: self.move_custom_shoe_card(ix, -1)),
                    ("Down", lambda _=False, ix=i: self.move_custom_shoe_card(ix, 1)),
                    ("Remove", lambda _=False, ix=i: self.remove_custom_shoe_card(ix)),
                ]):
                    btn = QPushButton(txt)
                    if txt == "Remove":
                        btn.setObjectName("goldButton")
                    btn.clicked.connect(cb)
                    grid.addWidget(btn, 2, col)
                layout.addWidget(box)
                self.custom_shoe_editors.append((i, title_edit, body_edit))
        else:
            gridw = QWidget(); grid = QGridLayout(gridw); grid.setAlignment(Qt.AlignmentFlag.AlignTop); layout.addWidget(gridw)
            for n, (i, d) in enumerate(items):
                card_wrap = QFrame(); card_wrap.setObjectName("messageCard")
                card_layout = QVBoxLayout(card_wrap)
                inner = MessageCardWidget(d.get('title',''), d.get('body',''), self.send_message, self.copy_to_clipboard, lambda ix=i: self.assign_hotkey_dialog('Shoes - Custom', ix))
                remove_btn = QPushButton("Remove from Custom")
                remove_btn.setObjectName("dangerButton")
                remove_btn.clicked.connect(lambda _=False, ix=i: self.remove_custom_shoe_card(ix))
                card_layout.addWidget(inner)
                card_layout.addWidget(remove_btn)
                grid.addWidget(card_wrap, n//4, n%4)
            grid.setRowStretch((len(items)+3)//4, 1)
            if not items:
                layout.addWidget(QLabel("No matching custom shoe messages." if raw_items else "No custom shoe messages yet."))

    def add_shoe_message_card(self, key: str):
        self.collect_all()
        arr = self.shoe_data.setdefault(key, [])
        arr.append({"title": f"{key} Message {len(arr) + 1}", "body": ""})
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab(f"Added card to {self.tab_label(key)}.", preferred_subtab=key)

    def remove_shoe_message_card(self, key: str, index: int):
        self.collect_all()
        arr = self.shoe_data.setdefault(key, [])
        if not (0 <= index < len(arr)):
            return
        title = str(arr[index].get("title", f"Card {index + 1}"))
        if QMessageBox.question(self, "Remove Shoe Card", f"Remove '{title}' from {self.tab_label(key)}?") != QMessageBox.Yes:
            return
        arr.pop(index)
        for seq, payload in list(self.hotkeys.items()):
            if payload.get("tab") != f"Shoes - {key}":
                continue
            ix = int(payload.get("index", -1))
            if ix == index:
                del self.hotkeys[seq]
            elif ix > index:
                payload["index"] = ix - 1
        self.save_hotkeys()
        self.register_hotkeys()
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab(f"Removed card from {self.tab_label(key)}.", preferred_subtab=key)

    def clear_shoe_message_tab(self, key: str):
        self.collect_all()
        arr = self.shoe_data.setdefault(key, [])
        if not arr:
            self.set_status(f"{self.tab_label(key)} is already empty.")
            return
        if QMessageBox.question(self, "Clear Shoe Cards", f"Remove every card from {self.tab_label(key)}?") != QMessageBox.Yes:
            return
        self.shoe_data[key] = []
        for seq, payload in list(self.hotkeys.items()):
            if payload.get("tab") == f"Shoes - {key}":
                del self.hotkeys[seq]
        self.save_hotkeys()
        self.register_hotkeys()
        ShoeConfigStore.save(self.shoe_data)
        self.refresh_auto_safe_shoes_tab(f"Cleared {self.tab_label(key)}.", preferred_subtab=key)

    def _render_shoes_tab(self):
        self.collect_all()
        w = QWidget(); v = QVBoxLayout(w)
        header = QLabel("Shoes Command Center · quick-send sizes, conversions, statuses, and notes")
        header.setStyleSheet("font-weight:700; font-size:18px;"); v.addWidget(header)
        sub = QTabWidget(); v.addWidget(sub, 1)
        q = self.search.text().strip().lower()
        shoe_tabs = ["All Shoes", "Men Sizes", "Women Sizes", "Youth Sizes", "M/W Conversion", "Status Buttons", "Custom"]
        for key in shoe_tabs:
            scroll, cont, layout = self._scroll_widget()
            if key == "Custom":
                self._render_custom_shoe_tab(layout, q)
                sub.addTab(scroll, self.tab_label(key))
                continue
            raw_items = self.shoe_tab_items(key)
            items = [(i, d) for i, d in enumerate(raw_items) if not q or q in (d.get('title','') + ' ' + d.get('body','')).lower()]
            if self.view_mode == "Edit" and key not in {"All Shoes", "Youth Sizes"}:
                toolbar = QFrame(); toolbar.setObjectName("settingsCard")
                tool_layout = QGridLayout(toolbar)
                tool_title = QLabel(f"{self.tab_label(key)} Editor")
                tool_title.setObjectName("sectionTitle")
                tool_hint = QLabel("Edit this saved shoe database, add new cards, or remove cards you do not use.")
                tool_hint.setObjectName("sectionHint")
                tool_hint.setWordWrap(True)
                add_btn = QPushButton("+ Add Card")
                add_btn.setObjectName("successButton")
                add_btn.clicked.connect(lambda _=False, k=key: self.add_shoe_message_card(k))
                clear_btn = QPushButton("Clear All")
                clear_btn.setObjectName("dangerButton")
                clear_btn.clicked.connect(lambda _=False, k=key: self.clear_shoe_message_tab(k))
                tool_layout.addWidget(tool_title, 0, 0)
                tool_layout.addWidget(QLabel(f"{len(raw_items)} card(s)"), 0, 1)
                tool_layout.addWidget(add_btn, 0, 2)
                tool_layout.addWidget(clear_btn, 0, 3)
                tool_layout.addWidget(tool_hint, 1, 0, 1, 4)
                layout.addWidget(toolbar)
                if not raw_items:
                    empty = QLabel("This shoe section is empty. Click + Add Card to create one.")
                    empty.setStyleSheet("color: #CFC7E8; padding: 14px;")
                    layout.addWidget(empty)
                for i, d in items:
                    box = QGroupBox(f"{d.get('title','Message')}"); grid = QGridLayout(box)
                    title = QLineEdit(d.get('title','')); body = QTextEdit(); body.setPlainText(d.get('body','')); body.setFixedHeight(72)
                    grid.addWidget(QLabel("Title"),0,0); grid.addWidget(title,0,1,1,5); grid.addWidget(QLabel("Body"),1,0); grid.addWidget(body,1,1,1,5)
                    def save_item(_=False, k=key, ix=i, te=title, be=body):
                        self.shoe_data[k][ix] = {"title": te.text().strip() or f"Message {ix+1}", "body": be.toPlainText().strip()}; ShoeConfigStore.save(self.shoe_data); self.set_status("Saved shoe message.")
                    actions = [
                        ("Send", lambda _=False,b=body: self.send_message(b.toPlainText()), ""),
                        ("Copy", lambda _=False,b=body: self.copy_to_clipboard(b.toPlainText()), "ghostButton"),
                        ("Save", save_item, ""),
                        ("Assign Key", lambda _=False,k=key,ix=i: self.assign_hotkey_dialog('Shoes - '+k, ix), "ghostButton"),
                        ("Remove", lambda _=False,k=key,ix=i: self.remove_shoe_message_card(k, ix), "dangerButton"),
                    ]
                    for col, (txt, cb, obj) in enumerate(actions):
                        btn=QPushButton(txt)
                        if obj:
                            btn.setObjectName(obj)
                        btn.clicked.connect(cb); grid.addWidget(btn,2,col)
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
        preferred = getattr(self, "shoe_preferred_subtab", "")
        if preferred:
            for i in range(sub.count()):
                if sub.tabText(i) == self.tab_label(preferred):
                    sub.setCurrentIndex(i)
                    break
            self.shoe_preferred_subtab = ""
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
            "Shoes - M/W Conversion", "Shoes - Status Buttons", "Shoes - Custom",
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
        self.collect_custom_shoe_editors()
        for k,e in self.note_editors.items(): self.notes_data[k] = e.toPlainText()

    def save_app_settings(self):
        self.app_settings["enter_to_send"] = bool(self.enter_to_send)
        self.app_settings["announce_mode"] = bool(self.announce_mode)
        self.app_settings["custom_dash_announce"] = bool(getattr(self, "custom_dash_announce", False))
        AppSettingsStore.save(self.app_settings)

    def set_enter_to_send(self, checked: bool):
        self.enter_to_send = bool(checked)
        self.save_app_settings()
        self.set_status("Enter-to-send enabled." if self.enter_to_send else "Enter-to-send disabled. Send will copy instead.")

    def set_announce_mode(self, checked: bool):
        self.announce_mode = bool(checked)
        self.save_app_settings()
        self.set_status("/Announce enabled for all outgoing messages." if self.announce_mode else "/Announce disabled.")

    def _serialize_state_for_snapshot(self) -> Dict[str, object]:
        return {
            "view_mode": self.view_mode,
            "tabs": {tab: [asdict(slot) for slot in self.tabs_data.get(tab, [])] for tab in MESSAGE_TABS},
            "notes": {tab: self.notes_data.get(tab, "") for tab in list(NOTE_TABS) + ["Shoe Notes"]},
            "shoes": self.shoe_data,
            "auto_messages": [asdict(msg) for msg in self.auto_messages],
            "app_settings": {
                "enter_to_send": bool(self.enter_to_send),
                "announce_mode": bool(self.announce_mode),
            },
        }

    def _make_state_snapshot(self) -> str:
        try:
            return json.dumps(self._serialize_state_for_snapshot(), sort_keys=True, ensure_ascii=False)
        except Exception:
            return str(time.time())

    def has_unsaved_changes(self) -> bool:
        try:
            self.collect_all()
            self.collect_auto_editors()
            return self._make_state_snapshot() != getattr(self, "_last_saved_snapshot", "")
        except Exception:
            return True

    def save_all(self, silent: bool=False):
        self.collect_all()
        self.collect_auto_editors()
        ConfigStore.save(self.tabs_data, self.notes_data, self.view_mode)
        ShoeConfigStore.save(self.shoe_data)
        AutoMessageStore.save(self.auto_messages)
        self.save_app_settings()
        self._last_saved_snapshot = self._make_state_snapshot()
        if not silent:
            self.set_status("Saved all WhatMod data.")

    def periodic_auto_save(self):
        if getattr(self, "_is_closing", False):
            return
        if self.has_unsaved_changes():
            self.save_all(silent=True)
            self.set_status("Auto-saved all WhatMod data.")

    def closeEvent(self, event):
        self._is_closing = True
        if not self.has_unsaved_changes():
            try:
                self.browser.close()
            except Exception:
                pass
            event.accept()
            return

        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Changes")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("You have unsaved changes in WhatMod.")
        box.setInformativeText("Save before exiting, exit without saving, or cancel and return to WhatMod?")
        save_exit = box.addButton("Save and Exit", QMessageBox.ButtonRole.AcceptRole)
        exit_no_save = box.addButton("Exit Without Saving", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_exit)
        box.exec()
        clicked = box.clickedButton()

        if clicked == save_exit:
            try:
                self.save_all(silent=True)
                self.browser.close()
            except Exception as exc:
                QMessageBox.critical(self, "Save Error", f"Could not save before exit.\n\n{exc}")
                self._is_closing = False
                event.ignore()
                return
            event.accept()
        elif clicked == exit_no_save:
            try:
                self.browser.close()
            except Exception:
                pass
            event.accept()
        else:
            self._is_closing = False
            event.ignore()

    def duplicate_message(self, tab: str, index: int):
        self.collect_tab(tab)
        arr=self.tabs_data.setdefault(tab, [])
        if 0 <= index < len(arr):
            source = arr[index]
            arr.insert(index + 1, MessageSlot((source.title or f"Message {index + 1}") + " Copy", source.body))
            self.refresh_current_tab()
            self.set_status("Duplicated message card.")

    def format_outgoing_message(self, message: str) -> str:
        msg = (message or "").strip()
        return add_announce_prefix(msg) if self.announce_mode else msg

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
        try:
            self.browser.launch()
            self.update_browser_status_display()
        except Exception as e:
            try:
                self.browser.close()
            except Exception:
                pass
            self.update_browser_status_display()
            QMessageBox.critical(self, "Browser Error", str(e))

    def show_license_dialog(self):
        current=LicenseManager.load(); key, ok = QInputDialog.getMultiLineText(self, "Activate License", "Paste license key:", str(current.get('key','')))
        if ok and key.strip():
            try:
                payload=LicenseManager.decode_license_key(key); LicenseManager.save(key, owner_from_license_payload(payload), payload); self.license_info = LicenseManager.load(); self.apply_license_gate(); self.set_status("License activated."); QMessageBox.information(self,"License","License activated.")
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

    def set_status(self, text: str):
        self.status.setText(text)
        if "browser" in str(text).lower() or "chrome" in str(text).lower():
            QTimer.singleShot(100, self.update_browser_status_display)

    def has_valid_license(self) -> bool:
        info = LicenseManager.load()
        key = str(info.get("key", "") or "").strip()
        if not key:
            return False
        try:
            LicenseManager.decode_license_key(key)
            return True
        except Exception:
            return False

    def apply_license_gate(self) -> None:
        if self.has_valid_license():
            if self.license_overlay is not None:
                self.license_overlay.hide()
                self.license_overlay.deleteLater()
                self.license_overlay = None
            return
        if self.license_overlay is None:
            self.license_overlay = self._build_license_overlay()
        self.license_overlay.setGeometry(self.rect())
        self.license_overlay.raise_()
        self.license_overlay.show()

    def _build_license_overlay(self) -> QWidget:
        overlay = QWidget(self)
        overlay.setObjectName("licenseOverlay")
        overlay.setStyleSheet("""
            QWidget#licenseOverlay { background: rgba(9, 11, 15, 244); }
            QFrame#licenseCard { background: #151820; border: 1px solid #303848; border-radius: 22px; }
            QLabel#licenseTitle { color: #FFFFFF; font-size: 28px; font-weight: 900; }
            QLabel#licenseHint { color: #AAB3C2; font-size: 14px; }
            QLineEdit#licenseInput { background: #0F1115; border: 1px solid #303848; border-radius: 12px; padding: 10px; color: #FFFFFF; }
        """)
        outer = QVBoxLayout(overlay)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)
        card = QFrame()
        card.setObjectName("licenseCard")
        card.setMaximumWidth(720)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(14)

        cover_path = find_bundled_asset("cover.png") or find_bundled_asset("assets/cover.png")
        if cover_path:
            cover = QLabel()
            pix = QPixmap(str(cover_path))
            if not pix.isNull():
                cover.setPixmap(pix.scaled(620, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(cover)

        title = QLabel("Activate WhatMod")
        title.setObjectName("licenseTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("Enter a valid WhatMod license key to unlock the app. This screen blocks the workspace until activation succeeds.")
        hint.setObjectName("licenseHint")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        key_input = QLineEdit()
        key_input.setObjectName("licenseInput")
        key_input.setPlaceholderText("Paste license key here")
        key_input.setEchoMode(QLineEdit.EchoMode.Normal)
        layout.addWidget(key_input)

        row = QHBoxLayout()
        activate = QPushButton("Activate")
        activate.setObjectName("primaryButton")
        buy = QPushButton("Purchase License")
        buy.setObjectName("softButton")
        row.addWidget(activate)
        row.addWidget(buy)
        layout.addLayout(row)

        message = QLabel("")
        message.setObjectName("licenseHint")
        message.setWordWrap(True)
        layout.addWidget(message)

        def do_activate():
            key = key_input.text().strip()
            if not key:
                message.setText("Paste your license key first.")
                return
            try:
                payload = LicenseManager.decode_license_key(key)
                LicenseManager.save(key, owner_from_license_payload(payload), payload)
                self.license_info = LicenseManager.load()
                message.setText("License activated.")
                self.apply_license_gate()
                self.set_status("License activated.")
            except Exception as exc:
                message.setText(str(exc))

        activate.clicked.connect(do_activate)
        key_input.returnPressed.connect(do_activate)
        buy.clicked.connect(lambda: webbrowser.open(PURCHASE_LICENSE_URL))

        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        outer.addStretch(1)
        return overlay

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.license_overlay is not None:
            self.license_overlay.setGeometry(self.rect())
            self.license_overlay.raise_()


class ModernStartupSplash(QWidget):
    """Compact modern startup splash with progress feedback.

    Uses Qt widgets instead of a raw image-only QSplashScreen so large bundled
    splash images cannot create an oversized startup window.
    """

    def __init__(self, app_name: str, version: str, logo_path: Optional[Path] = None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("modernSplash")
        self.setFixedSize(640, 380)
        self._steps = [
            "Loading message banks...",
            "Loading shoe cards...",
            "Loading auto dashboard...",
            "Loading hotkeys...",
            "Checking local license...",
            "Preparing WhatMod...",
        ]
        self._index = 0

        self.setStyleSheet("""
            QWidget#modernSplash {
                background: #0F1115;
                border: 1px solid #252B36;
                border-radius: 20px;
            }
            QLabel#splashTitle {
                color: #FFFFFF;
                font-size: 32px;
                font-weight: 900;
            }
            QLabel#splashSubtitle {
                color: #9CA3AF;
                font-size: 14px;
            }
            QLabel#splashStep {
                color: #FFFFFF;
                font-size: 14px;
                font-weight: 700;
            }
            QLabel#splashVersion {
                color: #737C8C;
                font-size: 12px;
            }
            QProgressBar {
                background: #171A21;
                border: 1px solid #2B303B;
                border-radius: 9px;
                height: 18px;
                text-align: center;
                color: #FFFFFF;
                font-weight: 700;
            }
            QProgressBar::chunk {
                background: #4057D6;
                border-radius: 8px;
            }
            QFrame#splashCard {
                background: #151820;
                border: 1px solid #252B36;
                border-radius: 20px;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        card = QFrame()
        card.setObjectName("splashCard")
        outer.addWidget(card, 1)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(38, 34, 38, 30)
        layout.setSpacing(16)

        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if logo_path:
            pix = QPixmap(str(logo_path))
            if not pix.isNull():
                logo_label.setPixmap(pix.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        if logo_label.pixmap() is None:
            logo_label.setText("WM")
            logo_label.setStyleSheet("font-size: 42px; font-weight: 900; color: #FFFFFF; background:#4057D6; border-radius: 24px; padding: 24px;")
        layout.addWidget(logo_label, 0, Qt.AlignmentFlag.AlignCenter)

        title = QLabel(app_name)
        title.setObjectName("splashTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        layout.addWidget(title)

        subtitle = QLabel("Moderator command center is starting up")
        subtitle.setObjectName("splashSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        layout.addStretch(1)

        self.step_label = QLabel(self._steps[0])
        self.step_label.setObjectName("splashStep")
        layout.addWidget(self.step_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(8)
        layout.addWidget(self.progress)

        self.version_label = QLabel(f"Version {version}")
        self.version_label.setObjectName("splashVersion")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.version_label)

    def center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        self.move(geo.center() - self.rect().center())

    def advance(self) -> None:
        self._index = min(self._index + 1, len(self._steps) - 1)
        self.step_label.setText(self._steps[self._index])
        value = int(((self._index + 1) / len(self._steps)) * 100)
        self.progress.setValue(min(100, value))

    def finish_progress(self) -> None:
        self.step_label.setText("Ready.")
        self.progress.setValue(100)


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app_icon = get_app_icon_path()
    if app_icon:
        app.setWindowIcon(QIcon(str(app_icon)))
    app.setStyleSheet(WHATMOD_QSS)

    logo_path = find_bundled_asset("splash.png") or find_bundled_asset("splash(2).png") or find_bundled_asset("assets/icon.ico") or find_bundled_asset("icon.ico") or find_bundled_asset("icon.png")
    splash = ModernStartupSplash(APP_NAME, APP_VERSION, logo_path)
    splash.center_on_screen()
    splash.show()
    app.processEvents()

    splash_timer = QTimer()
    splash_timer.setInterval(260)
    splash_timer.timeout.connect(splash.advance)
    splash_timer.start()

    win = WhatModQtApp()
    splash.finish_progress()
    app.processEvents()

    def show_main():
        splash_timer.stop()
        splash.close()
        win.show()
        win.raise_()
        win.activateWindow()

    QTimer.singleShot(900, show_main)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
