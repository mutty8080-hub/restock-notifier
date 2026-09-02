"""
Amazon restock + price monitor.
Reads watchlist.json, checks each product, sends Telegram + Gmail alerts
when a product is BOTH in stock and at/under its max_price.
Tracks alert state in state.json so you don't get spammed every run.
Auto-removes products past their watch_until date.
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

WATCHLIST_FILE = "watchlist.json"
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "DNT": "1",
}

# ---------- config from environment (GitHub Secrets) ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_ADDRESS)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def extract_asin(value):
    """Accepts a raw ASIN or a full Amazon URL and returns the ASIN."""
    value = value.strip()
    match = re.search(r"/dp/([A-Z0-9]{10})", value)
    if match:
        return match.group(1)
    match = re.search(r"/gp/product/([A-Z0-9]{10})", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Z0-9]{10}", value):
        return value
    return value  # fall back, let it fail loudly downstream


def check_product(asin):
    """Returns (in_stock: bool, price: float|None, title: str|None, image_url: str|None)."""
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            resp = session.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [!] request failed for {asin}: {e}")
        return False, None, None, None

    if resp.status_code != 200:
        print(f"  [!] status {resp.status_code} for {asin} (possibly blocked)")
        return False, None, None, None

    html = resp.text

    # detect a CAPTCHA/blocked page specifically, so it's distinguishable
    # in the logs from a genuine "out of stock" reading
    if "api-services-support@amazon.com" in html or "Enter the characters you see below" in html:
        print(f"  [!] {asin}: got a CAPTCHA/blocked page, not the real product page")
        return False, None, None, None

    # target the specific "availability" section just for diagnostic logging
    html_lower = html.lower()
    avail_match = re.search(
        r'id="availability"[\s\S]{0,600}?<span[^>]*>([^<]+)</span>', html
    )
    if avail_match:
        print(f"    availability text: '{avail_match.group(1).strip()}'")

    # scope price search to the actual buy-box price block, not the whole
    # page — otherwise prices from unrelated carousels/other listings can
    # get picked up as if they were this product's price
    price = None
    core_match = re.search(
        r'id="(corePriceDisplay_desktop_feature_div|corePrice_feature_div|apex_desktop|'
        r'unifiedPrice_feature_div|desktop_buybox|buyBoxAccordion|centerCol)"[\s\S]{0,8000}',
        html,
    )
    search_scope = core_match.group(0) if core_match else None

    if search_scope:
        price_patterns = [
            r'"priceAmount":\s*([\d.]+)',
            r'class="a-price-whole">([\d,]+)<[^<]*<span class="a-price-fraction">(\d+)<',
            r'id="priceblock_ourprice"[^>]*>\s*\$([\d,.]+)',
            r'id="priceblock_dealprice"[^>]*>\s*\$([\d,.]+)',
            r'class="a-price-whole">([\d,]+)<',
        ]
        for pat in price_patterns:
            m = re.search(pat, search_scope)
            if m:
                try:
                    if len(m.groups()) == 2:
                        # whole + fraction captured separately (e.g. 43 + 26 -> 43.26)
                        price = float(f"{m.group(1).replace(',', '')}.{m.group(2)}")
                    else:
                        price = float(m.group(1).replace(",", ""))
                    break
                except ValueError:
                    continue
    else:
        print(f"    [!] no core price block found for {asin} — treating as no price/out of stock")

    title = None
    m = re.search(r'id="productTitle"[^>]*>\s*([^<]+)\s*<', html)
    if m:
        title = m.group(1).strip()

    image_url = None
    m = re.search(r'id="landingImage"[^>]*data-old-hires="([^"]+)"', html)
    if not m:
        m = re.search(r'id="landingImage"[^>]*src="([^"]+)"', html)
    if not m:
        m = re.search(r'"hiRes":"([^"]+)"', html)
    if m:
        image_url = m.group(1).replace("\\/", "/")

    # rule: if Amazon is showing a price in the actual buy-box, treat the
    # product as in stock — but let explicit "unavailable" text override that,
    # as a safety net against a stray/misattributed price
    in_stock = price is not None
    if avail_match and "unavailable" in avail_match.group(1).strip().lower():
        print(f"    [!] availability text says unavailable — overriding in_stock to False")
        in_stock = False

    return in_stock, price, title, image_url


def send_telegram(message, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Telegram not configured, skipping")
        return
    try:
        if image_url:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": message, "photo": image_url},
                timeout=10,
            )
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except requests.RequestException as e:
        print(f"  [!] telegram send failed: {e}")


def send_gmail(subject, body, image_url=None):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("  [!] Gmail not configured, skipping")
        return

    if image_url:
        html_body = f"""
        <div style="font-family: sans-serif;">
          <p>{body.replace(chr(10), '<br>')}</p>
          <img src="{image_url}" style="max-width: 400px; border-radius: 8px;">
        </div>
        """
        msg = MIMEText(html_body, "html")
    else:
        msg = MIMEText(body)

    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"  [!] gmail send failed: {e}")


def main():
    if os.environ.get("TEST_MODE") == "true":
        msg = "Test notification from your restock notifier — Telegram/Gmail are wired up correctly."
        print("Running in TEST_MODE, sending test notification...")
        send_telegram(msg)
        send_gmail("Restock Notifier: Test Notification", msg)
        print("Done.")
        return

    watchlist = load_json(WATCHLIST_FILE, [])
    state = load_json(STATE_FILE, {})

    if not watchlist:
        print("Watchlist is empty, nothing to do.")
        return

    now = datetime.now(timezone.utc)
    still_active = []

    for item in watchlist:
        asin = extract_asin(item["asin"])
        name = item.get("name") or asin
        max_price = item.get("max_price")
        watch_until = item.get("watch_until")  # "YYYY-MM-DD"

        if watch_until:
            expiry = datetime.strptime(watch_until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now > expiry:
                print(f"[-] {name} ({asin}) expired, removing from watchlist")
                state.pop(asin, None)
                continue

        still_active.append(item)

        print(f"[.] checking {name} ({asin})")
        in_stock, price, title, image_url = check_product(asin)
        print(f"    in_stock={in_stock} price={price} image={'yes' if image_url else 'no'}")

        condition_met = in_stock and (max_price is None or (price is not None and price <= max_price))

        was_alerted = state.get(asin, {}).get("alerted", False)

        state[asin] = {
            **state.get(asin, {}),
            "alerted": state.get(asin, {}).get("alerted", False),
            "last_price": price,
            "last_in_stock": in_stock,
            "last_checked": now.strftime("%Y-%m-%d %H:%M UTC"),
        }

        if condition_met and not was_alerted:
            product_url = f"https://www.amazon.com/dp/{asin}"
            price_str = f"${price:.2f}" if price is not None else "unknown price"
            found_at = now.strftime("%Y-%m-%d %H:%M UTC")
            message = f"IN STOCK: {title or name}\n{price_str}\nFound: {found_at}\n{product_url}"
            print(f"    -> ALERT: {message}")
            send_telegram(message, image_url)
            send_gmail(f"Restock Alert: {title or name}", message, image_url)
            state[asin]["alerted"] = True
        elif not condition_met and was_alerted:
            # condition no longer true (sold out again / price rose) — reset so it can re-alert later
            state[asin]["alerted"] = False

    save_json(WATCHLIST_FILE, still_active)
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
