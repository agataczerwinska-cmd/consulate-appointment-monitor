#!/usr/bin/env python3
"""
watch_booking.py
-----------------
Watches the Consulado de Colombia en Berlín Microsoft Bookings page for a
SPECIFIC SERVICE and pushes a phone notification the moment a new open
date/time appears for that service.

HOW IT WORKS
------------
1. Opens the booking page in a real (headless) browser.
2. Clicks the service card matching SERVICE_NAME (the calendar is empty/
   disabled until a service is picked -- this was the bug in v1).
3. Reads which calendar days are enabled (=bookable) across the current
   and next couple of months, and for each enabled day, clicks it and
   reads the list of available times.
4. Compares that against what it saw last time (state.json).
5. If something NEW appears -> sends a push notification via ntfy.sh and
   saves a screenshot.

SETUP -- see README.md for the full walkthrough.
Quick version:
    pip install -r requirements.txt
    playwright install chromium
    export NTFY_TOPIC=your-private-topic   (never commit the real one)
    # then edit SERVICE_NAME below if you need a different service
    python3 watch_booking.py --once --debug   (first test, check screenshot!)
    python3 watch_booking.py                  (run continuously)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ============== CONFIGURE THESE ==============
BOOKING_URL = "https://bookings.cloud.microsoft/book/CONSULADODECOLOMBIAENBERLINALEMANIA@cancilleria.gov.co/?ismsaljsauthenabled=true"

# Must match (or be a clear substring of) the service card text you saw on
# the page. Copy it as exactly as you can from the screenshot.
SERVICE_NAME = "REC. DE FIRMA EN DOC. PRIVADO"

# The ntfy.sh topic works like a password: anyone who knows it can read or
# send notifications. It is read from the environment (a GitHub Actions
# secret in CI) and is never stored in this repository.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
CHECK_INTERVAL_SECONDS = 300                       # 5 minutes, for continuous mode
MONTHS_TO_CHECK = 6                                # how many months ahead to look
MAX_DAYS_TO_CLICK_PER_MONTH = 15                   # safety cap

STATE_FILE = Path(__file__).parent / "state.json"
SCREENSHOT_FILE = Path(__file__).parent / "latest_screenshot.png"
DEBUG_FILE = Path(__file__).parent / "debug.html"
# ===============================================


def notify(title: str, message: str):
    if not NTFY_TOPIC:
        print("[!] NTFY_TOPIC is not set -- skipping notification.")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "urgent",
                "Tags": "rotating_light",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[!] Failed to send notification: {e}")


def load_previous_state():
    """Returns None if this is truly the first-ever run (no state file yet),
    otherwise returns the saved state dict (which may have an empty
    'available' list -- that's a normal, later run finding zero slots,
    NOT the same as 'no state file exists')."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def select_service(page, debug: bool = False):
    """Click the service card matching SERVICE_NAME."""
    locator = page.get_by_text(SERVICE_NAME, exact=False)
    if locator.count() == 0:
        print(f"[!] Could not find a service matching '{SERVICE_NAME}' on the page.")
        if debug:
            DEBUG_FILE.write_text(page.content())
            print(f"[debug] Saved page HTML to {DEBUG_FILE} -- check the exact service text there.")
        return False
    try:
        locator.first.click(timeout=10000)
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        print(f"[!] Failed clicking service '{SERVICE_NAME}': {e}")
        return False


def get_calendar_days(page) -> dict:
    """Returns {date_iso: is_open} using the site's REAL markup (confirmed via
    DevTools inspection): each calendar day is a
    <div role="button" data-value="2026-09-10T00:00:00.000Z" aria-label="...">
    with aria-disabled="true" present ONLY on closed/unavailable days.
    (Earlier versions of this script wrongly looked only for <button> tags,
    which this site does not use for calendar days -- that's why it missed
    real open slots.)"""
    results = {}
    cells = page.locator("div[role='button'][data-value]")
    count = cells.count()
    for i in range(count):
        cell = cells.nth(i)
        try:
            if not cell.is_visible():
                continue
            date_value = cell.get_attribute("data-value")
            if not date_value:
                continue
            aria_disabled = cell.get_attribute("aria-disabled")
            is_open = aria_disabled != "true"
            # a date can appear twice (selected vs unselected DOM state) --
            # if any instance says open, treat the day as open
            if date_value not in results:
                results[date_value] = is_open
            else:
                results[date_value] = results[date_value] or is_open
        except Exception:
            continue
    return results


def get_visible_times(page):
    """Return visible text of anything that looks like a time slot. This site
    uses div[role='button'] for custom widgets (not real <button> tags), so
    we check both -- matching strictly on a time-shaped pattern (e.g. "9:55
    AM") keeps this from accidentally picking up calendar day numbers or
    other unrelated buttons."""
    import re
    time_pattern = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM|am|pm)?$")
    times = []
    candidates = page.locator("button, [role='button'], [role='option']")
    count = candidates.count()
    for i in range(count):
        el = candidates.nth(i)
        try:
            if not el.is_visible():
                continue
            text = el.inner_text().strip()
            if not text or len(text) > 12:
                continue
            if time_pattern.match(text):
                times.append(text)
        except Exception:
            continue
    seen = set()
    out = []
    for t in times:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def get_suggested_slot_text(page):
    """MS Bookings shows a banner like 'El Septiembre 10 a las 9:55 AM' right
    after a service is selected, IF anything is open at all. Its absence is
    itself a strong signal that nothing is currently open. This is much more
    reliable than clicking through the calendar day by day."""
    import re
    try:
        candidates = page.get_by_text(re.compile(r"a las\s+\d", re.IGNORECASE))
        if candidates.count() > 0:
            text = candidates.first.inner_text().strip()
            return " ".join(text.split())
    except Exception:
        pass
    return None


