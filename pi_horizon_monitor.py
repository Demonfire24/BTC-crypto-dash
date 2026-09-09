#!/usr/bin/env python3
"""Pi Network Horizon payout monitor.

Watches a Pi Network (Stellar Horizon) account for outgoing payouts, either
live, over a historical window, or aggregated into payout "waves".

Requires: PyQt6, requests
"""

import re
import struct
import sys
import threading
from base64 import b32decode
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QHBoxLayout, QWidget, QTabWidget, QDoubleSpinBox,
    QSpinBox, QGroupBox, QProgressBar, QSystemTrayIcon, QStyle
)

DEFAULT_WALLET = "GABT7EMPGNCQSZM22DIYC4FNKHUVJTXITUF6Y5HNIWPU4GA7BHT4GC5G"

HORIZON_BASE = "https://api.mainnet.minepi.com"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
USER_AGENT = "pi-horizon-monitor/1.0"

# (connect, read) timeouts. A bounded read timeout keeps shutdown responsive.
REQUEST_TIMEOUT = (5, 20)
PRICE_POLL_SECONDS = 60
LEDGER_POLL_SECONDS = 15

HORIZON_PAGE_LIMIT = 100
LIVE_MAX_PAGES = 3          # enough to see past a 100-operation wave
HISTORICAL_MAX_PAGES = 100
ANALYTICS_MAX_PAGES = 50

MAX_TABLE_ROWS = 2000       # keeps a multi-day live session from growing forever
MAX_LOG_LINES = 500
MAX_TRACKED_IDS = 20000
SHUTDOWN_TIMEOUT_MS = 4000

WALLET_RE = re.compile(r"^G[A-Z2-7]{55}$")


# ==========================================
# HELPERS
# ==========================================

