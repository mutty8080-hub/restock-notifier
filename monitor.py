"""
Amazon restock + price monitor.
Reads watchlist.json, checks each product, sends Telegram + Gmail alerts
when a product is BOTH in stock and at/under its max_price.
Tracks alert state in state.json so you don't get spammed every run.
Auto-removes products past their watch_until date.
"""

import hashlib
import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

WATCHLIST_FILE = "watchlist.json"
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
STATE_FILE = f"state_shard{SHARD_INDEX}.json"
PAGES_URL = os.environ.get("PAGES_URL", "https://mutty8080-hub.github.io/restock-notifier/")
VERCEL_ACTION_URL = os.environ.get("VERCEL_ACTION_URL")  # e.g. https://your-project.vercel.app/api/action


def get_item_id(item):
    """Stable identifier for a watchlist item, used for state tracking and
    sharding — an ASIN for Amazon items, a stored id (set at add-time by the
    UI) for Kohls and future non-Amazon marketplaces."""
    marketplace = item.get("marketplace", "amazon")
    if marketplace == "amazon":
        return extract_asin(item["asin"])
    return item["id"]


def belongs_to_this_shard(item_id):
    """Stable hash-based assignment so a product always lands on the same
    shard regardless of list order/pruning — avoids two shards double-checking
    (or nobody checking) the same product."""
    if SHARD_COUNT <= 1:
        return True
    h = int(hashlib.md5(item_id.encode()).hexdigest(), 16)
    return h % SHARD_COUNT == SHARD_INDEX

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
    """Accepts a raw ASIN or a full Amazon URL and returns the ASIN, normalized to uppercase."""
    value = value.strip()
    match = re.search(r"/dp/([A-Za-z0-9]{10})", value)
    if match:
        return match.group(1).upper()
    match = re.search(r"/gp/product/([A-Za-z0-9]{10})", value)
    if match:
        return match.group(1).upper()
    if re.fullmatch(r"[A-Za-z0-9]{10}", value):
        return value.upper()
    return value  # fall back, let it fail loudly downstream


def check_product(asin, session, max_attempts=5):
    """Retries a few times with the SAME session (cookies persist across retries,
    mimicking a real browsing session) before giving up.
    Returns (in_stock, price, title, image_url, definitely_unavailable, confirmed).
    confirmed=False means every attempt was blocked (no real data this run).
    definitely_unavailable=True only when the page explicitly said so — a plain
    'no price found' does NOT set this, since that can also mean our parsing
    missed it, not that the product is actually out of stock."""
    for attempt in range(1, max_attempts + 1):
        in_stock, price, title, image_url, definitely_unavailable, blocked = _check_product_once(asin, session)
        if not blocked:
            return in_stock, price, title, image_url, definitely_unavailable, True
        if attempt < max_attempts:
            wait = random.uniform(2, 5) * attempt
            print(f"    [!] attempt {attempt} blocked for {asin}, retrying in {wait:.1f}s...")
            time.sleep(wait)
    print(f"    [!] {asin}: still blocked after {max_attempts} attempts, giving up this run")
    return False, None, None, None, False, False