def click_next_month(page) -> bool:
    # Try common label text first (works regardless of exact wording)
    for label in ["Next month", "Next", "Siguiente", "Mes siguiente", ">"]:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue

    # Fallback: find the "Month Year" header (e.g. "Julio 2026") and assume
    # the LAST button in its parent row is the "next" arrow (prev is first).
    try:
        import re
        header = page.get_by_text(re.compile(r"[A-Za-zÀ-ÿ]+\s+20\d{2}")).first
        if header.count() > 0:
            container = header.locator("xpath=..")
            buttons = container.locator("button")
            n = buttons.count()
            if n >= 2:
                buttons.nth(n - 1).click()
                page.wait_for_timeout(1200)
                return True
    except Exception:
        pass

    return False


def scan_booking_page(debug: bool = False) -> list[str]:
    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="es-ES", timezone_id="Europe/Berlin")
        page = context.new_page()
        page.set_default_timeout(30000)

        page.goto(BOOKING_URL, wait_until="networkidle")
        page.wait_for_timeout(1500)

        if not select_service(page, debug=debug):
            page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
            browser.close()
            return found

        # Primary, reliable signal: MS Bookings' own "earliest open slot" banner
        suggested = get_suggested_slot_text(page)
        if suggested:
            found.append(f"SUGGESTED: {suggested}")
            print(f"  [signal] Site suggests earliest slot: {suggested}")
        else:
            print("  [signal] No 'earliest slot' banner found (likely nothing open).")

        for month_index in range(MONTHS_TO_CHECK):
            day_states = get_calendar_days(page)
            open_dates = sorted(d for d, is_open in day_states.items() if is_open)
            print(f"  [month {month_index}] {len(open_dates)} open day(s) out of {len(day_states)} total")

            checked = 0
            for date_iso in open_dates:
                if checked >= MAX_DAYS_TO_CLICK_PER_MONTH:
                    break
                try:
                    cell = page.locator(f"div[role='button'][data-value='{date_iso}']").first
                    cell.click(timeout=5000)
                    page.wait_for_timeout(800)
                    times = get_visible_times(page)
                    if times:
                        for t in times:
                            found.append(f"{date_iso} @ {t}")
                    else:
                        found.append(f"{date_iso} (day open, no time list captured)")
                    checked += 1
                except Exception:
                    continue

            if debug and month_index == 0:
                DEBUG_FILE.write_text(page.content())
                print(f"[debug] Saved page HTML to {DEBUG_FILE}")

            if month_index < MONTHS_TO_CHECK - 1:
                advanced = click_next_month(page)
                if not advanced:
                    break

        page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
        browser.close()

    seen = set()
    unique = []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def run_once(debug: bool = False):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Checking booking page for '{SERVICE_NAME}'...")
    current = scan_booking_page(debug=debug)

    previous_state = load_previous_state()
    is_first_run = previous_state is None
    previous = previous_state.get("available", []) if previous_state else []

    new_items = [item for item in current if item not in previous]

    print(f"  Found {len(current)} open date/time entries.")
    if current:
        for item in current[:20]:
            print(f"    - {item}")

    if is_first_run:
        print("  (First run -- nothing to compare against yet, saving baseline.)")
    elif new_items:
        print(f"  >>> NEW: {new_items}")
        notify(
            "🚨 New consulate slot available!",
            f"Service: {SERVICE_NAME}\n" + "\n".join(new_items[:10]) +
            f"\n\nBook now:\n{BOOKING_URL}",
        )
    else:
        print("  No new slots since last check.")

    save_state({"available": current, "last_checked": datetime.now().isoformat()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if not NTFY_TOPIC:
        print("!! NTFY_TOPIC environment variable is not set -- checks will run but no notifications will be sent. !!")

    if args.once:
        run_once(debug=args.debug)
        return

    print(f"Watching every {CHECK_INTERVAL_SECONDS}s for '{SERVICE_NAME}'. Press Ctrl+C to stop.")
    while True:
        try:
            run_once(debug=args.debug)
        except Exception as e:
            print(f"[!] Error during check: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[!!!] CRASHED before completing this check: {e}")
        print("[!!!] This check did NOT run to completion -- no comparison was made this cycle.")
        sys.exit(1)

# ==================================================================
# SCHEDULING WITHOUT KEEPING YOUR LAPTOP AWAKE 24/7
# ==================================================================
# macOS (cron), check every 5 minutes -- run: EDITOR=nano crontab -e
#   */5 * * * * cd /Users/YOURNAME/Desktop/booking-watcher && ./venv/bin/python3 watch_booking.py --once >> log.txt 2>&1
# ==================================================================
