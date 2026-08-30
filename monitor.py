"""Ebisu Store new-arrivals monitor.

Logs into the OpenCart storefront, scrapes the New Arrivals category
(path=148), compares against LAST RUN's snapshot (not a permanent history),
and alerts via email (Gmail SMTP) + ntfy.sh push when new products appear.
Because comparison is against last run only, restocked items that
disappeared and came back will alert again.

First run saves a baseline without alerting. Login/parse failures alert
once (not every run) via an error flag persisted in state.json.
"""

import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BASE_URL = "https://www.ebisustore.com/"
LOGIN_URL = BASE_URL + "index.php?route=account/login"
CATEGORY_URL = BASE_URL + "index.php?route=product/category&path=148&limit=100"

STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "monitor.log"

SUPPLIER_EMAIL = os.getenv("SUPPLIER_EMAIL", "")
SUPPLIER_PASSWORD = os.getenv("SUPPLIER_PASSWORD", "")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_TO = os.getenv("ALERT_TO") or GMAIL_ADDRESS
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("monitor")
if sys.stdout and sys.stdout.isatty():
    log.addHandler(logging.StreamHandler(sys.stdout))


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"products": {}, "error_alerted": False}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(STATE_FILE)


def send_email(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_TO):
        log.warning("Email not configured; skipping email alert")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_ADDRESS, [ALERT_TO], msg.as_string())
        log.info("Email sent: %s", subject)
        return True
    except Exception as e:
        log.error("Email send failed: %s", e)
        return False


def send_ntfy(title, body, priority="high"):
    if not NTFY_TOPIC:
        log.warning("NTFY_TOPIC not configured; skipping push alert")
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "package",
                "Click": CATEGORY_URL,
            },
            timeout=30,
        )
        r.raise_for_status()
        log.info("ntfy push sent: %s", title)
        return True
    except Exception as e:
        log.error("ntfy push failed: %s", e)
        return False


def alert(subject, body):
    send_email(subject, body)
    send_ntfy(subject, body)


def alert_error_once(state, message):
    log.error(message)
    if state.get("error_alerted") != message:
        alert("Ebisu monitor problem", message + "\n\nYou will not be re-alerted until the monitor recovers.")
        state["error_alerted"] = message
        save_state(state)


class LayoutChangedError(Exception):
    pass


class LoginError(Exception):
    pass


def login(session):
    # IMPORTANT: GET the login page first so the session picks up its
    # cookie (and any hidden CSRF token OpenCart may render into the form)
    # before we POST credentials. Posting cold, without this GET, can
    # produce a session the server treats as unauthenticated even though
    # the POST itself returns 200 - the previous version of this script
    # skipped this step, which is a likely cause of missed/inaccurate runs.
    get_resp = session.get(LOGIN_URL, timeout=60)
    get_resp.raise_for_status()

    form_data = {"email": SUPPLIER_EMAIL, "password": SUPPLIER_PASSWORD}
    soup = BeautifulSoup(get_resp.text, "html.parser")
    token_field = soup.select_one('input[name="user_token"], input[name="csrf_token"]')
    if token_field and token_field.get("value"):
        form_data[token_field["name"]] = token_field["value"]

    resp = session.post(LOGIN_URL, data=form_data, timeout=60)
    resp.raise_for_status()
    if "route=account/login" in resp.url and 'name="password"' in resp.text:
        raise LoginError("Login rejected (still on login form). Check SUPPLIER_EMAIL / SUPPLIER_PASSWORD in .env.")
    return resp


def clean_price(text):
    m = re.search(r"[\$€£¥]?\s?\d[\d,]*\.?\d*", text)
    return m.group(0).strip() if m else text.strip()


