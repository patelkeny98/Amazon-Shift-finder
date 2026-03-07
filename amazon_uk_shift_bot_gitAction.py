"""
Amazon UK Shift Notifier Bot
================================
Monitors https://www.jobsatamazon.co.uk/app#/jobSearch
and sends a Telegram / email / sound alert when new shifts appear.

Install:
    pip install selenium webdriver-manager beautifulsoup4 requests python-dotenv

Run:
    python amazon_uk_shift_bot.py

Tips:
    - The site is a JavaScript SPA — we wait for job cards to render before scraping.
    - If no jobs appear, open the site in Chrome DevTools and update the CSS
      selectors in parse_shifts() to match current class names.
    - Set HEADLESS=false in .env while testing so you can see what the browser does.
"""

import os
import time
import smtplib
import logging
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False

# ── Load config ───────────────────────────────────────────────────────────────
load_dotenv()

TARGET_URL       = "https://www.jobsatamazon.co.uk/app#/jobSearch"
REFRESH_SECONDS  = int(os.getenv("REFRESH_SECONDS", "30"))
HEADLESS         = os.getenv("HEADLESS", "true").lower() == "true"

# Filter (optional) — only alert for shifts matching these keywords
# Leave empty to alert on ALL shifts
KEYWORDS         = os.getenv("KEYWORDS", "").split(",")  # e.g. "warehouse,night"

# Telegram
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Email
EMAIL_SENDER     = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD   = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER   = os.getenv("EMAIL_RECEIVER", "")
SMTP_HOST        = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))

# Sound
SOUND_ALERT      = os.getenv("SOUND_ALERT", "true").lower() == "true"

# ── Logger ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("AmazonUK-ShiftBot")

# ── Notifications ─────────────────────────────────────────────────────────────

def notify_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured, skipping.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        log.info("✅ Telegram notification sent.")
    except Exception as e:
        log.warning(f"Telegram error: {e}")


def notify_email(subject: str, body: str) -> None:
    if not EMAIL_SENDER or not EMAIL_RECEIVER:
        log.debug("Email not configured, skipping.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        log.info("✅ Email notification sent.")
    except Exception as e:
        log.warning(f"Email error: {e}")


def notify_sound() -> None:
    if SOUND_ALERT:
        print("\a\a\a", end="", flush=True)


def alert(shifts: list[dict]) -> None:
    count   = len(shifts)
    lines   = "\n".join(
        f"• {s['title']} — {s['location']} | {s['type']} | {s['pay']}"
        for s in shifts
    )
    message = (
        f"🚨 <b>{count} Amazon UK shift{'s' if count > 1 else ''} found!</b>\n\n"
        f"{lines}\n\n"
        f"🔗 {TARGET_URL}\n"
        f"⏰ {datetime.now().strftime('%d %b %Y %H:%M:%S')}"
    )
    log.warning(f"\n{'='*50}\nSHIFTS FOUND:\n{lines}\n{'='*50}")
    notify_telegram(message)
    notify_email(
        subject=f"[ShiftBot] {count} Amazon UK shift(s) available!",
        body=message.replace("<b>", "").replace("</b>", ""),
    )
    notify_sound()

# ── Browser ───────────────────────────────────────────────────────────────────

def create_driver() -> webdriver.Chrome:
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    # Realistic user-agent to reduce bot detection
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    if USE_WDM:
        service = Service(ChromeDriverManager().install())
        driver  = webdriver.Chrome(service=service, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)

    # Hide webdriver flag
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def load_page(driver: webdriver.Chrome) -> None:
    """Navigate to the job search page and wait for content to render."""
    log.info(f"Loading {TARGET_URL} …")
    driver.get(TARGET_URL)

    # The page is a JS SPA — wait up to 20s for job cards or a "no results" notice
    wait = WebDriverWait(driver, 20)
    try:
        wait.until(
            EC.presence_of_element_located(
                # Try several possible containers — update if Amazon changes their DOM
                (By.CSS_SELECTOR, (
                    "app-job-card, "                         # Angular component
                    "[class*='job-card'], "
                    "[class*='jobCard'], "
                    "[data-test*='job'], "
                    ".job-result, "
                    ".results-list li"
                ))
            )
        )
        log.info("Job cards detected.")
    except TimeoutException:
        # Page may show "no results" — that's fine, we'll parse 0 cards
        log.info("No job cards found within timeout (may be no results).")

# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_shifts(driver: webdriver.Chrome) -> list[dict]:
    """
    Parse job listings from the rendered page.

    ⚠️  jobsatamazon.co.uk is an Angular SPA and Amazon updates its DOM often.
        If this returns 0 results:
          1. Open the URL in Chrome
          2. Press F12 → Elements tab
          3. Find the job card element
          4. Update the selectors below to match
    """
    soup  = BeautifulSoup(driver.page_source, "html.parser")
    jobs  = []

    # ── Selector set 1: Angular component tags ──────────────────────────────
    cards = soup.find_all("app-job-card")

    # ── Selector set 2: Common class-based cards (fallback) ─────────────────
    if not cards:
        cards = (
            soup.select("[class*='job-card']") or
            soup.select("[class*='jobCard']")  or
            soup.select("[data-test*='job']")  or
            soup.select(".job-result")         or
            soup.select(".results-list li")
        )

    for card in cards:
        # Try multiple likely element names for each field
        title_el    = (
            card.find(attrs={"class": lambda c: c and any(k in c for k in ["title", "jobTitle", "job-title"])})
            or card.find(["h2", "h3", "h4"])
        )
        location_el = card.find(attrs={"class": lambda c: c and any(k in c for k in ["location", "city", "site"])})
        type_el     = card.find(attrs={"class": lambda c: c and any(k in c for k in ["type", "shift", "schedule", "employment"])})
        pay_el      = card.find(attrs={"class": lambda c: c and any(k in c for k in ["pay", "salary", "rate", "wage"])})

        shift = {
            "title":    title_el.get_text(strip=True)    if title_el    else "Unknown title",
            "location": location_el.get_text(strip=True) if location_el else "Unknown location",
            "type":     type_el.get_text(strip=True)     if type_el     else "Unknown type",
            "pay":      pay_el.get_text(strip=True)      if pay_el      else "Pay not listed",
        }

        # Apply keyword filter if set
        if KEYWORDS and KEYWORDS != [""]:
            text = " ".join(shift.values()).lower()
            if not any(kw.strip().lower() in text for kw in KEYWORDS):
                continue

        jobs.append(shift)

    return jobs


def deduplicate(shifts: list[dict], seen: set) -> list[dict]:
    new = []
    for s in shifts:
        key = (s["title"], s["location"], s["type"])
        if key not in seen:
            seen.add(key)
            new.append(s)
    return new

# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("Starting Amazon UK Shift Bot (single check)…")
    log.info(f"Target URL    : {TARGET_URL}")
    log.info(f"Headless mode : {HEADLESS}")

    driver = create_driver()

    try:
        load_page(driver)
        time.sleep(4)

        all_shifts = parse_shifts(driver)
        log.info(f"Total shifts found: {len(all_shifts)}")

        new_shifts = all_shifts  # alert on everything found each run

        if new_shifts:
            alert(new_shifts)
        else:
            log.info("No shifts found this check.")

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
    finally:
        driver.quit()
        log.info("Done.")


if __name__ == "__main__":
    run()
