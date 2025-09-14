"""
Vesteda Telegram Watcher

Monitors Vesteda's public search page for Amsterdam (>= 1 bedroom) and sends a
Telegram message whenever the number of visible listings changes.

- Runs only on working days (Mon–Fri) between 09:00 and 17:00 Amsterdam time
- Checks every CHECK_INTERVAL_MINUTES (default 5 minutes)
- Persists the last seen count in vesteda_state.json (same folder)
- Notifies via a Telegram bot (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)

Dependencies:
    pip install playwright python-dotenv requests
    python -m playwright install

Tested with Python 3.10+.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, Page

# ===================== USER SETTINGS =====================
# Amsterdam + 1+ bedroom public search page (English UI)
VESTEDA_URL = (
    "https://www.vesteda.com/en/unit-search?"
    "bedRooms=1&lat=52.3666954&lng=4.89454&placeType=1&priceFrom=600&priceTo=9999&"
    "radius=10&s=Amsterdam%2C+Nederland&sc=woning&unitTypes=1&unitTypes=2&unitTypes=4"
)

# Check every N minutes during working hours
CHECK_INTERVAL_MINUTES = 5

# Working window (local Amsterdam time)
WORKDAY_START = dtime(0, 0)   # 09:00
WORKDAY_END = dtime(17, 0)    # 17:00 (inclusive)

# Notify only when count INCREASES (True) or on ANY change (False)
ONLY_NOTIFY_ON_INCREASE = False

# Optional: extra debugging prints
VERBOSE = True

# ===================== CONSTANTS =====================
try:
    TZ = ZoneInfo("Europe/Amsterdam")
except Exception:
    print("[WARN] tzdata missing; defaulting to UTC")
    TZ = timezone.utc
STATE_FILE = Path("vesteda_state.json")

# Load environment (.env) for Telegram credentials
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Single-pass mode for GitHub Actions and remote state config (optional)
SINGLE_PASS = os.getenv("SINGLE_PASS", "0") == "1"
GIST_TOKEN = os.getenv("GIST_TOKEN", "").strip()
GIST_ID = os.getenv("GIST_ID", "").strip()
GIST_FILE = os.getenv("GIST_FILE", "vesteda_state.json").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("[WARN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Messages will not be sent.")

# ===================== TELEGRAM =====================

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG disabled] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"[TG ERROR] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")

# ===================== STATE =====================

def load_last_count_remote() -> Optional[int]:
    if not GIST_TOKEN or not GIST_ID:
        return None
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"Bearer {GIST_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=20,
        )
        if r.status_code != 200:
            if VERBOSE:
                print(f"[gist] GET {r.status_code}: {r.text[:180]}")
            return None
        data = r.json()
        files = data.get("files", {})
        file_info = files.get(GIST_FILE)
        if not file_info or "content" not in file_info:
            return None
        content = file_info["content"]
        try:
            obj = json.loads(content)
            return int(obj.get("last_count"))
        except Exception:
            try:
                return int(content.strip())
            except Exception:
                return None
    except Exception as e:
        if VERBOSE:
            print(f"[gist] load error: {e}")
        return None


def save_last_count_remote(count: int) -> bool:
    if not GIST_TOKEN or not GIST_ID:
        return False
    try:
        payload = {"files": {GIST_FILE: {"content": json.dumps({"last_count": count})}}}
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"Bearer {GIST_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
            timeout=20,
        )
        if r.status_code not in (200, 201):
            if VERBOSE:
                print(f"[gist] PATCH {r.status_code}: {r.text[:180]}")
            return False
        return True
    except Exception as e:
        if VERBOSE:
            print(f"[gist] save error: {e}")
        return False


def load_last_count() -> Optional[int]:
    # Prefer remote state when available (GitHub Actions)
    v = load_last_count_remote()
    if v is not None:
        return v
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return int(data.get("last_count"))
        except Exception:
            return None
    return None


def save_last_count(count: int) -> None:
    # Save both remotely (if configured) and locally for dev runs
    saved_remote = save_last_count_remote(count)
    try:
        STATE_FILE.write_text(json.dumps({"last_count": count}, indent=2))
    except Exception:
        if VERBOSE and not saved_remote:
            print("[state] failed to save locally and remotely")

# ===================== SCRAPER =====================

COOKIE_CLICK_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("I agree")',
    'button:has-text("OK")',
    'button:has-text("Akkoord")',
    'button:has-text("Toestaan")',
]


async def ensure_all_results_loaded(page: Page) -> None:
    """Scroll to bottom repeatedly to trigger lazy-loading if present."""
    last_height = 0
    for _ in range(25):  # up to ~20 seconds total
        height = await page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)


async def click_cookie_banner_if_present(page: Page) -> None:
    for sel in COOKIE_CLICK_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                await loc.click()
                if VERBOSE:
                    print(f"[cookie] Clicked {sel}")
                return
        except Exception:
            pass


async def fetch_listing_count(page: Page) -> int:
    await page.goto(VESTEDA_URL, wait_until="networkidle", timeout=60_000)
    await click_cookie_banner_if_present(page)
    await ensure_all_results_loaded(page)

    # Count how many "View unit" links are present (one per listing card in EN UI)
    try:
        count = await page.get_by_role("link", name="View unit").count()
        if VERBOSE:
            print(f"[count] Found {count} 'View unit' links")
        return count
    except Exception:
        # Fallback: count listing cards by common CSS hooks
        for css in [
            "a:has-text('View unit')",
            ".listing-card a[href*='/en/']",
            "[data-testid='unit-card']",
        ]:
            try:
                n = await page.locator(css).count()
                if n > 0:
                    if VERBOSE:
                        print(f"[count] Fallback via {css} => {n}")
                    return n
            except Exception:
                pass
        raise RuntimeError("Could not determine listing count.")

# ===================== SCHEDULER =====================

def within_work_window(now: datetime) -> bool:
    if now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    start_dt = now.replace(hour=WORKDAY_START.hour, minute=WORKDAY_START.minute, second=0, microsecond=0)
    end_dt = now.replace(hour=WORKDAY_END.hour, minute=WORKDAY_END.minute, second=0, microsecond=0)
    return start_dt <= now <= end_dt


async def monitor_loop() -> None:
    last_count = load_last_count()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ua = globals().get("USER_AGENT") or os.getenv("USER_AGENT") or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        )
        context = await browser.new_context(locale="en-US", user_agent=ua)
        context.set_default_timeout(45_000)
        context.set_default_navigation_timeout(60_000)
        page = await context.new_page()

        while True:
            now = datetime.now(TZ)
            if within_work_window(now):
                try:
                    count = await fetch_listing_count(page)
                    if last_count is None:
                        last_count = count
                        save_last_count(last_count)
                        if VERBOSE:
                            print(f"[init] Baseline set to {count}")
                    else:
                        changed = (count != last_count)
                        increased = (count > last_count)
                        if changed and (not ONLY_NOTIFY_ON_INCREASE or increased):
                            delta = count - last_count
                            arrow = "↗️" if delta > 0 else "↘️"
                            sign = "+" if delta > 0 else ""
                            msg = (
                                f"⚠️ Vesteda changed: {last_count} → {count} "
                                f"({sign}{delta}) {arrow}\n{VESTEDA_URL}"
                            )
                            send_telegram(msg)
                            last_count = count
                            save_last_count(last_count)
                        elif VERBOSE:
                            print(f"[no change] {last_count} -> {count}")
                except Exception as e:
                    print(f"[ERROR] {e}")
                    traceback.print_exc()

                if SINGLE_PASS:
                    if VERBOSE:
                        print("[single-pass] done")
                    return
                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
            else:
                if VERBOSE:
                    print("[sleep] Outside working window")
                if SINGLE_PASS:
                    return
                await asyncio.sleep(60)

def main() -> None:
    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        print("[exit] Stopped by user")

    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        print("[exit] Stopped by user")

    # Handle Ctrl+C gracefully
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, loop.stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(monitor_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