def parse_products(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    cards = soup.select(".product-layout")
    seen_links = set()
    for card in cards:
        caption = card.select_one(".caption")
        a = (caption.select_one("a") if caption else None) or card.select_one("a[href*='route=product/product']")
        if not a or not a.get("href"):
            continue
        link = urljoin(page_url, a["href"])
        if link in seen_links:
            continue
        seen_links.add(link)
        name = a.get_text(strip=True)
        if not name:
            continue

        price_el = (caption.select_one("p.price") if caption else None) or card.select_one(".price-new, .price")
        price = clean_price(price_el.get_text(" ", strip=True)) if price_el else "n/a"

        stock_el = card.select_one(".stock, .out-of-stock, .stock-status, .availability")
        if stock_el:
            stock = stock_el.get_text(strip=True)
        elif card.find(string=re.compile(r"out of stock|sold out", re.I)):
            stock = "Out of Stock"
        else:
            stock = "n/a"

        products.append({"name": name, "price": price, "link": link, "stock": stock})

    if not products:
        page_text = soup.get_text(" ", strip=True).lower()
        if "there are no products" in page_text or "no products to list" in page_text:
            return []
        raise LayoutChangedError(
            f"No products parsed from {page_url} and no empty-category message found. "
            "The page layout may have changed or the session was not authenticated."
        )
    return products


def next_page_url(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    nxt = soup.select_one("ul.pagination a:-soup-contains('>'), ul.pagination a[rel=next]")
    if nxt and nxt.get("href"):
        url = urljoin(page_url, nxt["href"])
        if url != page_url:
            return url
    return None


def fetch_all_products(session):
    products = []
    url = CATEGORY_URL
    visited = set()
    while url and url not in visited and len(visited) < 20:
        visited.add(url)
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        if "route=account/login" in resp.url:
            raise LoginError("Category page redirected to login; session not authenticated.")
        products.extend(parse_products(resp.text, url))
        url = next_page_url(resp.text, url)
    unique = {}
    for p in products:
        unique[p["link"]] = p
    return list(unique.values())


def product_key(p):
    m = re.search(r"product_id=(\d+)", p["link"])
    return m.group(1) if m else p["link"]


def format_new_products(new_products):
    lines = []
    for p in new_products:
        lines.append(f"• {p['name']} — {p['price']} ({p['stock']})\n  {p['link']}")
    return "\n\n".join(lines)


def main():
    log.info("=== Run started ===")

    if "--test-alert" in sys.argv:
        body = (
            "This is a test alert from your Ebisu Store new-arrivals monitor.\n\n"
            "• Example Product — $9.99 (In Stock)\n  " + CATEGORY_URL + "\n\n"
            f"Sent {datetime.now():%Y-%m-%d %H:%M:%S}. If you received this by email "
            "and on your phone, both alert channels are working."
        )
        email_ok = send_email("TEST: Ebisu monitor alert test", body)
        ntfy_ok = send_ntfy("TEST: Ebisu monitor alert test", body)
        print(f"email: {'sent' if email_ok else 'FAILED (see monitor.log)'}")
        print(f"ntfy:  {'sent' if ntfy_ok else 'FAILED (see monitor.log)'}")
        log.info("=== Test-alert run finished ===")
        return 0 if (email_ok and ntfy_ok) else 1

    state = load_state()

    if not (SUPPLIER_EMAIL and SUPPLIER_PASSWORD):
        alert_error_once(state, "SUPPLIER_EMAIL / SUPPLIER_PASSWORD missing in .env; cannot log in.")
        return 1

    try:
        with requests.Session() as session:
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            })
            login(session)
            products = fetch_all_products(session)
    except (LoginError, LayoutChangedError) as e:
        alert_error_once(state, str(e))
        return 1
    except requests.RequestException as e:
        log.warning("Network error, will retry next run: %s", e)
        return 1

    if state.get("error_alerted"):
        log.info("Monitor recovered; clearing error flag")
        state["error_alerted"] = False

    # --- restock-aware diff: compare against LAST RUN's snapshot only ---
    # (not an ever-growing "seen it once" set). If a product disappears
    # and later comes back - a restock - it is absent from `known` at
    # that point and will alert again, same as a brand-new product.
    known = state["products"]
    current = {product_key(p): p for p in products}
    new_keys = [k for k in current if k not in known]

    first_run = not known
    if first_run:
        log.info("First run: baseline saved with %d products (no alert)", len(current))
    elif new_keys:
        new_products = [current[k] for k in new_keys]
        log.info("NEW/RESTOCKED products detected: %d", len(new_products))
        for p in new_products:
            log.info("  NEW: %s | %s | %s | %s", p["name"], p["price"], p["stock"], p["link"])
        subject = f"Ebisu: {len(new_products)} new/restocked item{'s' if len(new_products) > 1 else ''}!"
        alert(subject, format_new_products(new_products) + f"\n\nCategory: {CATEGORY_URL}")
    else:
        log.info("No new products (%d tracked)", len(current))

    # Replace (not merge) - this is what makes restock detection possible.
    state["products"] = current
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    state["last_count"] = len(current)
    save_state(state)

    if sys.stdout and sys.stdout.isatty():
        print(f"\n{len(current)} products on the New Arrivals page:")
        for p in current.values():
            print(f"  {p['name']} — {p['price']} ({p['stock']})")
            print(f"    {p['link']}")

    log.info("=== Run finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
