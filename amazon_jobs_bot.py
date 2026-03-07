"""
Amazon Jobs Bot
===============
- Auto-fetches session token via Selenium (no manual copy-paste)
- Polls the GraphQL API every X minutes
- Prints found jobs with direct links
- Saves results to JSON
- Desktop notification when jobs found (Windows/Mac/Linux)

Requirements:
    pip install selenium requests webdriver-manager plyer

Chrome must be installed on your machine.
"""

import time
import json
import requests
import threading
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

try:
    from plyer import notification
    NOTIFY = True
except ImportError:
    NOTIFY = False

# ══════════════════════════════════════════════════════════
#  CONFIG — edit these
# ══════════════════════════════════════════════════════════
CHECK_INTERVAL_MINUTES = 0.5      # how often to check
SEARCH_RADIUS_MILES    = 100     # radius around location
SEARCH_LAT             = 51.5074 # your latitude  (London default)
SEARCH_LNG             = -0.1278 # your longitude
HEADLESS               = True    # False = show browser window
TOKEN_REFRESH_HOURS    = 1       # refresh token every N hours
# ══════════════════════════════════════════════════════════

GRAPHQL_URL = "https://qy64m4juabaffl7tjakii4gdoa.appsync-api.eu-west-1.amazonaws.com/graphql"
SITE_URL    = "https://www.jobsatamazon.co.uk/app#/jobSearch"

QUERY = """
query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {
    nextToken
    jobCards {
      jobId
      jobTitle
      jobType
      employmentType
      city
      locationName
      totalPayRateMin
      totalPayRateMax
      currencyCode
      distance
      scheduleCount
      featuredJob
      bonusPay
      jobTypeL10N
      employmentTypeL10N
      totalPayRateMinL10N
      totalPayRateMaxL10N
      distanceL10N
      monthlyBasePayMin
      monthlyBasePayMax
      virtualLocation
      jobLocationType
      __typename
    }
    __typename
  }
}
"""

# ── State ─────────────────────────────────────────────────
state = {
    "auth_token": None,
    "token_fetched_at": None,
    "check_count": 0,
    "jobs_found_total": 0,
}

# ── Token Fetcher (Selenium) ──────────────────────────────
def fetch_token():
    """Opens Chrome, visits Amazon Jobs, intercepts the GraphQL auth token."""
    print("🌐 Launching Chrome to fetch auth token...")

    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    token = None
    try:
        driver.get(SITE_URL)
        # Wait up to 15s for Angular to load and fire GraphQL request
        time.sleep(8)

        logs = driver.get_log("performance")
        for entry in logs:
            log = json.loads(entry["message"])["message"]
            if log.get("method") == "Network.requestWillBeSent":
                req = log.get("params", {}).get("request", {})
                url = req.get("url", "")
                if "appsync" in url and "/graphql" in url:
                    headers = req.get("headers", {})
                    auth = headers.get("authorization") or headers.get("Authorization")
                    if auth and auth.startswith("Status|"):
                        token = auth
                        print("✅ Token captured successfully!")
                        break

        if not token:
            print("⚠️  Could not capture token from logs — trying JS injection...")
            # Fallback: inject fetch interceptor and trigger a search
            driver.execute_script("""
                window._capturedAuth = null;
                const orig = window.fetch;
                window.fetch = function(...args) {
                    if (args[0] && args[0].includes('appsync')) {
                        const h = args[1] && args[1].headers ? args[1].headers : {};
                        window._capturedAuth = h['authorization'] || h['Authorization'] || null;
                    }
                    return orig.apply(this, args);
                };
            """)
            time.sleep(5)
            token = driver.execute_script("return window._capturedAuth;")
            if token:
                print("✅ Token captured via JS injection!")

    except Exception as e:
        print(f"❌ Selenium error: {e}")
    finally:
        driver.quit()

    return token

def maybe_refresh_token():
    """Refresh token if it's old or missing."""
    now = datetime.now()
    if (
        state["auth_token"] is None
        or state["token_fetched_at"] is None
        or (now - state["token_fetched_at"]).total_seconds() > TOKEN_REFRESH_HOURS * 3600
    ):
        token = fetch_token()
        if token:
            state["auth_token"] = token
            state["token_fetched_at"] = now
        else:
            print("❌ Failed to get token. Will retry next cycle.")

