#!/usr/bin/env python3
"""Report what the Pi Horizon API actually returns for a wallet.

Run this when the dashboard's numbers look wrong. It applies the monitor's
own parsing rules to real records and reports what passed, what did not,
and why -- so a surprising figure can be traced to the data behind it.

    python pi_diagnose.py                  fetch live and analyse
    python pi_diagnose.py saved.json       analyse a saved response
    python pi_diagnose.py <WALLET>         analyse a different wallet

Writes pi_diagnose_report.txt and pi_diagnose_raw.json next to this file.
"""

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _install_qt_stub_if_missing():
    """Let the diagnostic run without PyQt6.

    The monitor imports PyQt6 at module level, but none of the parsing
    helpers need it. A placeholder keeps the import working so this tool
    tests the very same code the dashboard runs.
    """
    try:
        import PyQt6  # noqa: F401
        return False
    except ImportError:
        pass

    import types

    def _make(name):
        return type(name, (), {"__init__": lambda self, *a, **k: None})

    core = types.ModuleType("PyQt6.QtCore")
    core.QThread = _make("QThread")
    core.Qt = _make("Qt")

    class _Signal:
        def __init__(self, *a, **k):
            pass

        def __get__(self, obj, objtype=None):
            return self

        def connect(self, *a, **k):
            pass

        def emit(self, *a, **k):
            pass

    core.pyqtSignal = _Signal

    widgets = types.ModuleType("PyQt6.QtWidgets")
    for name in [
        "QApplication", "QHeaderView", "QLabel", "QLineEdit", "QMainWindow",
        "QMessageBox", "QPushButton", "QTableWidget", "QTableWidgetItem",
        "QTextEdit", "QVBoxLayout", "QHBoxLayout", "QWidget", "QTabWidget",
        "QDoubleSpinBox", "QSpinBox", "QGroupBox", "QProgressBar",
        "QSystemTrayIcon", "QStyle",
    ]:
        setattr(widgets, name, _make(name))

    package = types.ModuleType("PyQt6")
    package.QtCore = core
    package.QtWidgets = widgets
    sys.modules["PyQt6"] = package
    sys.modules["PyQt6.QtCore"] = core
    sys.modules["PyQt6.QtWidgets"] = widgets
    return True


STUBBED = _install_qt_stub_if_missing()

import pi_horizon_monitor as mon  # noqa: E402


def reject_reason(record, wallet):
    """Why the monitor would ignore this record, or None if it accepts it.

    Mirrors extract_payout step by step using the same helpers, so the
    reasons reported here are the reasons the dashboard actually applies.
    """
    op_type = record.get("type")
    if op_type not in mon.PAYOUT_OP_TYPES:
        return f"operation type is '{op_type}', not a payout"
    if record.get("transaction_successful") is False:
        return "transaction failed"
    if op_type != "create_account" and not mon.is_native_amount(record):
        asset = record.get("asset_type") or record.get("asset") or "(no asset field)"
        return f"asset is '{asset}', not native Pi"
    if mon.parse_horizon_time(record.get("created_at")) is None:
        return f"unreadable created_at: {record.get('created_at')!r}"
    field = "starting_balance" if op_type == "create_account" else "amount"
    amount = mon.safe_float(record.get(field), default=None)
    if amount is None:
        return f"unreadable {field}: {record.get(field)!r}"
    if amount <= 0:
        return f"{field} is zero or negative"
    if op_type == "payment":
        if record.get("from") != wallet:
            return "payment was not sent by this wallet"
    elif op_type == "create_account":
        if (record.get("funder") or record.get("source_account")) != wallet:
            return "account was not funded by this wallet"
    else:
        sponsor = record.get("sponsor") or record.get("source_account")
        if sponsor != wallet:
            return "claimable balance was not created by this wallet"
    return None