def _check_product_once(asin, session):
    """Returns (in_stock, price, title, image_url, blocked)."""
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        resp = session.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [!] request failed for {asin}: {e}")
        return False, None, None, None, None, True

    if resp.status_code != 200:
        print(f"  [!] status {resp.status_code} for {asin} (possibly blocked)")
        return False, None, None, None, None, True

    html = resp.text
    print(f"    [debug] page length: {len(html)} chars, "
          f"has corePriceDisplay: {'corePriceDisplay' in html}, "
          f"has a-price-whole: {'a-price-whole' in html}")

    # detect a CAPTCHA/blocked page specifically, so it's distinguishable
    # in the logs from a genuine "out of stock" reading
    if "api-services-support@amazon.com" in html or "Enter the characters you see below" in html:
        print(f"  [!] {asin}: got a CAPTCHA/blocked page, not the real product page")
        return False, None, None, None, None, True

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
    core_match = re.search(r'corePriceDisplay[^"]*"\s*class="celwidget"[\s\S]{0,4000}', html)
    if not core_match:
        core_match = re.search(r'desktop_unifiedPrice[^"]*"\s*class="celwidget"[\s\S]{0,4000}', html)
    if not core_match:
        core_match = re.search(r'corePriceDisplay[\s\S]{0,4000}', html)
    if not core_match:
        core_match = re.search(r'id="[^"]*[Pp]rice[^"]*"[\s\S]{0,4000}', html)
    search_scope = core_match.group(0) if core_match else None
    if core_match:
        print(f"    [debug] price anchor matched, scope length: {len(search_scope)}")
        print(f"    [debug] scope snippet: {search_scope[:200]!r}")

    if search_scope:
        price_patterns = [
            r'class="a-price-whole">(\d[\d,]*)[\s\S]{0,100}?<span class="a-price-fraction">(\d+)<',
            r'id="priceblock_ourprice"[^>]*>\s*\$([\d,.]+)',
            r'id="priceblock_dealprice"[^>]*>\s*\$([\d,.]+)',
            r'"priceAmount":\s*([\d.]+)',
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
    definitely_unavailable = bool(
        avail_match and "unavailable" in avail_match.group(1).strip().lower()
    )
    if definitely_unavailable:
        print(f"    [!] availability text says unavailable — overriding in_stock to False")
        in_stock = False

    return in_stock, price, title, image_url, definitely_unavailable, False


def check_kohls(url, session, max_attempts=5):
    """Retries a few times with the SAME session before giving up.
    Returns (in_stock, price, title, image_url, definitely_unavailable, confirmed)."""
    for attempt in range(1, max_attempts + 1):
        in_stock, price, title, image_url, definitely_unavailable, blocked = _check_kohls_once(url, session)
        if not blocked:
            return in_stock, price, title, image_url, definitely_unavailable, True
        if attempt < max_attempts:
            wait = random.uniform(2, 5) * attempt
            print(f"    [!] attempt {attempt} blocked for Kohls URL, retrying in {wait:.1f}s...")
            time.sleep(wait)
    print(f"    [!] still blocked after {max_attempts} attempts, giving up this run")
    return False, None, None, None, False, False


def _check_kohls_once(url, session):
    """Returns (in_stock, price, title, image_url, definitely_unavailable, blocked)."""
    try:
        resp = session.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [!] request failed for Kohls URL: {e}")
        return False, None, None, None, None, True

    if resp.status_code != 200:
        print(f"  [!] status {resp.status_code} for Kohls URL (possibly blocked)")
        return False, None, None, None, None, True

    html = resp.text
    print(f"    [debug] page length: {len(html)} chars")

    # generic bot-block detection (Kohls, like most large retailers, uses
    # Akamai/PerimeterX-style challenge pages)
    lower = html.lower()
    if "are you a human" in lower or "captcha" in lower or "access denied" in lower:
        print(f"  [!] got a CAPTCHA/blocked page, not the real product page")
        return False, None, None, None, None, True

    # Kohls (like most modern e-commerce sites) embeds structured product
    # data as JSON-LD for search engines — far more reliable than hunting
    # for the right HTML widget by trial and error.
    price = None
    title = None
    image_url = None
    availability_raw = None

    for block_match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>', html
    ):
        raw = block_match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("@type", "")
            if isinstance(entry_type, list):
                is_product = "Product" in entry_type
            else:
                is_product = entry_type == "Product"
            if not is_product:
                continue

            title = entry.get("name", title)
            image_field = entry.get("image")
            if isinstance(image_field, list) and image_field:
                image_url = image_field[0]
            elif isinstance(image_field, str):
                image_url = image_field

            offers = entry.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                price_val = offers.get("price")
                if price_val is not None:
                    try:
                        price = float(price_val)
                    except (ValueError, TypeError):
                        pass
                availability_raw = offers.get("availability", availability_raw)

    if availability_raw:
        print(f"    availability (JSON-LD): '{availability_raw}'")

    definitely_unavailable = bool(
        availability_raw and "outofstock" in availability_raw.lower().replace(" ", "")
    )
    in_stock = price is not None and not definitely_unavailable
    if not price:
        print(f"    [debug] no JSON-LD product price found on this page")

    return in_stock, price, title, image_url, definitely_unavailable, False


