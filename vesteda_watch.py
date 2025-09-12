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
import re
import traceback
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, Page

# ===================== USER SETTINGS =====================
# Amsterdam + 1+ bedroom public search page (English UI)
VESTEDA_URL = "https://www.vesteda.com/en/unit-search?placeType=1&sortType=0&radius=5&s=1078%20PJ%20Amsterdam,%20Nederland&sc=woning&latitude=52.347286&longitude=4.9105463&filters=&priceFrom=600&priceTo=9999"

# Check every N minutes during working hours
CHECK_INTERVAL_MINUTES = 3

# Working window (local Amsterdam time)
WORKDAY_START = dtime(9, 0)   # 00:00
WORKDAY_END = dtime(16, 59)   # 23:59 (inclusive)

# Notify only when count INCREASES (True) or on ANY change (False)
ONLY_NOTIFY_ON_INCREASE = False

# Optional: extra debugging prints
VERBOSE = True

# Pretend to be a normal desktop browser to avoid any headless blocking
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# ===================== CONSTANTS =====================
TZ = ZoneInfo("Europe/Amsterdam")
STATE_FILE = Path("vesteda_state.json")

# Load environment (.env) for Telegram credentials
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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

def load_last_count() -> Optional[int]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return int(data.get("last_count"))
        except Exception:
            return None
    return None


def save_last_count(count: int) -> None:
    STATE_FILE.write_text(json.dumps({"last_count": count}, indent=2))

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
    # Try up to 3 attempts in case of transient load issues
    for attempt in range(1, 4):
        try:
            # Faster first paint, then optionally wait for network idle
            await page.goto(VESTEDA_URL, wait_until="domcontentloaded", timeout=60_000)
            await click_cookie_banner_if_present(page)

            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                if VERBOSE:
                    print("[warn] networkidle wait skipped")

            await ensure_all_results_loaded(page)
            await page.wait_for_timeout(1500)  # let lazy content render

            # Count via accessible role (EN + NL)
            for label in ["View unit", "Bekijk woning"]:
                try:
                    n = await page.get_by_role("link", name=label).count()
                    if n > 0:
                        if VERBOSE:
                            print(f"[count] via role('{label}') => {n}")
                        return n
                except Exception:
                    pass

            # CSS fallbacks
            for css in [
                "a:has-text('View unit')",
                "a:has-text('Bekijk woning')",
                "[data-testid*='unit-card']",
                "[class*='unit-card']",
            ]:
                try:
                    n = await page.locator(css).count()
                    if n > 0:
                        if VERBOSE:
                            print(f"[count] via CSS {css} => {n}")
                        return n
                except Exception:
                    pass

            # Fallback: try to parse a numeric total from visible text
            try:
                body_text = await page.inner_text("body")
                m = re.search(r"(\d+)\s+properties\s+for\s+rent", body_text, flags=re.I)
                if m:
                    n = int(m.group(1))
                    if VERBOSE:
                        print(f"[count] via text => {n}")
                    return n
            except Exception:
                pass

            # Save a debug screenshot on failure
            try:
                await page.screenshot(path=f"vesteda_debug_attempt{attempt}.png", full_page=True)
                print(f"[debug] Saved screenshot to vesteda_debug_attempt{attempt}.png")
            except Exception:
                pass

            raise RuntimeError("Could not determine listing count.")

        except Exception as e:
            print(f"[retry {attempt}/3] {e}")
            traceback.print_exc()
            if attempt == 3:
                # last attempt: re-raise to be handled by caller
                raise
            await asyncio.sleep(2 * attempt)

# ===================== SCHEDULER =====================

def within_work_window(now: datetime) -> bool:
    if now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    start_dt = now.replace(hour=WORKDAY_START.hour, minute=WORKDAY_START.minute, second=0, microsecond=0)
    end_dt = now.replace(hour=WORKDAY_END.hour, minute=WORKDAY_END.minute, second=0, microsecond=0)
    return start_dt <= now <= end_dt


async def monitor_loop() -> None:
    last_count = load_last_count()
    initial_msg_sent = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-US", user_agent=USER_AGENT)
        context.set_default_timeout(45_000)
        context.set_default_navigation_timeout(60_000)
        page = await context.new_page()

        while True:
            now = datetime.now(TZ)
            if within_work_window(now):
                try:
                    count = await fetch_listing_count(page)
                    # On stateless runners, avoid a startup ping every run
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
                            arrow = "\u2197\ufe0f" if delta > 0 else "\u2198\ufe0f"
                            send_telegram(
                                f"\u26a0\ufe0f Vesteda changed: {last_count} \u2192 {count} ({'+' if delta>0 else ''}{delta}) {arrow}\n{VESTEDA_URL}"
                            )
                            last_count = count
                            save_last_count(last_count)
                        elif VERBOSE:
                            print(f"[no change] {last_count} -> {count}")
                except Exception as e:
                    print(f"[ERROR] {e}")
                    traceback.print_exc()

                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
            else:
                # Sleep until next minute during off-hours
                if VERBOSE:
                    print("[sleep] Outside working window; checking again in 60s")
                await asyncio.sleep(60)


def main() -> None:
    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        print("[exit] Stopped by user")


if __name__ == "__main__":
    main()
