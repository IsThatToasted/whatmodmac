"""
WhatMod License Admin + Update Manager

Purpose:
- Generate signed WhatMod license keys
- Track customers, expirations, status, seats, notes
- Revoke / suspend / reactivate licenses locally
- Build a release manifest for distributing updates later
- Export/import license records for backups

Install:
    pip install customtkinter pyperclip

Run:
    python whatmod_license_admin.py

Important:
- This is a local admin console. For real production enforcement, host the exported
  license/update JSON on a small server/API so client apps can check status online.
- Keep your secret key private. Anyone with the secret can generate valid keys.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import traceback
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


try:
    import pyperclip
except Exception:
    pyperclip = None

APP_NAME = "WhatMod License Admin"
APP_DIR = Path.home() / ".whatmod_admin"
DB_FILE = APP_DIR / "licenses.db"
SECRET_FILE = APP_DIR / "admin_secret.key"
CLIENT_SECRET_FILE = Path.home() / ".whatmod" / "license_secret.key"
LOCAL_CLIENT_SECRET_FILE = Path(__file__).resolve().parent / "whatmod_license_secret.key"
EXPORT_DIR = APP_DIR / "exports"
RELEASE_DIR = APP_DIR / "releases"
PRODUCT_ID = "whatmod"
CURRENT_SCHEMA = 1
FREE_HOSTING_HELP_URL = "https://pages.github.com/"
DEFAULT_LICENSE_STATUS_URL = "https://raw.githubusercontent.com/IsThatToasted/whatmod/main/license_status.json"
GITHUB_BLOB_STATUS_URL = "https://github.com/IsThatToasted/whatmod/blob/main/license_status.json"
DEFAULT_UPDATE_HOST_HINT = "Direct release asset URL. Default: GitHub release tag update / WhatMod.exe"
DEFAULT_UPDATE_DOWNLOAD_URL = "https://github.com/IsThatToasted/whatmod/releases/download/update/WhatMod.exe"
DEFAULT_MANIFEST_UPLOAD_URL = "https://github.com/IsThatToasted/whatmod/blob/main/updates/download/manifest.json"


def manifest_download_url(base_download_url: str, filename: str = "WhatMod.exe") -> str:
    """Build the manifest download_url. Supports folder URLs or direct EXE URLs."""
    base = (base_download_url or "").strip()
    if not base:
        return DEFAULT_UPDATE_DOWNLOAD_URL
    lowered = base.lower()
    # Direct file URLs: Dropbox ?dl=1/raw=1, R2 public object URLs, signed URLs, etc.
    if lowered.endswith(".exe") or ".exe?" in lowered or "dl=1" in lowered or "raw=1" in lowered:
        return base
    return base.rstrip("/") + "/" + filename

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"
LICENSE_STATUSES = [STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REVOKED]
PLANS = ["trial", "monthly", "yearly", "lifetime", "beta", "custom"]


def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "Never"
    return time.strftime("%Y-%m-%d %I:%M %p", time.localtime(int(ts)))


def parse_date_to_ts(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    for pattern in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]:
        try:
            return int(time.mktime(time.strptime(value, pattern)))
        except ValueError:
            pass
    raise ValueError("Date must be YYYY-MM-DD, MM/DD/YYYY, or blank for no expiration.")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("utf-8"))


def sync_client_secret(secret: bytes) -> None:
    """Write the local verifier secret where the main app can find it.

    This makes local generated keys activate locally without hosting anything.
    For public release, move activation to a server or asymmetric verifier so
    the signing secret is not shipped to client machines.
    """
    encoded = b64url(secret)
    for path in (CLIENT_SECRET_FILE, LOCAL_CLIENT_SECRET_FILE):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception:
            pass


def load_or_create_secret() -> bytes:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        raw = SECRET_FILE.read_text(encoding="utf-8").strip()
        secret = b64url_decode(raw)
        sync_client_secret(secret)
        return secret
    secret = secrets.token_bytes(32)
    SECRET_FILE.write_text(b64url(secret), encoding="utf-8")
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    sync_client_secret(secret)
    return secret


def public_key_fingerprint(secret: bytes) -> str:
    return hashlib.sha256(secret).hexdigest()[:16].upper()


def make_license_key(payload: Dict[str, Any], secret: bytes) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).digest()[:18]
    token = f"{b64url(body)}.{b64url(sig)}"
    grouped = "-".join([token[i : i + 5] for i in range(0, len(token), 5)])
    return f"WMOD-{grouped}"


def decode_license_key(key: str, secret: bytes) -> Dict[str, Any]:
    compact = re.sub(r"\s+", "", key.strip()) if "re" in globals() else "".join(key.strip().split())
    raw = compact.replace("WMOD-", "", 1).replace("wmod-", "", 1).replace("-", "")
    if "." not in raw:
        raise ValueError("Invalid license format.")
    body64, sig64 = raw.split(".", 1)
    body = b64url_decode(body64)
    expected = hmac.new(secret, body, hashlib.sha256).digest()[:18]
    actual = b64url_decode(sig64)
    if not hmac.compare_digest(expected, actual):
        raise ValueError("License signature failed.")
    return json.loads(body.decode("utf-8"))


@dataclass
class LicenseRecord:
    license_id: str
    customer_name: str
    customer_email: str
    plan: str
    status: str
    seats: int
    expires_at: Optional[int]
    license_key: str
    notes: str
    created_at: int
    updated_at: int
    last_seen_at: Optional[int] = None
    device_limit: int = 1


class LicenseDB:
    def __init__(self, path: Path):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS licenses (
                license_id TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                seats INTEGER NOT NULL DEFAULT 1,
                device_limit INTEGER NOT NULL DEFAULT 1,
                expires_at INTEGER,
                license_key TEXT NOT NULL UNIQUE,
                notes TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_seen_at INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS releases (
                version TEXT PRIMARY KEY,
                channel TEXT NOT NULL DEFAULT 'stable',
                file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                release_notes TEXT NOT NULL DEFAULT '',
                required INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(CURRENT_SCHEMA),))
        self.conn.commit()

    def add_license(self, rec: LicenseRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO licenses (
                license_id, customer_name, customer_email, plan, status, seats, device_limit,
                expires_at, license_key, notes, created_at, updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.license_id,
                rec.customer_name,
                rec.customer_email,
                rec.plan,
                rec.status,
                rec.seats,
                rec.device_limit,
                rec.expires_at,
                rec.license_key,
                rec.notes,
                rec.created_at,
                rec.updated_at,
                rec.last_seen_at,
            ),
        )
        self.conn.commit()

    def update_license(self, license_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_ts()
        columns = ", ".join([f"{k} = ?" for k in fields.keys()])
        self.conn.execute(f"UPDATE licenses SET {columns} WHERE license_id = ?", list(fields.values()) + [license_id])
        self.conn.commit()

    def list_licenses(self, query: str = "", status: str = "All") -> List[LicenseRecord]:
        where = []
        params: List[Any] = []
        if query.strip():
            q = f"%{query.strip()}%"
            where.append("(customer_name LIKE ? OR customer_email LIKE ? OR license_id LIKE ? OR license_key LIKE ?)")
            params += [q, q, q, q]
        if status != "All":
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM licenses"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [LicenseRecord(**dict(row)) for row in rows]

    def get_license(self, license_id: str) -> Optional[LicenseRecord]:
        row = self.conn.execute("SELECT * FROM licenses WHERE license_id = ?", (license_id,)).fetchone()
        return LicenseRecord(**dict(row)) if row else None

    def export_public_licenses(self, path: Path) -> None:
        """Export a public-safe status file for GitHub/static hosting.

        IMPORTANT: this file intentionally contains no customer names, emails,
        notes, full license keys, or active customer list. It only publishes
        license IDs that should be blocked by the client app. Customer PII stays
        inside the local admin database and CSV backup only.
        """
        rows = self.conn.execute(
            "SELECT license_id, status, expires_at, updated_at FROM licenses WHERE status != ? ORDER BY updated_at DESC",
            (STATUS_ACTIVE,),
        ).fetchall()
        blocked = [dict(row) for row in rows]
        revoked_ids = [r["license_id"] for r in blocked if r.get("status") == STATUS_REVOKED]
        suspended_ids = [r["license_id"] for r in blocked if r.get("status") == STATUS_SUSPENDED]
        disabled_ids = [r["license_id"] for r in blocked if r.get("status") not in {STATUS_REVOKED, STATUS_SUSPENDED, STATUS_ACTIVE}]
        data = {
            "product": PRODUCT_ID,
            "generated_at": now_ts(),
            "schema": 4,
            "privacy": "public-safe: no names, no emails, no full license keys, no active customer list",
            "blocked_count": len(blocked),
            "revoked_ids": revoked_ids,
            "suspended_ids": suspended_ids,
            "disabled_ids": disabled_ids,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def export_csv(self, path: Path) -> None:
        rows = self.conn.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else ["license_id"])
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

    def add_release(self, version: str, channel: str, file_path: str, release_notes: str, required: bool) -> None:
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            raise ValueError("Update file does not exist.")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        size = p.stat().st_size
        self.conn.execute(
            """
            INSERT OR REPLACE INTO releases(version, channel, file_path, sha256, size_bytes, release_notes, required, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (version.strip(), channel.strip(), str(p), digest, size, release_notes, 1 if required else 0, now_ts()),
        )
        self.conn.commit()

    def list_releases(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM releases ORDER BY created_at DESC").fetchall()

    def export_manifest(self, path: Path, base_download_url: str = "") -> None:
        rows = [dict(r) for r in self.list_releases()]
        latest_by_channel: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            channel = r["channel"]
            if channel not in latest_by_channel:
                filename = "WhatMod.exe"
                latest_by_channel[channel] = {
                    "version": r["version"],
                    "channel": channel,
                    "filename": filename,
                    "download_url": manifest_download_url(base_download_url, filename),
                    "sha256": r["sha256"],
                    "size_bytes": r["size_bytes"],
                    "required": bool(r["required"]),
                    "release_notes": r["release_notes"],
                    "created_at": r["created_at"],
                }
        payload = {
            "product": PRODUCT_ID,
            "generated_at": now_ts(),
            "channels": latest_by_channel,
            "releases": [
                {
                    "version": r["version"],
                    "channel": r["channel"],
                    "filename": "WhatMod.exe",
                    "sha256": r["sha256"],
                    "size_bytes": r["size_bytes"],
                    "required": bool(r["required"]),
                    "release_notes": r["release_notes"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")



# ------------------------- PySide6 UI Port -------------------------
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
        QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
        QPushButton, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget, QCheckBox
    )
except ImportError as exc:
    raise SystemExit("PySide6 is required. Install with: pip install PySide6") from exc


def copy_text_qt(text: str) -> None:
    if pyperclip:
        try:
            pyperclip.copy(text); return
        except Exception:
            pass
    QApplication.clipboard().setText(text)


class AdminQtApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME + " (PySide6)")
        self.resize(1180, 780)
        self.secret = load_or_create_secret()
        self.db = LicenseDB(DB_FILE)
        self.selected_license_id: Optional[str] = None
        self._build_ui()
        self.refresh_all()

    def _build_ui(self):
        root = QWidget(); outer=QVBoxLayout(root); self.setCentralWidget(root)
        header=QLabel(f"{APP_NAME} · Secret fingerprint {public_key_fingerprint(self.secret)}")
        header.setStyleSheet("font-weight:700; font-size:20px;"); outer.addWidget(header)
        self.tabs=QTabWidget(); outer.addWidget(self.tabs,1)
        self.status=QLabel("Ready."); outer.addWidget(self.status)
        self.tabs.addTab(self._dashboard_tab(), "Dashboard")
        self.tabs.addTab(self._generate_tab(), "Generate License")
        self.tabs.addTab(self._manage_tab(), "Manage Licenses")
        self.tabs.addTab(self._updates_tab(), "Updates")
        self.tabs.addTab(self._exports_tab(), "Exports")
        self.tabs.addTab(self._integration_tab(), "Client Integration")

    def _dashboard_tab(self):
        w=QWidget(); v=QVBoxLayout(w)
        self.metrics={}
        row=QHBoxLayout(); v.addLayout(row)
        for k in ["Total","Active","Suspended","Revoked"]:
            box=QGroupBox(k); bx=QVBoxLayout(box); lab=QLabel("0"); lab.setStyleSheet("font-size:34px;font-weight:700;"); lab.setAlignment(Qt.AlignCenter); bx.addWidget(lab); row.addWidget(box); self.metrics[k]=lab
        info=QTextEdit(); info.setReadOnly(True); info.setPlainText("Generate signed WhatMod licenses, manage status, export public status JSON, and build update manifests. For production, host exported JSON on a server/static host and keep the admin secret private."); v.addWidget(info,1)
        return w

    def _generate_tab(self):
        w=QWidget(); h=QHBoxLayout(w)
        formbox=QGroupBox("Customer / License"); form=QFormLayout(formbox); h.addWidget(formbox,1)
        self.gen_name=QLineEdit(); self.gen_email=QLineEdit(); self.gen_seats=QSpinBox(); self.gen_seats.setRange(1,999); self.gen_device_limit=QSpinBox(); self.gen_device_limit.setRange(1,999)
        self.gen_exp=QLineEdit(); self.gen_exp.setPlaceholderText("YYYY-MM-DD or blank")
        self.gen_plan=QComboBox(); self.gen_plan.addItems(PLANS); self.gen_plan.setCurrentText("monthly")
        self.gen_notes=QTextEdit()
        for label, widget in [("Customer Name",self.gen_name),("Customer Email",self.gen_email),("Seats",self.gen_seats),("Device Limit",self.gen_device_limit),("Expiration",self.gen_exp),("Plan",self.gen_plan),("Notes",self.gen_notes)]: form.addRow(label, widget)
        b=QPushButton("Generate License"); b.clicked.connect(self.generate_license); form.addRow(b)
        resbox=QGroupBox("Generated License Key"); rv=QVBoxLayout(resbox); h.addWidget(resbox,1)
        self.generated_key=QTextEdit(); rv.addWidget(self.generated_key,1)
        copy=QPushButton("Copy Generated Key"); copy.clicked.connect(lambda: copy_text_qt(self.generated_key.toPlainText().strip())); rv.addWidget(copy)
        return w

    def _manage_tab(self):
        w=QWidget(); v=QVBoxLayout(w)
        tools=QHBoxLayout(); self.search=QLineEdit(); self.search.setPlaceholderText("Search customer, email, license ID, or key"); tools.addWidget(self.search,1)
        self.status_filter=QComboBox(); self.status_filter.addItems(["All"]+LICENSE_STATUSES); tools.addWidget(self.status_filter)
        b=QPushButton("Search"); b.clicked.connect(self.refresh_license_list); tools.addWidget(b); v.addLayout(tools)
        body=QHBoxLayout(); v.addLayout(body,1)
        self.license_list=QListWidget(); self.license_list.currentRowChanged.connect(self.select_current_license); body.addWidget(self.license_list,1)
        detail=QGroupBox("Details"); form=QFormLayout(detail); body.addWidget(detail,1)
        self.detail_labels={}
        for k in ["License ID","Customer","Email","Plan","Status","Seats","Device Limit","Expires","Created","Updated","Last Seen"]:
            lab=QLabel("—"); lab.setWordWrap(True); form.addRow(k,lab); self.detail_labels[k]=lab
        self.detail_notes=QTextEdit(); form.addRow("Notes", self.detail_notes)
        row=QHBoxLayout()
        for txt, fn in [("Save Notes",self.save_detail_notes),("Suspend",lambda:self.update_selected_status(STATUS_SUSPENDED)),("Reactivate",lambda:self.update_selected_status(STATUS_ACTIVE)),("Revoke",lambda:self.update_selected_status(STATUS_REVOKED)),("Copy Key",self.copy_selected_key)]:
            btn=QPushButton(txt); btn.clicked.connect(fn); row.addWidget(btn)
        form.addRow(row)
        return w

    def _updates_tab(self):
        w=QWidget(); form=QFormLayout(w)
        self.rel_version=QLineEdit(); self.rel_channel=QComboBox(); self.rel_channel.addItems(["stable","beta","dev"]); self.rel_file=QLineEdit(); self.rel_notes=QTextEdit(); self.rel_required=QCheckBox("Required update")
        browse=QPushButton("Browse..."); browse.clicked.connect(self.browse_release_file)
        file_row=QHBoxLayout(); file_row.addWidget(self.rel_file,1); file_row.addWidget(browse)
        form.addRow("Version",self.rel_version); form.addRow("Channel",self.rel_channel); form.addRow("File",file_row); form.addRow("Release Notes",self.rel_notes); form.addRow(self.rel_required)
        add=QPushButton("Add / Replace Release"); add.clicked.connect(self.add_release); form.addRow(add)
        self.release_list=QTextEdit(); self.release_list.setReadOnly(True); form.addRow("Releases",self.release_list)
        return w

    def _exports_tab(self):
        w=QWidget(); v=QVBoxLayout(w)
        for txt, fn in [("Export Public License Status JSON", self.export_public_status), ("Export Private CSV Backup", self.export_csv), ("Export Update Manifest", self.export_manifest)]:
            b=QPushButton(txt); b.clicked.connect(fn); v.addWidget(b)
        self.export_log=QTextEdit(); self.export_log.setReadOnly(True); v.addWidget(self.export_log,1)
        return w

    def _integration_tab(self):
        w=QWidget(); v=QVBoxLayout(w)
        txt=QTextEdit(); txt.setReadOnly(True); txt.setPlainText(f"Client secret locations:\n{CLIENT_SECRET_FILE}\n{LOCAL_CLIENT_SECRET_FILE}\n\nPublic status default:\n{DEFAULT_LICENSE_STATUS_URL}\n\nDefault update manifest upload URL:\n{DEFAULT_MANIFEST_UPLOAD_URL}\n\nOpen this admin once before local testing so the verifier secret syncs into the client locations.")
        v.addWidget(txt,1)
        btn=QPushButton("Open GitHub Pages Help"); btn.clicked.connect(lambda: webbrowser.open(FREE_HOSTING_HELP_URL)); v.addWidget(btn)
        return w

    def refresh_all(self):
        self.refresh_dashboard(); self.refresh_license_list(); self.refresh_releases()

    def refresh_dashboard(self):
        rows=self.db.list_licenses(); counts={"Total":len(rows),"Active":0,"Suspended":0,"Revoked":0}
        for r in rows:
            if r.status==STATUS_ACTIVE: counts["Active"]+=1
            if r.status==STATUS_SUSPENDED: counts["Suspended"]+=1
            if r.status==STATUS_REVOKED: counts["Revoked"]+=1
        for k,v in counts.items(): self.metrics[k].setText(str(v))

    def generate_license(self):
        try:
            name=self.gen_name.text().strip(); email=self.gen_email.text().strip()
            if not name or not email: raise ValueError("Customer name and email are required.")
            lid="LIC-"+secrets.token_hex(6).upper(); now=now_ts(); exp=parse_date_to_ts(self.gen_exp.text())
            payload={"pid":PRODUCT_ID,"lid":lid,"owner":name,"email":email,"plan":self.gen_plan.currentText(),"seats":self.gen_seats.value(),"device_limit":self.gen_device_limit.value(),"exp":exp,"iat":now}
            key=make_license_key(payload,self.secret)
            rec=LicenseRecord(lid,name,email,self.gen_plan.currentText(),STATUS_ACTIVE,self.gen_seats.value(),exp,key,self.gen_notes.toPlainText().strip(),now,now,None,self.gen_device_limit.value())
            self.db.add_license(rec); self.generated_key.setPlainText(key); copy_text_qt(key); self.set_status("License generated and copied."); self.refresh_all()
        except Exception as e: QMessageBox.critical(self,"Generate License",str(e))

    def refresh_license_list(self):
        self.records=self.db.list_licenses(self.search.text() if hasattr(self,'search') else "", self.status_filter.currentText() if hasattr(self,'status_filter') else "All")
        self.license_list.clear()
        for r in self.records: self.license_list.addItem(f"{r.customer_name} <{r.customer_email}> · {r.status} · {r.license_id}")
        self.refresh_dashboard()

    def select_current_license(self, row:int):
        if row<0 or row>=len(getattr(self,'records',[])): return
        r=self.records[row]; self.selected_license_id=r.license_id
        vals={"License ID":r.license_id,"Customer":r.customer_name,"Email":r.customer_email,"Plan":r.plan,"Status":r.status,"Seats":str(r.seats),"Device Limit":str(r.device_limit),"Expires":fmt_ts(r.expires_at),"Created":fmt_ts(r.created_at),"Updated":fmt_ts(r.updated_at),"Last Seen":fmt_ts(r.last_seen_at)}
        for k,v in vals.items(): self.detail_labels[k].setText(v)
        self.detail_notes.setPlainText(r.notes)

    def _selected(self):
        if not self.selected_license_id: raise ValueError("Select a license first.")
        rec=self.db.get_license(self.selected_license_id)
        if not rec: raise ValueError("Selected license not found.")
        return rec

    def save_detail_notes(self):
        try:
            r=self._selected(); self.db.update_license(r.license_id, notes=self.detail_notes.toPlainText()); self.set_status("Notes saved."); self.refresh_license_list()
        except Exception as e: QMessageBox.warning(self,"Notes",str(e))

    def update_selected_status(self,status:str):
        try:
            r=self._selected(); self.db.update_license(r.license_id,status=status); self.set_status(f"License {status}."); self.refresh_license_list()
        except Exception as e: QMessageBox.warning(self,"Status",str(e))

    def copy_selected_key(self):
        try: copy_text_qt(self._selected().license_key); self.set_status("License key copied.")
        except Exception as e: QMessageBox.warning(self,"Copy",str(e))

    def browse_release_file(self):
        path,_=QFileDialog.getOpenFileName(self,"Select update file")
        if path: self.rel_file.setText(path)

    def add_release(self):
        try:
            self.db.add_release(self.rel_version.text(), self.rel_channel.currentText(), self.rel_file.text(), self.rel_notes.toPlainText(), self.rel_required.isChecked()); self.refresh_releases(); self.set_status("Release saved.")
        except Exception as e: QMessageBox.critical(self,"Release",str(e))

    def refresh_releases(self):
        if hasattr(self,'release_list'):
            lines=[]
            for r in self.db.list_releases(): lines.append(f"{r['version']} · {r['channel']} · {r['size_bytes']} bytes · {r['file_path']}")
            self.release_list.setPlainText("\n".join(lines) or "No releases yet.")

    def export_public_status(self):
        path,_=QFileDialog.getSaveFileName(self,"Export public status",str(EXPORT_DIR/'license_status.json'),"JSON (*.json)")
        if path: self.db.export_public_licenses(Path(path)); self.log_export(path)

    def export_csv(self):
        path,_=QFileDialog.getSaveFileName(self,"Export CSV",str(EXPORT_DIR/'licenses_backup.csv'),"CSV (*.csv)")
        if path: self.db.export_csv(Path(path)); self.log_export(path)

    def export_manifest(self):
        path,_=QFileDialog.getSaveFileName(self,"Export manifest",str(EXPORT_DIR/'manifest.json'),"JSON (*.json)")
        if path: self.db.export_manifest(Path(path), DEFAULT_UPDATE_DOWNLOAD_URL); self.log_export(path)

    def log_export(self,path):
        self.export_log.append(f"Exported: {path}"); self.set_status(f"Exported {path}")

    def set_status(self,text): self.status.setText(text)


def main():
    app=QApplication(sys.argv); app.setStyle("Fusion"); win=AdminQtApp(); win.show(); sys.exit(app.exec())

if __name__ == "__main__":
    main()