def send_telegram(message, image_url=None, buttons=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [!] Telegram not configured, skipping")
        return

    reply_markup = None
    if buttons:
        reply_markup = json.dumps({"inline_keyboard": [buttons]})

    try:
        if image_url:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": message, "photo": image_url}
            if reply_markup:
                data["reply_markup"] = reply_markup
            requests.post(url, data=data, timeout=10)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
            if reply_markup:
                data["reply_markup"] = reply_markup
            requests.post(url, data=data, timeout=10)
    except requests.RequestException as e:
        print(f"  [!] telegram send failed: {e}")


def send_gmail(subject, body, image_url=None, extra_html=None):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("  [!] Gmail not configured, skipping")
        return

    if image_url or extra_html:
        html_body = f"""
        <div style="font-family: sans-serif;">
          <p>{body.replace(chr(10), '<br>')}</p>
          {f'<img src="{image_url}" style="max-width: 400px; border-radius: 8px;">' if image_url else ''}
          {extra_html or ''}
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
        if SHARD_INDEX != 0:
            print(f"[shard {SHARD_INDEX}] test mode — only shard 0 sends the test notification, skipping.")
            return
        msg = "Test notification from your restock notifier — Telegram/Gmail are wired up correctly."
        print("Running in TEST_MODE, sending test notification...")
        send_telegram(msg)
        send_gmail("Restock Notifier: Test Notification", msg)
        print("Done.")
        return

    watchlist = load_json(WATCHLIST_FILE, [])
    state = load_json(STATE_FILE, {})

    # only shard 0 prunes expired products, since it edits the SHARED
    # watchlist.json — Telegram/Gmail stop-adjust actions are now handled
    # instantly by the Vercel webhook instead of polling here
    if SHARD_INDEX == 0:
        now_prune = datetime.now(timezone.utc)
        pruned = []
        for item in watchlist:
            watch_until = item.get("watch_until")
            if watch_until:
                expiry = datetime.strptime(watch_until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if now_prune > expiry:
                    print(f"[-] {item.get('name', get_item_id(item))} expired, removing from watchlist")
                    continue
            pruned.append(item)
        watchlist = pruned
        save_json(WATCHLIST_FILE, watchlist)

    session = requests.Session()
    session.headers.update(HEADERS)

    check_asin = os.environ.get("CHECK_ASIN", "").strip().upper()
    if check_asin:
        if SHARD_INDEX != 0:
            print(f"[shard {SHARD_INDEX}] immediate check mode — only shard 0 handles this, skipping.")
            return
        print(f"[immediate check] looking for {check_asin} in watchlist")
        item = next((i for i in watchlist if get_item_id(i).upper() == check_asin), None)
        if not item:
            print(f"[immediate check] {check_asin} not found in watchlist, nothing to do")
            return
        now = datetime.now(timezone.utc)
        check_and_maybe_alert(item, state, now, session)
        save_json(STATE_FILE, state)
        return

    if not watchlist:
        print("Watchlist is empty, nothing to do.")
        save_json(STATE_FILE, state)
        return

    now = datetime.now(timezone.utc)
    my_items = [item for item in watchlist if belongs_to_this_shard(get_item_id(item))]
    print(f"[shard {SHARD_INDEX}/{SHARD_COUNT}] handling {len(my_items)} of {len(watchlist)} total product(s)")

    for item in my_items:
        check_and_maybe_alert(item, state, now, session)

    save_json(STATE_FILE, state)


def check_and_maybe_alert(item, state, now, session):
    marketplace = item.get("marketplace", "amazon")
    item_id = get_item_id(item)
    name = item.get("name") or item_id
    max_price = item.get("max_price")
    watch_until = item.get("watch_until")

    # local re-check in case this shard's copy predates shard 0's pruning this same run
    if watch_until:
        expiry = datetime.strptime(watch_until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if now > expiry:
            return

    print(f"[.] checking {name} ({item_id}) [{marketplace}]")
    time.sleep(random.uniform(1, 3))  # small jitter, less bot-like than instant back-to-back hits

    if marketplace == "kohls":
        in_stock, price, title, image_url, definitely_unavailable, confirmed = check_kohls(item["url"], session)
    else:
        in_stock, price, title, image_url, definitely_unavailable, confirmed = check_product(item_id, session)

    print(f"    in_stock={in_stock} price={price} image={'yes' if image_url else 'no'} "
          f"definitely_unavailable={definitely_unavailable} confirmed={confirmed}")

    if not confirmed:
        print(f"    [!] no confirmed data this run for {item_id} — skipping alert logic, state unchanged")
        return

    condition_met = in_stock and (max_price is None or (price is not None and price <= max_price))

    prev = state.get(item_id, {})
    was_alerted = prev.get("alerted", False)
    last_alert_price = prev.get("last_alert_price")
    seen_spike = prev.get("seen_spike", False)
    seen_oos = prev.get("seen_oos", False)

    if definitely_unavailable:
        seen_oos = True
    elif was_alerted and last_alert_price is not None and price is not None:
        if price >= last_alert_price + 10:
            seen_spike = True

    can_alert = (not was_alerted) or seen_spike or seen_oos

    state[item_id] = {
        **prev,
        "alerted": was_alerted,
        "last_alert_price": last_alert_price,
        "seen_spike": seen_spike,
        "seen_oos": seen_oos,
        "last_price": price,
        "last_in_stock": in_stock,
        "last_checked": now.strftime("%Y-%m-%d %H:%M UTC"),
    }

    if condition_met and can_alert:
        product_url = f"https://www.amazon.com/dp/{item_id}" if marketplace == "amazon" else item["url"]
        if VERCEL_ACTION_URL:
            stop_url = f"{VERCEL_ACTION_URL}?asin={item_id}&do=stop"
            adjust_url = f"{VERCEL_ACTION_URL}?asin={item_id}&do=adjust"
        else:
            stop_url = f"{PAGES_URL}?asin={item_id}&action=stop"
            adjust_url = f"{PAGES_URL}?asin={item_id}&action=adjust"
        price_str = f"${price:.2f}" if price is not None else "unknown price"
        found_at = now.strftime("%Y-%m-%d %H:%M UTC")
        message = f"IN STOCK: {title or name}\n{price_str}\nFound: {found_at}\n{product_url}"
        telegram_buttons = [
            {"text": "🛑 Stop tracking", "callback_data": f"stop:{item_id}"},
            {"text": "✏️ Adjust price", "callback_data": f"adjust:{item_id}"},
        ]
        email_links = (
            f'<p><a href="{stop_url}">Stop tracking this product</a> · '
            f'<a href="{adjust_url}">Adjust price threshold</a></p>'
        )
        print(f"    -> ALERT: {message}")
        send_telegram(message, image_url, telegram_buttons)
        send_gmail(f"Restock Alert: {title or name}", message, image_url, extra_html=email_links)
        state[item_id]["alerted"] = True
        state[item_id]["last_alert_price"] = price
        state[item_id]["seen_spike"] = False
        state[item_id]["seen_oos"] = False


if __name__ == "__main__":
    main()