# ── API Call ──────────────────────────────────────────────
def check_jobs():
    if not state["auth_token"]:
        return None

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": state["auth_token"],
        "content-type": "application/json",
        "country": "United Kingdom",
        "iscanary": "false",
        "origin": "https://www.jobsatamazon.co.uk",
        "referer": "https://www.jobsatamazon.co.uk/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
    }

    payload = {
        "operationName": "searchJobCardsByLocation",
        "query": QUERY,
        "variables": {
            "searchJobRequest": {
                "locale": "en-GB",
                "country": "United Kingdom",
                "keyWords": "",
                "equalFilters": [],
                "containFilters": [],
                "rangeFilters": [],
                "sortFields": [],
                "pageSize": 20,
                "geoQueryClause": {
                    "lat": SEARCH_LAT,
                    "lng": SEARCH_LNG,
                    "unit": "mi",
                    "distance": SEARCH_RADIUS_MILES
                }
            }
        }
    }

    try:
        r = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()

        if "errors" in data:
            err = data["errors"][0].get("message", "")
            print(f"  ⚠️  API error: {err}")
            # Token likely expired — force refresh
            if "unauthorized" in err.lower() or "token" in err.lower():
                state["auth_token"] = None
            return None

        return data["data"]["searchJobCardsByLocation"]["jobCards"]

    except Exception as e:
        print(f"  ⚠️  Request error: {e}")
        return None

# ── Notify ────────────────────────────────────────────────
def send_notification(count):
    if NOTIFY:
        try:
            notification.notify(
                title="🎉 Amazon Jobs Found!",
                message=f"{count} job(s) available near you. Check terminal for links.",
                timeout=10
            )
        except Exception:
            pass

# ── Print Job ─────────────────────────────────────────────
def print_job(job):
    url = f"https://www.jobsatamazon.co.uk/app#/jobSearch?jobId={job.get('jobId','')}"
    print(f"""
  ┌──────────────────────────────────────────────────────
  │ 🏷️  {job.get('jobTitle', 'N/A')}
  │ 📍  {job.get('locationName', job.get('city', 'N/A'))}
  │ 💷  {job.get('totalPayRateMinL10N', 'N/A')} – {job.get('totalPayRateMaxL10N', 'N/A')} / hr
  │ 🕐  {job.get('jobTypeL10N', 'N/A')} | {job.get('employmentTypeL10N', 'N/A')}
  │ 📅  {job.get('scheduleCount', 0)} schedule(s) available
  │ 🔗  {url}
  └──────────────────────────────────────────────────────""")

# ── Save Results ──────────────────────────────────────────
def save_jobs(jobs):
    fname = f"amazon_jobs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as f:
        json.dump(jobs, f, indent=2)
    print(f"\n  💾 Saved to {fname}")
    return fname

# ── Main Loop ─────────────────────────────────────────────
def main():
    print("=" * 58)
    print("  🤖 Amazon Jobs Bot")
    print(f"  📍 Radius : {SEARCH_RADIUS_MILES} miles")
    print(f"  ⏱️  Check  : every {CHECK_INTERVAL_MINUTES} minutes")
    print(f"  🔄 Token  : refreshed every {TOKEN_REFRESH_HOURS} hour(s)")
    print("=" * 58)
    print()

    while True:
        # Refresh token if needed
        maybe_refresh_token()

        if state["auth_token"]:
            state["check_count"] += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] Check #{state['check_count']} — looking for jobs...")

            jobs = check_jobs()

            if jobs is None:
                print("  ❌ Could not fetch jobs\n")
            elif len(jobs) == 0:
                print("  😴 No jobs found yet\n")
            else:
                state["jobs_found_total"] += len(jobs)
                print(f"\n  🎉 {len(jobs)} job(s) found!\n")
                for job in jobs:
                    print_job(job)
                save_jobs(jobs)
                send_notification(len(jobs))
                # Keep checking — comment out break to stop after first find
                # break
        else:
            print("  ⏳ Waiting for token before checking...\n")

        print(f"  💤 Next check in {CHECK_INTERVAL_MINUTES} minutes...\n")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