def _crc16_xmodem(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def is_valid_wallet(address):
    """True if `address` is a well-formed Stellar/Pi ed25519 public key."""
    address = (address or "").strip()
    if not WALLET_RE.match(address):
        return False
    try:
        raw = b32decode(address)
    except Exception:
        return False
    if len(raw) != 35 or raw[0] != 0x30:  # version byte for ed25519 public key
        return False
    payload, checksum = raw[:-2], struct.unpack("<H", raw[-2:])[0]
    return _crc16_xmodem(payload) == checksum


def safe_float(value, default=0.0):
    """Horizon returns amounts as strings; never let a bad one kill a thread."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_horizon_time(value):
    """Parse a Horizon ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # A timestamp without an offset would break every comparison downstream.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_native_amount(record):
    """True only for native Pi. Payments carry `asset_type`, claimable
    balances carry `asset`; a non-native token must never be counted as Pi."""
    asset_type = record.get("asset_type")
    if asset_type is not None:
        return asset_type == "native"
    return record.get("asset") == "native"


def new_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def operations_url(wallet):
    # The wallet is validated before use, but keep it out of the path structure.
    return (
        f"{HORIZON_BASE}/accounts/{quote(wallet, safe='')}"
        f"/operations?order=desc&limit={HORIZON_PAGE_LIMIT}"
    )


def extract_payout(record, wallet, min_amount=0.0):
    """Parse a Horizon operation record into a standardized dictionary."""
    op_type = record.get("type")
    if op_type not in ("payment", "create_claimable_balance"):
        return None

    # Failed transactions are excluded by Horizon by default; be explicit anyway.
    if record.get("transaction_successful") is False:
        return None
    if not is_native_amount(record):
        return None

    created_at = parse_horizon_time(record.get("created_at"))
    if created_at is None:
        return None

    # A missing, unparseable or non-positive amount is not a payout; it must
    # not reach the table as a bogus 0.00 Pi row.
    amount = safe_float(record.get("amount"), default=None)
    if amount is None or amount <= 0 or amount < min_amount:
        return None

    if op_type == "payment":
        if record.get("from") != wallet:
            return None
        payout_type = "Payment"
        target = str(record.get("to"))
    else:
        sponsor = record.get("sponsor") or record.get("source_account")
        if sponsor != wallet:
            return None
        payout_type = "Claimable Bal"
        target = "Unknown"
        for claimant in record.get("claimants") or []:
            destination = claimant.get("destination")
            if destination and destination != wallet:
                target = destination
                break

    return {
        "time": created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "dt": created_at,
        "type": payout_type,
        "amount": f"{amount:,.2f}",
        "raw_amount": amount,
        "target": target,
        "hash": str(record.get("transaction_hash") or ""),
        "id": str(record.get("id")),
    }


def wave_slice(payouts, gap_minutes):
    """Split a newest-first payout list at the first gap wider than
    `gap_minutes`. Returns (leading wave, boundary_found)."""
    for i in range(len(payouts) - 1):
        gap = (payouts[i]["dt"] - payouts[i + 1]["dt"]).total_seconds() / 60.0
        if gap > gap_minutes:
            return payouts[:i + 1], True
    return list(payouts), False


class BoundedIdSet:
    """Membership set that forgets the oldest ids instead of growing forever."""

    def __init__(self, max_size=MAX_TRACKED_IDS):
        self.max_size = max_size
        self._ids = set()
        self._order = deque()

    def __contains__(self, item):
        return item in self._ids

    def add(self, item):
        if item in self._ids:
            return
        self._ids.add(item)
        self._order.append(item)
        while len(self._order) > self.max_size:
            self._ids.discard(self._order.popleft())


class LiveSettings:
    """Thread-safe mirror of the live-monitor spin boxes.

    Worker threads must never touch a QWidget, so the GUI thread pushes new
    values in here and the worker reads a snapshot.
    """

    def __init__(self, min_amount=0.0, ping_interval=180):
        self._lock = threading.Lock()
        self._min_amount = float(min_amount)
        self._ping_interval = int(ping_interval)

    def set_min_amount(self, value):
        with self._lock:
            self._min_amount = float(value)

    def set_ping_interval(self, value):
        with self._lock:
            self._ping_interval = max(1, int(value))

    def snapshot(self):
        with self._lock:
            return self._min_amount, self._ping_interval


# ==========================================
# WORKER THREADS
# ==========================================

class BaseWorker(QThread):
    """QThread with a cooperative, instantly-interruptible stop."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._session = None

    @property
    def session(self):
        # Created lazily so it is owned by the worker thread that uses it.
        if self._session is None:
            self._session = new_session()
        return self._session

    def close_session(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def stop(self):
        """Ask the thread to finish. Never blocks the caller."""
        self._stop_event.set()

    @property
    def stopping(self):
        return self._stop_event.is_set()

    def sleep_interruptible(self, seconds):
        """Sleep, waking immediately on stop. Returns False if stopped."""
        return not self._stop_event.wait(seconds)

    def run(self):
        """Guard the worker body: an exception escaping QThread.run() is
        fatal to the whole application under PyQt6."""
        try:
            self.work()
        except Exception as exc:  # noqa: BLE001 - a worker must never abort the app
            self.report_unexpected(exc)
        finally:
            self.close_session()

    def work(self):
        raise NotImplementedError

    def report_unexpected(self, exc):
        """Overridden by workers that own a log signal."""
        print(f"{type(self).__name__} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)

    def iter_operation_pages(self, url, max_pages):
        """Yield (page_number, records) for a Horizon operations feed.

        Network/JSON errors propagate to the caller so each worker can report
        them in its own log.
        """
        visited = set()
        page = 0
        while url and page < max_pages and not self.stopping:
            page += 1
            visited.add(url)
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            records = data.get("_embedded", {}).get("records", [])
            yield page, records
            if not records:
                return
            next_url = data.get("_links", {}).get("next", {}).get("href")
            # Only follow paging links that stay on the Horizon host.
            if (not next_url or next_url in visited
                    or not next_url.startswith(HORIZON_BASE)):
                return
            url = next_url


class PiPriceWorker(BaseWorker):
    price_updated = pyqtSignal(float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_price = 0.0

    def work(self):
        params = {"ids": "pi-network", "vs_currencies": "usd"}
        while not self.stopping:
            self._poll_once(params)
            self.sleep_interruptible(PRICE_POLL_SECONDS)

    def _poll_once(self, params):
        try:
            response = self.session.get(
                COINGECKO_PRICE_URL, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException:
            self._emit_problem("Network Error")
            return

        if response.status_code == 429:
            self._emit_problem("Rate Limited")
            return
        if response.status_code != 200:
            self._emit_problem(f"HTTP {response.status_code}")
            return

        try:
            data = response.json()
        except ValueError:
            self._emit_problem("Bad Response")
            return

        price = safe_float(data.get("pi-network", {}).get("usd"), default=None)
        if price is None or price <= 0:
            self._emit_problem("Price N/A")
            return

        self._last_price = price
        self.price_updated.emit(price, f"${price:,.4f}")

    def _emit_problem(self, reason):
        # Keep the last good price so USD conversions survive a blip.
        if self._last_price > 0:
            self.price_updated.emit(
                self._last_price, f"${self._last_price:,.4f} ({reason})"
            )
        else:
            self.price_updated.emit(0.0, reason)


class MainnetLedgerWorker(BaseWorker):
    ledger_updated = pyqtSignal(dict)
    ledger_error = pyqtSignal(str)

    def work(self):
        url = f"{HORIZON_BASE}/ledgers?order=desc&limit=1"
        while not self.stopping:
            self._poll_once(url)
            self.sleep_interruptible(LEDGER_POLL_SECONDS)

    def report_unexpected(self, exc):
        self.ledger_error.emit(type(exc).__name__)

    def _poll_once(self, url):
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            records = response.json().get("_embedded", {}).get("records", [])
        except (requests.RequestException, ValueError) as exc:
            self.ledger_error.emit(type(exc).__name__)
            return

        if not records:
            self.ledger_error.emit("No ledger data")
            return

        latest = records[0]
        closed_at = parse_horizon_time(latest.get("closed_at"))
        self.ledger_updated.emit({
            "sequence": latest.get("sequence"),
            "protocol_version": latest.get("protocol_version"),
            "base_fee": latest.get("base_fee_in_stroops"),
            "closed_at": closed_at.strftime("%Y-%m-%d %H:%M:%S") if closed_at else "",
        })


class HistoricalWorker(BaseWorker):
    record_found = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    finished_scanning = pyqtSignal(int)

    def __init__(self, wallet, min_amount, days=10, parent=None):
        super().__init__(parent)
        self.wallet = wallet.strip()
        self.min_amount = min_amount
        self.days = days
        self.time_limit = datetime.now(timezone.utc) - timedelta(days=days)

    def report_unexpected(self, exc):
        self.log_message.emit(f"Scan failed: {type(exc).__name__}: {exc}")
        self.finished_scanning.emit(0)

    def work(self):
        found_count = 0
        seen_ids = set()
        self.log_message.emit(
            f"Starting {self.days}-day scan for payouts >= {self.min_amount:,.2f} Pi..."
        )

        try:
            pages = self.iter_operation_pages(
                operations_url(self.wallet), HISTORICAL_MAX_PAGES
            )
            for page, records in pages:
                self.log_message.emit(f"Scanned page {page} ({len(records)} ops)...")
                if not records:
                    break

                reached_limit = False
                for record in records:
                    if self.stopping:
                        break

                    created_at = parse_horizon_time(record.get("created_at"))
                    if created_at is None:
                        continue
                    if created_at < self.time_limit:
                        self.log_message.emit(
                            f"Reached {self.days}-day limit. Stopping."
                        )
                        reached_limit = True
                        break

                    item = extract_payout(record, self.wallet, self.min_amount)
                    if item and item["id"] not in seen_ids:
                        seen_ids.add(item["id"])
                        found_count += 1
                        self.record_found.emit(item)

                if reached_limit or self.stopping:
                    break
                if page >= HISTORICAL_MAX_PAGES:
                    self.log_message.emit(
                        f"Stopped at the {HISTORICAL_MAX_PAGES}-page safety cap; "
                        "results may be incomplete."
                    )
        except (requests.RequestException, ValueError) as exc:
            self.log_message.emit(f"API Error: {exc}")

        self.finished_scanning.emit(found_count)


class LiveMonitorWorker(BaseWorker):
    record_found = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    # active, last_tx_time, session_top_sum, session_top_wallet, wave_start,
    # wave_vol, session_vol
    status_update = pyqtSignal(bool, str, float, str, str, float, float)
    # [(recipient, session_total, payout_count)] biggest first, session volume
    recipient_totals = pyqtSignal(list, float)
    last_ping = pyqtSignal(str)

    MAX_RANKED_RECIPIENTS = 500

    def __init__(self, wallet, settings, wave_gap_mins=60, parent=None):
        super().__init__(parent)
        self.wallet = wallet.strip()
        self.settings = settings
        self.wave_gap_mins = wave_gap_mins
        self.emitted_ids = BoundedIdSet()   # already listed in the table
        self.counted_ids = BoundedIdSet()   # already added to the session totals
        self.session_total_vol = 0.0
        self._recipient_totals = {}
        self._force_ping = threading.Event()
        self._first_cycle = True

    def stop(self):
        super().stop()
        self._force_ping.set()  # wake an in-progress interval wait

    def trigger_force_ping(self):
        self._force_ping.set()

    def report_unexpected(self, exc):
        self.log_message.emit(f"Monitor stopped: {type(exc).__name__}: {exc}")

    def work(self):
        self.log_message.emit("Monitoring live payouts...")
        while not self.stopping:
            try:
                self.process_active_cycle()
            except Exception as exc:  # one bad cycle must not end the monitor
                self.log_message.emit(f"Cycle error: {type(exc).__name__}: {exc}")
            if self.stopping:
                break
            _, ping_interval = self.settings.snapshot()
            self._force_ping.wait(ping_interval)
            self._force_ping.clear()

    def _fetch_wave(self):
        """Fetch enough pages to cover the current wave. Returns (wave, ok)."""
        all_payouts = []
        wave = []
        try:
            pages = self.iter_operation_pages(
                operations_url(self.wallet), LIVE_MAX_PAGES
            )
            for _, records in pages:
                self.last_ping.emit(utc_stamp())
                for record in records:
                    item = extract_payout(record, self.wallet, min_amount=0.0)
                    if item:
                        all_payouts.append(item)

                all_payouts.sort(key=lambda p: p["dt"], reverse=True)
                wave, boundary_found = wave_slice(all_payouts, self.wave_gap_mins)
                if boundary_found:
                    break
        except (requests.RequestException, ValueError) as exc:
            self.last_ping.emit(f"{utc_stamp()} (Error)")
            self.log_message.emit(f"API Read Error: {exc}")
            return [], False
        return wave, True

    def _tally_session(self, wave):
        """Add payouts not yet counted to the running session totals.

        Every payout counts here whatever its size. The session figures
        describe what the wallet actually paid out; the Min Amount setting
        only decides what is worth listing individually, and gating the
        totals behind it left them reading zero whenever a wallet pays out
        in many small operations.
        """
        new_count = 0
        for payout in wave:
            if payout["id"] in self.counted_ids:
                continue
            self.counted_ids.add(payout["id"])
            new_count += 1
            self.session_total_vol += payout["raw_amount"]
            entry = self._recipient_totals.setdefault(
                payout["target"], {"total": 0.0, "count": 0}
            )
            entry["total"] += payout["raw_amount"]
            entry["count"] += 1
        return new_count

    def _session_peak(self):
        """Largest per-recipient total seen this session.

        Accumulated across the whole session rather than recomputed from the
        current wave, so the headline figure never falls back when a new
        wave starts.
        """
        top_wallet, top_total = "", 0.0
        for target, entry in self._recipient_totals.items():
            if entry["total"] > top_total:
                top_wallet, top_total = target, entry["total"]
        return top_wallet, top_total

    def _emit_new_records(self, wave, min_amount):
        """List payouts at or above the threshold, oldest first."""
        emitted = 0
        for payout in reversed(wave):
            if self.stopping:
                break
            if payout["id"] in self.emitted_ids:
                continue
            if payout["raw_amount"] < min_amount:
                continue  # may cross the threshold later if the user lowers it
            self.emitted_ids.add(payout["id"])
            # The first cycle backfills the wave that was already in progress;
            # those are not new events, so they must not raise alerts.
            payout["backfill"] = self._first_cycle
            self.record_found.emit(payout)
            emitted += 1
            self.log_message.emit(
                f"⚡ [PAYOUT] {payout['amount']} Pi -> {payout['target'][:8]}..."
            )
        return emitted

    def _emit_recipient_totals(self):
        ranked = sorted(
            ((target, entry["total"], entry["count"])
             for target, entry in self._recipient_totals.items()),
            key=lambda row: row[1],
            reverse=True,
        )
        self.recipient_totals.emit(
            ranked[:self.MAX_RANKED_RECIPIENTS], self.session_total_vol
        )

    def process_active_cycle(self):
        current_min_amount, _ = self.settings.snapshot()
        wave, ok = self._fetch_wave()
        if not ok:
            return

        new_count = self._tally_session(wave)
        emitted = self._emit_new_records(wave, current_min_amount)
        self._first_cycle = False

        wave_vol = sum(payout["raw_amount"] for payout in wave)
        at_threshold = sum(
            1 for payout in wave if payout["raw_amount"] >= current_min_amount
        )
        # Say what was seen and what the filter did with it, so an empty
        # table is always explained rather than just being empty.
        self.log_message.emit(
            f"Checked {len(wave)} payouts in the current wave: {new_count} new, "
            f"{at_threshold} at/above {current_min_amount:,.2f} Pi, "
            f"{emitted} added to the table."
        )

        self._emit_recipient_totals()

        top_wallet, top_total = self._session_peak()
        if wave:
            latest_tx_dt = wave[0]["dt"]
            idle_minutes = (
                datetime.now(timezone.utc) - latest_tx_dt
            ).total_seconds() / 60.0
            is_active = idle_minutes < self.wave_gap_mins
            last_time = latest_tx_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            wave_start = wave[-1]["dt"].strftime("%Y-%m-%d %H:%M:%S UTC")
        else:
            is_active, last_time, wave_start = False, "N/A", "N/A"

        self.status_update.emit(
            is_active,
            last_time,
            top_total,
            top_wallet,
            wave_start,
            wave_vol,
            self.session_total_vol,
        )


class AnalyticsWorker(BaseWorker):
    log_message = pyqtSignal(str)
    cycles_found = pyqtSignal(list, dict)
    progress_update = pyqtSignal(int)

    def __init__(self, wallet, days=30, split_hrs=24, parent=None):
        super().__init__(parent)
        self.wallet = wallet.strip()
        self.days = days
        self.time_limit = datetime.now(timezone.utc) - timedelta(days=days)
        self.split_hrs = split_hrs
        self.max_pages = ANALYTICS_MAX_PAGES

    def report_unexpected(self, exc):
        self.log_message.emit(f"Deep scan failed: {type(exc).__name__}: {exc}")
        self.progress_update.emit(0)

    def work(self):
        raw_payouts = []
        pages_read = 0
        complete = False

        self.log_message.emit("Initiating deep scan for cycle analysis...")
        try:
            pages = self.iter_operation_pages(
                operations_url(self.wallet), self.max_pages
            )
            for page, records in pages:
                pages_read = page
                self.progress_update.emit(int((page / self.max_pages) * 100))
                if not records:
                    complete = True
                    break

                hit_time_limit = False
                for record in records:
                    if self.stopping:
                        return
                    dt = parse_horizon_time(record.get("created_at"))
                    if dt is None:
                        continue
                    if dt < self.time_limit:
                        hit_time_limit = True
                        break

                    item = extract_payout(record, self.wallet, min_amount=0.0)
                    if item:
                        raw_payouts.append(item)

                if hit_time_limit:
                    self.log_message.emit("Reached lookback limit.")
                    complete = True
                    break
        except (requests.RequestException, ValueError) as exc:
            self.log_message.emit(f"API Error during deep scan: {exc}")

        if self.stopping:
            return

        if not complete and pages_read >= self.max_pages:
            self.log_message.emit(
                f"Stopped at the {self.max_pages}-page cap before reaching "
                f"{self.days} days; older cycles are not included."
            )

        self.progress_update.emit(100)
        self.log_message.emit(f"Processing {len(raw_payouts)} transactions...")
        self.analyze_cycles(raw_payouts)

    def analyze_cycles(self, raw_payouts):
        raw_payouts.sort(key=lambda x: x["dt"])

        cycles = []
        current_cycle = None
        cycle_gap_minutes = self.split_hrs * 60

        for payout in raw_payouts:
            if current_cycle is not None:
                gap = (payout["dt"] - current_cycle["end"]).total_seconds() / 60.0
                if gap <= cycle_gap_minutes:
                    current_cycle["end"] = payout["dt"]
                    current_cycle["count"] += 1
                    current_cycle["volume"] += payout["raw_amount"]
                    continue
                cycles.append(current_cycle)

            current_cycle = {
                "start": payout["dt"], "end": payout["dt"],
                "count": 1, "volume": payout["raw_amount"],
                "gap_from_prev_hrs": 0.0,
            }

        if current_cycle:
            cycles.append(current_cycle)

        gaps_hours = []
        for i in range(1, len(cycles)):
            hours = (cycles[i]["start"] - cycles[i - 1]["end"]).total_seconds() / 3600.0
            cycles[i]["gap_from_prev_hrs"] = hours
            gaps_hours.append(hours)

        stats = {
            "total_cycles": len(cycles),
            "avg_gap_hrs": sum(gaps_hours) / len(gaps_hours) if gaps_hours else 0.0,
            "max_gap_hrs": max(gaps_hours) if gaps_hours else 0.0,
            "total_txs": len(raw_payouts),
        }

        cycles.reverse()
        if not self.stopping:
            self.cycles_found.emit(cycles, stats)


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ==========================================
# MAIN UI APPLICATION
# ==========================================

class PiScannerUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.hist_data = []
        self.live_data = []
        self.session_ranked = []
        self.session_vol = 0.0
        self.current_pi_price = 0.0
        self.hist_worker = None
        self.live_worker = None
        self.analytics_worker = None
        self.price_worker = None
        self.ledger_worker = None
        self.live_settings = LiveSettings()

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

        self.init_ui()
        self.init_threads()

    def init_ui(self):
        self.setWindowTitle("Pi Network Advanced Horizon Monitor")
        self.resize(1200, 800)

        main_layout = QVBoxLayout()
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        # --- TOP SETTINGS BOX ---
        settings_group = QGroupBox("Global Settings & Network Status")
        settings_layout = QVBoxLayout()

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Target Wallet:"))
        self.val_wallet = QLineEdit(DEFAULT_WALLET)
        self.val_wallet.setMaxLength(56)
        row1.addWidget(self.val_wallet)

        row1.addWidget(QLabel("Min Amount (Pi):"))
        self.val_amount = QDoubleSpinBox()
        self.val_amount.setDecimals(4)
        self.val_amount.setMaximum(1_000_000_000)
        self.val_amount.setValue(20000.0)
        row1.addWidget(self.val_amount)

        row1.addWidget(QLabel("Ping Interval (s):"))
        self.val_ping = QSpinBox()
        self.val_ping.setRange(10, 3600)
        self.val_ping.setValue(180)
        row1.addWidget(self.val_ping)

        row1.addWidget(QLabel("Alert Threshold:"))
        self.val_alert_thresh = QDoubleSpinBox()
        self.val_alert_thresh.setDecimals(4)
        self.val_alert_thresh.setMaximum(1_000_000_000)
        self.val_alert_thresh.setValue(50000.0)
        row1.addWidget(self.val_alert_thresh)
        settings_layout.addLayout(row1)

        # Mirror the live-tunable settings into a thread-safe holder; worker
        # threads must never read a QWidget directly.
        self.live_settings.set_min_amount(self.val_amount.value())
        self.live_settings.set_ping_interval(self.val_ping.value())
        self.val_amount.valueChanged.connect(self.live_settings.set_min_amount)
        self.val_ping.valueChanged.connect(self.live_settings.set_ping_interval)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("<b>Migration Status:</b>"))
        self.status_label = QLabel("AWAITING DATA...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(
            "background-color: gray; color: white; padding: 5px; "
            "font-weight: bold; border-radius: 4px;"
        )
        row2.addWidget(self.status_label)

        row2.addWidget(QLabel("<b>Cycle Start:</b>"))
        self.lbl_cycle_start = QLabel("AWAITING DATA...")
        self.lbl_cycle_start.setStyleSheet("color: #2c3e50; font-weight: bold;")
        row2.addWidget(self.lbl_cycle_start)

        row2.addWidget(QLabel("<b>Wave Vol:</b>"))
        self.lbl_wave_vol = QLabel("AWAITING DATA...")
        self.lbl_wave_vol.setStyleSheet("color: #2c3e50; font-weight: bold;")
        row2.addWidget(self.lbl_wave_vol)

        row2.addWidget(QLabel("<b>Total Session Vol:</b>"))
        self.lbl_cycle_vol = QLabel("AWAITING DATA...")
        self.lbl_cycle_vol.setStyleSheet("color: #2c3e50; font-weight: bold;")
        row2.addWidget(self.lbl_cycle_vol)

        row2.addWidget(QLabel("<b>Highest Account Total (Session):</b>"))
        self.max_payout_label = QLabel("AWAITING DATA...")
        self.max_payout_label.setStyleSheet("color: gray; font-weight: bold;")
        row2.addWidget(self.max_payout_label)
        row2.addStretch()
        settings_layout.addLayout(row2)

        # --- ROW 3: API HEARTBEAT, PRICE & LEDGER ---
        row3 = QHBoxLayout()
        self.lbl_last_ping = QLabel("<b>Last API Read:</b> Not Started")
        row3.addWidget(self.lbl_last_ping)

        self.lbl_ledger = QLabel("<b>Mainnet Ledger:</b> Connecting...")
        self.lbl_ledger.setStyleSheet("color: #3498db; font-weight: bold;")
        row3.addWidget(self.lbl_ledger)
        row3.addStretch()

        self.lbl_price = QLabel("<b>Live PI Price:</b> Fetching...")
        self.lbl_price.setStyleSheet(
            "color: #f39c12; font-weight: bold; font-size: 14px; "
            "background-color: #2c3e50; padding: 4px 10px; border-radius: 4px;"
        )
        row3.addWidget(self.lbl_price)

        settings_layout.addLayout(row3)
        settings_group.setLayout(settings_layout)
        main_layout.addWidget(settings_group)

        # --- TABS ---
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.tab_live = QWidget()
        self.setup_live_tab()
        self.tabs.addTab(self.tab_live, "🔴 Live Monitor")

        self.tab_hist = QWidget()
        self.setup_hist_tab()
        self.tabs.addTab(self.tab_hist, "⏪ Historical Scan (10 Days)")

        self.tab_analytics = QWidget()
        self.setup_analytics_tab()
        self.tabs.addTab(self.tab_analytics, "📊 Cycle Analytics")

        # Connected once the live tab exists, so moving the threshold
        # re-filters the session totals straight away.
        self.val_amount.valueChanged.connect(self.render_recipient_totals)

    def init_threads(self):
        self.price_worker = PiPriceWorker()
        self.price_worker.price_updated.connect(self.update_price_ui)
        self.price_worker.start()

        self.ledger_worker = MainnetLedgerWorker()
        self.ledger_worker.ledger_updated.connect(self.update_ledger_ui)
        self.ledger_worker.ledger_error.connect(self.update_ledger_error)
        self.ledger_worker.start()

    def update_price_ui(self, price_float, price_str):
        self.current_pi_price = price_float
        self.lbl_price.setText(f"<b>Live PI Price:</b> {price_str}")

    def update_ledger_ui(self, info):
        seq = info.get("sequence")
        proto = info.get("protocol_version")
        fee = info.get("base_fee")
        self.lbl_ledger.setText(
            f"<b>Mainnet Ledger:</b> #{seq} (Proto v{proto}, Fee: {fee} stroops)"
        )
        self.lbl_ledger.setToolTip(f"Closed at {info.get('closed_at', 'unknown')} UTC")

    def update_ledger_error(self, reason):
        self.lbl_ledger.setText(f"<b>Mainnet Ledger:</b> unavailable ({reason})")

    # --- TAB SETUPS ---
    def setup_live_tab(self):
        layout = QVBoxLayout()
        btn_layout = QHBoxLayout()
        self.btn_live_start = QPushButton("Start Live Monitor")
        self.btn_live_start.clicked.connect(self.start_live)
        self.btn_live_stop = QPushButton("Stop Live Monitor")
        self.btn_live_stop.setEnabled(False)
        self.btn_live_stop.clicked.connect(self.stop_live)

        self.btn_force_ping = QPushButton("⚡ Force Ping Now")
        self.btn_force_ping.setEnabled(False)
        self.btn_force_ping.clicked.connect(self.force_ping)

        btn_layout.addWidget(self.btn_live_start)
        btn_layout.addWidget(self.btn_live_stop)
        btn_layout.addWidget(self.btn_force_ping)

        layout.addLayout(btn_layout)

        layout.addWidget(QLabel("Individual payouts at or above Min Amount:"))
        self.table_live = self.create_tx_table()
        layout.addWidget(self.table_live, 3)

        self.lbl_totals_header = QLabel(
            "Recipient totals this session at or above Min Amount:"
        )
        layout.addWidget(self.lbl_totals_header)
        self.table_live_totals = QTableWidget(0, 4)
        self.table_live_totals.setHorizontalHeaderLabels([
            "Recipient", "Session Total (Pi & USD)", "Payouts", "% of Session"
        ])
        self.table_live_totals.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table_live_totals, 2)

        self.log_live = self.create_log_box(100)
        layout.addWidget(QLabel("Live Logs:"))
        layout.addWidget(self.log_live)
        self.tab_live.setLayout(layout)

    def setup_hist_tab(self):
        layout = QVBoxLayout()
        self.btn_hist_start = QPushButton("Start 10-Day Historical Scan")
        self.btn_hist_start.clicked.connect(self.start_hist)
        layout.addWidget(self.btn_hist_start)
        self.table_hist = self.create_tx_table()
        layout.addWidget(self.table_hist)
        self.log_hist = self.create_log_box(100)
        layout.addWidget(QLabel("Historical Logs:"))
        layout.addWidget(self.log_hist)
        self.tab_hist.setLayout(layout)

    def setup_analytics_tab(self):
        layout = QVBoxLayout()

        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Split Waves if Idle for (Hours):"))
        self.val_split_hrs = QSpinBox()
        self.val_split_hrs.setRange(1, 168)
        self.val_split_hrs.setValue(24)
        top_bar.addWidget(self.val_split_hrs)

        self.btn_analytics_start = QPushButton("Run Deep Cycle Analysis")
        self.btn_analytics_start.clicked.connect(self.start_analytics)
        top_bar.addWidget(self.btn_analytics_start)

        self.progress_analytics = QProgressBar()
        self.progress_analytics.setValue(0)
        top_bar.addWidget(self.progress_analytics)
        layout.addLayout(top_bar)

        stats_layout = QHBoxLayout()
        self.lbl_stat_avg = QLabel("<b>Avg Time Between Cycles:</b> N/A")
        self.lbl_stat_max = QLabel("<b>Longest Gap Found:</b> N/A")
        self.lbl_stat_total = QLabel("<b>Cycles Analyzed:</b> N/A")
        stats_layout.addWidget(self.lbl_stat_avg)
        stats_layout.addWidget(self.lbl_stat_max)
        stats_layout.addWidget(self.lbl_stat_total)
        layout.addLayout(stats_layout)

        self.table_analytics = QTableWidget(0, 6)
        self.table_analytics.setHorizontalHeaderLabels([
            "Wave Start (UTC)", "Wave End (UTC)", "Duration (Hrs)",
            "Gap from Prev (Hrs)", "Txs in Wave", "Total Volume (Pi & USD)"
        ])
        self.table_analytics.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table_analytics)

        self.log_analytics = self.create_log_box(80)
        layout.addWidget(QLabel("Analytics Logs:"))
        layout.addWidget(self.log_analytics)

        self.tab_analytics.setLayout(layout)

    # --- UI HELPERS ---
    def create_tx_table(self):
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(
            ["Timestamp (UTC)", "Type", "Amount (Pi)", "Recipient", "Tx Hash"]
        )
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    def create_log_box(self, max_height):
        box = QTextEdit()
        box.setReadOnly(True)
        box.setMaximumHeight(max_height)
        # Bound the log so a long-running session does not grow without limit.
        box.document().setMaximumBlockCount(MAX_LOG_LINES)
        return box

    def resolved_wallet(self, log_box):
        """Return the validated wallet, or None after reporting the problem."""
        wallet = self.val_wallet.text().strip()
        if not is_valid_wallet(wallet):
            log_box.append(
                "Invalid wallet address: expected a 56-character Pi/Stellar "
                "public key starting with 'G'."
            )
            return None
        return wallet

    def add_tx_record(self, item, table, data_list, insert_top=False):
        row = 0 if insert_top else table.rowCount()
        table.insertRow(row)
        if insert_top:
            data_list.insert(0, item)
        else:
            data_list.append(item)

        tx_hash = item["hash"]
        hash_display = f"{tx_hash[:15]}..." if len(tx_hash) > 15 else (tx_hash or "N/A")
        hash_item = QTableWidgetItem(hash_display)
        hash_item.setToolTip(tx_hash or "No transaction hash")

        target_item = QTableWidgetItem(item["target"])
        target_item.setToolTip(item["target"])

        table.setItem(row, 0, QTableWidgetItem(item["time"]))
        table.setItem(row, 1, QTableWidgetItem(item["type"]))
        table.setItem(row, 2, QTableWidgetItem(item["amount"]))
        table.setItem(row, 3, target_item)
        table.setItem(row, 4, hash_item)

        while table.rowCount() > MAX_TABLE_ROWS:
            drop = table.rowCount() - 1 if insert_top else 0
            table.removeRow(drop)
            if data_list:
                data_list.pop(drop if drop < len(data_list) else -1)

        # Backfilled rows describe payouts that happened before monitoring
        # started, so they must not raise an alert.
        if (table is self.table_live and not item.get("backfill")
                and item["raw_amount"] >= self.val_alert_thresh.value()):
            self.trigger_alert(item)

    def trigger_alert(self, item):
        QApplication.beep()
        if self.tray_icon.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray_icon.showMessage(
                "🚨 Large Pi Payout Detected!",
                f"{item['amount']} Pi transferred to {item['target'][:8]}...",
                QSystemTrayIcon.MessageIcon.Information,
                5000
            )

    def shutdown_worker(self, worker, timeout_ms=SHUTDOWN_TIMEOUT_MS):
        """Stop a worker without hanging the GUI on an in-flight request."""
        if worker is None:
            return
        worker.stop()
        if not worker.wait(timeout_ms):
            worker.terminate()  # last resort: the request outlived its timeout
            worker.wait(1000)

    # --- HISTORICAL ACTIONS ---
    def start_hist(self):
        if self.hist_worker and self.hist_worker.isRunning():
            return
        wallet = self.resolved_wallet(self.log_hist)
        if wallet is None:
            return

        self.table_hist.setRowCount(0)
        self.hist_data.clear()
        self.btn_hist_start.setEnabled(False)
        self.log_hist.clear()
        self.hist_worker = HistoricalWorker(wallet, self.val_amount.value())
        self.hist_worker.record_found.connect(
            lambda item: self.add_tx_record(item, self.table_hist, self.hist_data)
        )
        self.hist_worker.log_message.connect(self.log_hist.append)
        self.hist_worker.finished_scanning.connect(
            lambda count: self.log_hist.append(f"Scan complete: {count} payouts.")
        )
        # `finished` fires once the thread has really exited, unlike a signal
        # emitted from inside run().
        self.hist_worker.finished.connect(
            lambda: self.btn_hist_start.setEnabled(True)
        )
        self.hist_worker.start()

    # --- LIVE ACTIONS ---
    def start_live(self):
        if self.live_worker and self.live_worker.isRunning():
            return
        wallet = self.resolved_wallet(self.log_live)
        if wallet is None:
            return

        self.table_live.setRowCount(0)
        self.table_live_totals.setRowCount(0)
        self.live_data.clear()
        self.session_ranked = []
        self.session_vol = 0.0
        self.btn_live_start.setEnabled(False)
        self.btn_live_stop.setEnabled(True)
        self.btn_force_ping.setEnabled(True)

        self.live_worker = LiveMonitorWorker(wallet, self.live_settings)
        self.live_worker.record_found.connect(
            lambda item: self.add_tx_record(
                item, self.table_live, self.live_data, insert_top=True
            )
        )
        self.live_worker.log_message.connect(self.log_live.append)
        self.live_worker.status_update.connect(self.update_status_ui)
        self.live_worker.recipient_totals.connect(self.update_recipient_totals)
        self.live_worker.last_ping.connect(self.update_last_ping)
        self.live_worker.start()

    def stop_live(self):
        self.shutdown_worker(self.live_worker)
        self.btn_live_start.setEnabled(True)
        self.btn_live_stop.setEnabled(False)
        self.btn_force_ping.setEnabled(False)
        self.log_live.append("Live Monitor Stopped.")
        self.lbl_last_ping.setText("<b>Last API Read:</b> Stopped")

    def force_ping(self):
        if self.live_worker and self.live_worker.isRunning():
            self.log_live.append("⚡ Force ping requested by user...")
            self.live_worker.trigger_force_ping()

    def update_status_ui(self, active, last_time, max_amt, max_target,
                         start_time, wave_vol, session_vol):
        fiat_max = (f" (${max_amt * self.current_pi_price:,.2f})"
                    if self.current_pi_price > 0 else "")
        fiat_wave = (f" (${wave_vol * self.current_pi_price:,.2f})"
                     if self.current_pi_price > 0 else "")
        fiat_tot = (f" (${session_vol * self.current_pi_price:,.2f})"
                    if self.current_pi_price > 0 else "")

        self.lbl_cycle_start.setText(start_time)
        self.lbl_wave_vol.setText(f"{wave_vol:,.2f} Pi{fiat_wave}")
        self.lbl_cycle_vol.setText(f"{session_vol:,.2f} Pi{fiat_tot}")

        target_display = f"{max_target[:8]}..." if max_target else "N/A"
        self.max_payout_label.setToolTip(max_target or "No payouts seen yet")

        if active:
            self.status_label.setText(f"ACTIVE (Last Tx: {last_time})")
            self.status_label.setStyleSheet(
                "background-color: green; color: white; padding: 5px; "
                "font-weight: bold; border-radius: 4px;"
            )
            self.max_payout_label.setText(
                f"{max_amt:,.2f} Pi{fiat_max} -> {target_display}"
            )
            self.max_payout_label.setStyleSheet("color: darkgreen; font-weight: bold;")
        else:
            self.status_label.setText(f"INACTIVE (Last Tx: {last_time})")
            self.status_label.setStyleSheet(
                "background-color: darkred; color: white; padding: 5px; "
                "font-weight: bold; border-radius: 4px;"
            )
            # The session peak stands whether or not a wave is running; it is
            # cumulative, so it must not be relabelled or cleared here.
            if max_amt > 0:
                self.max_payout_label.setText(
                    f"{max_amt:,.2f} Pi{fiat_max} -> {target_display}"
                )
            else:
                self.max_payout_label.setText("N/A")
            self.max_payout_label.setStyleSheet("color: gray; font-weight: bold;")

    def update_last_ping(self, time_str):
        self.lbl_last_ping.setText(f"<b>Last API Read:</b> {time_str}")

    def update_recipient_totals(self, ranked, session_vol):
        self.session_ranked = ranked
        self.session_vol = session_vol
        self.render_recipient_totals()

    def render_recipient_totals(self, _value=None):
        """Redraw the session totals against the current Min Amount.

        Takes an ignored argument so it can be connected directly to the
        spin box's valueChanged signal.

        Driven off the stored ranking rather than the worker, so moving the
        threshold re-filters immediately instead of waiting for the next ping.
        """
        threshold = self.val_amount.value()
        rows = [row for row in self.session_ranked if row[1] >= threshold]

        self.table_live_totals.setRowCount(0)
        for target, total, count in rows:
            row = self.table_live_totals.rowCount()
            self.table_live_totals.insertRow(row)

            fiat = (f" (${total * self.current_pi_price:,.2f})"
                    if self.current_pi_price > 0 else "")
            share = (total / self.session_vol * 100.0) if self.session_vol > 0 else 0.0

            target_item = QTableWidgetItem(target)
            target_item.setToolTip(target)
            self.table_live_totals.setItem(row, 0, target_item)
            self.table_live_totals.setItem(
                row, 1, QTableWidgetItem(f"{total:,.2f} Pi{fiat}")
            )
            self.table_live_totals.setItem(row, 2, QTableWidgetItem(str(count)))
            self.table_live_totals.setItem(row, 3, QTableWidgetItem(f"{share:.1f}%"))

        matched_vol = sum(row[1] for row in rows)
        matched_fiat = (f" (${matched_vol * self.current_pi_price:,.2f})"
                        if self.current_pi_price > 0 else "")
        self.lbl_totals_header.setText(
            f"Recipient totals this session at or above {threshold:,.2f} Pi: "
            f"<b>{len(rows)}</b> of {len(self.session_ranked)} recipients, "
            f"<b>{matched_vol:,.2f} Pi{matched_fiat}</b>"
        )

    # --- ANALYTICS ACTIONS ---
    def start_analytics(self):
        if self.analytics_worker and self.analytics_worker.isRunning():
            return
        wallet = self.resolved_wallet(self.log_analytics)
        if wallet is None:
            return

        self.table_analytics.setRowCount(0)
        self.btn_analytics_start.setEnabled(False)
        self.log_analytics.clear()
        self.progress_analytics.setValue(0)

        self.lbl_stat_avg.setText("<b>Avg Time Between Cycles:</b> Analyzing...")
        self.lbl_stat_max.setText("<b>Longest Gap Found:</b> Analyzing...")
        self.lbl_stat_total.setText("<b>Cycles Analyzed:</b> Analyzing...")

        self.analytics_worker = AnalyticsWorker(
            wallet, days=30, split_hrs=self.val_split_hrs.value()
        )
        self.analytics_worker.log_message.connect(self.log_analytics.append)
        self.analytics_worker.progress_update.connect(self.progress_analytics.setValue)
        self.analytics_worker.cycles_found.connect(self.display_analytics_results)
        self.analytics_worker.finished.connect(
            lambda: self.btn_analytics_start.setEnabled(True)
        )
        self.analytics_worker.start()

    def display_analytics_results(self, cycles, stats):
        self.lbl_stat_avg.setText(
            f"<b>Avg Time Between Cycles:</b> {stats['avg_gap_hrs']:.1f} Hours"
        )
        self.lbl_stat_max.setText(
            f"<b>Longest Gap Found:</b> {stats['max_gap_hrs']:.1f} Hours"
        )
        self.lbl_stat_total.setText(
            f"<b>Cycles Analyzed:</b> {stats['total_cycles']} waves "
            f"(from {stats['total_txs']} txs)"
        )

        for cycle in cycles:
            row = self.table_analytics.rowCount()
            self.table_analytics.insertRow(row)

            start_str = cycle["start"].strftime("%Y-%m-%d %H:%M")
            end_str = cycle["end"].strftime("%Y-%m-%d %H:%M")
            duration_hrs = (cycle["end"] - cycle["start"]).total_seconds() / 3600.0

            vol_pi = cycle["volume"]
            vol_usd = vol_pi * self.current_pi_price if self.current_pi_price > 0 else 0.0
            vol_str = f"{vol_pi:,.0f} Pi" + (f" (${vol_usd:,.2f})" if vol_usd > 0 else "")

            self.table_analytics.setItem(row, 0, QTableWidgetItem(start_str))
            self.table_analytics.setItem(row, 1, QTableWidgetItem(end_str))
            self.table_analytics.setItem(row, 2, QTableWidgetItem(f"{duration_hrs:.2f}"))
            self.table_analytics.setItem(
                row, 3, QTableWidgetItem(f"{cycle['gap_from_prev_hrs']:.2f}")
            )
            self.table_analytics.setItem(row, 4, QTableWidgetItem(str(cycle["count"])))
            self.table_analytics.setItem(row, 5, QTableWidgetItem(vol_str))

    def closeEvent(self, event):
        for worker in (self.price_worker, self.ledger_worker, self.live_worker,
                       self.hist_worker, self.analytics_worker):
            self.shutdown_worker(worker)
        self.tray_icon.hide()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName("Pi Network Advanced Horizon Monitor")
    window = PiScannerUI()
    window.show()
    sys.exit(app.exec())