def fetch(wallet, max_pages=3):
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": mon.USER_AGENT,
                            "Accept": "application/json"})
    url = mon.operations_url(wallet)
    records, pages = [], []
    for _ in range(max_pages):
        response = session.get(url, timeout=mon.REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        pages.append(data)
        page_records = data.get("_embedded", {}).get("records", [])
        records.extend(page_records)
        if not page_records:
            break
        next_url = data.get("_links", {}).get("next", {}).get("href")
        if not next_url or next_url == url:
            break
        url = next_url
    return records, pages


def analyse(records, wallet, out):
    def say(line=""):
        out.append(line)

    say("=" * 68)
    say("PI HORIZON DIAGNOSTIC")
    say("=" * 68)
    say(f"Wallet:        {wallet}")
    say(f"Valid address: {mon.is_valid_wallet(wallet)}")
    say(f"Records read:  {len(records)}")
    if STUBBED:
        say("Note:          PyQt6 not installed; parsing tested without it.")
    say()

    if not records:
        say("No operations came back at all. Either the wallet has no")
        say("activity, or the API could not be reached.")
        return

    say("-- Operation types --")
    for op_type, count in Counter(
            r.get("type") for r in records).most_common():
        say(f"  {count:5d}  {op_type}")
    say()

    say("-- Asset field --")
    for asset, count in Counter(
            r.get("asset_type") or r.get("asset") or "(missing)"
            for r in records).most_common():
        say(f"  {count:5d}  {asset}")
    say()

    balances = [r for r in records if r.get("type") == "create_claimable_balance"]
    if balances:
        say("-- Claimable balance shape --")
        say(f"  Claimants per balance: "
            f"{dict(Counter(len(r.get('claimants') or []) for r in balances))}")
        chosen = Counter(mon.pick_claimant(r.get("claimants"), wallet)
                         for r in balances)
        say(f"  Distinct chosen recipients: {len(chosen)}")
        for target, count in chosen.most_common(5):
            say(f"      x{count:<5d} {target or '(none readable)'}")
        first_seen = balances[0].get("claimants") or []
        if len(first_seen) > 1:
            say("  Predicates on the first balance:")
            for claimant in first_seen:
                say(f"      {json.dumps(claimant.get('predicate'))}"
                    f"  ->  {claimant.get('destination')}")
        say()

    accepted, rejected = [], Counter()
    for record in records:
        reason = reject_reason(record, wallet)
        if reason:
            rejected[reason] += 1
            continue
        item = mon.extract_payout(record, wallet, min_amount=0.0)
        if item:
            accepted.append(item)
        else:
            rejected["accepted by checks but extract_payout returned None"] += 1

    say("-- What the monitor keeps --")
    say(f"  Counted as payouts: {len(accepted)}")
    say(f"  Ignored:            {sum(rejected.values())}")
    for reason, count in rejected.most_common():
        say(f"      {count:5d}  {reason}")
    say()

    if not accepted:
        say("NOTHING was counted as a payout. The reasons above say why.")
        say("A raw sample follows so the field names can be checked.")
        say()
        say(json.dumps(records[0], indent=2)[:2000])
        return

    totals = defaultdict(lambda: {"total": 0.0, "count": 0})
    unidentified = {"total": 0.0, "count": 0}
    for item in accepted:
        bucket = totals[item["target"]] if item.get("target_known", True) \
            else unidentified
        bucket["total"] += item["raw_amount"]
        bucket["count"] += 1

    grand_total = sum(item["raw_amount"] for item in accepted)
    say("-- Recipients --")
    say(f"  Distinct identified recipients: {len(totals)}")
    say(f"  Payouts with no readable recipient: {unidentified['count']} "
        f"({unidentified['total']:,.2f} Pi)")
    say(f"  Total volume across all payouts: {grand_total:,.2f} Pi")
    say()
    say("  Top recipients by total:")
    ranked = sorted(totals.items(), key=lambda kv: kv[1]["total"], reverse=True)
    for target, entry in ranked[:10]:
        share = entry["total"] / grand_total * 100 if grand_total else 0.0
        say(f"    {entry['total']:>18,.2f} Pi  x{entry['count']:<5d} "
            f"{share:5.1f}%  {target}")
    say()

    accepted.sort(key=lambda item: item["dt"], reverse=True)
    wave, boundary = mon.wave_slice(accepted, 60)
    wave_vol = sum(item["raw_amount"] for item in wave)
    peak_target, peak_total = ("", 0.0)
    if ranked:
        peak_target, peak_total = ranked[0][0], ranked[0][1]["total"]

    say("-- What the dashboard would show on the first check --")
    say(f"  Wave Vol:              {wave_vol:,.2f} Pi  ({len(wave)} payouts)")
    say(f"  Total Session Vol:     {grand_total:,.2f} Pi")
    say(f"  Highest Account Total: {peak_total:,.2f} Pi  -> {peak_target[:12]}")
    say(f"  Wave boundary found within the records read: {boundary}")
    say()
    if not boundary:
        say("  The 60-minute gap that ends a wave was not found, so the whole")
        say("  window counts as one wave. That is why Wave Vol and Session")
        say("  Vol agree at the start.")
    if not totals:
        say("  NO recipient could be read from any payout, so there is no")
        say("  per-recipient total to report. The raw record below shows")
        say("  which fields the API actually returns.")
    elif len(totals) == 1:
        say("  Only one recipient was identified, so the highest account")
        say("  total necessarily equals the session total.")
    say()

    say("-- Time span --")
    say(f"  Newest: {accepted[0]['time']} UTC")
    say(f"  Oldest: {accepted[-1]['time']} UTC")
    say()
    say("-- One raw record for reference --")
    say(json.dumps(records[0], indent=2)[:2000])


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    wallet = mon.DEFAULT_WALLET
    records = []
    out = []

    try:
        if arg.lower().endswith(".json"):
            with open(arg, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            records = (data.get("_embedded", {}).get("records", [])
                       if isinstance(data, dict) else data)
            print(f"Analysing {len(records)} records from {arg}")
        else:
            if arg:
                wallet = arg.strip()
            print(f"Fetching operations for {wallet} ...")
            records, pages = fetch(wallet)
            raw_path = os.path.join(HERE, "pi_diagnose_raw.json")
            with open(raw_path, "w", encoding="utf-8") as handle:
                json.dump(pages[0] if pages else {}, handle, indent=2)
            print(f"Raw first page saved to {raw_path}")
    except Exception as exc:
        out.append(f"Could not gather data: {type(exc).__name__}: {exc}")
        import traceback
        out.append(traceback.format_exc())
    else:
        analyse(records, wallet, out)

    report = "\n".join(out)
    print()
    print(report)
    report_path = os.path.join(HERE, "pi_diagnose_report.txt")
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"\nReport saved to {report_path}")
    except OSError as exc:
        print(f"\nCould not save the report: {exc}")

    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


if __name__ == "__main__":
    main()
