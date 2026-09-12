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


def belongs_to_this_shard(asin):
    """Stable hash-based assignment so a product always lands on the same
    shard regardless of list order/pruning — avoids two shards double-checking
    (or nobody checking) the same product."""
    if SHARD_COUNT <= 1:
        return True
    h = int(hashlib.md5(asin.encode()).hexdigest(), 16)
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


def check_product(asin, max_attempts=10):
    """Retries a few times with fresh sessions/delays if blocked, before giving up.
    Returns (in_stock, price, title, image_url, confirmed) — confirmed=False means
    every attempt was blocked, so this run has NO real data (not "confirmed out of stock")."""
    for attempt in range(1, max_attempts + 1):
        in_stock, price, title, image_url, blocked = _check_product_once(asin)
        if not blocked:
            return in_stock, price, title, image_url, True
        if attempt < max_attempts:
            wait = random.uniform(2, 5) * attempt
            print(f"    [!] attempt {attempt} blocked for {asin}, retrying in {wait:.1f}s...")
            time.sleep(wait)
    print(f"    [!] {asin}: still blocked after {max_attempts} attempts, giving up this run")
    return False, None, None, None, False


def _check_product_once(asin):
    """Returns (in_stock, price, title, image_url, blocked)."""
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            resp = session.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  [!] request failed for {asin}: {e}")
        return False, None, None, None, True

    if resp.status_code != 200:
        print(f"  [!] status {resp.status_code} for {asin} (possibly blocked)")
        return False, None, None, None, True

    html = resp.text
    print(f"    [debug] page length: {len(html)} chars, "
          f"has corePriceDisplay: {'corePriceDisplay' in html}, "
          f"has a-price-whole: {'a-price-whole' in html}")

    # detect a CAPTCHA/blocked page specifically, so it's distinguishable
    # in the logs from a genuine "out of stock" reading
    if "api-services-support@amazon.com" in html or "Enter the characters you see below" in html:
        print(f"  [!] {asin}: got a CAPTCHA/blocked page, not the real product page")
        return False, None, None, None, True

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
    if avail_match and "unavailable" in avail_match.group(1).strip().lower():
        print(f"    [!] availability text says unavailable — overriding in_stock to False")
        in_stock = False

    return in_stock, price, title, image_url, False


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


def answer_callback(callback_id, text=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    try:
        requests.post(url, data=data, timeout=10)
    except requests.RequestException:
        pass


def process_telegram_actions(watchlist, state):
    """Polls Telegram for 'Stop tracking' / 'Adjust price' button taps and pending
    price replies, applying them directly to the watchlist. No browser needed."""
    if not TELEGRAM_BOT_TOKEN:
        return watchlist

    offset = state.get("_telegram_offset", 0)
    print(f"[telegram] polling for updates, current offset={offset}")
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            print(f"  [!] telegram getUpdates returned error: {data}")
        updates = data.get("result", [])
        print(f"[telegram] received {len(updates)} update(s)")
    except requests.RequestException as e:
        print(f"  [!] telegram getUpdates failed: {e}")
        return watchlist

    pending_adjust = state.get("_pending_adjust")  # {"asin": ..., "chat_id": ...}

    for update in updates:
        print(f"[telegram] processing update_id={update['update_id']}, keys={list(update.keys())}")
        state["_telegram_offset"] = update["update_id"] + 1

        cq = update.get("callback_query")
        if cq:
            data = cq.get("data", "")
            print(f"[telegram] callback_query data={data!r}")
            chat_id = cq["message"]["chat"]["id"]
            if data.startswith("stop:"):
                asin = data.split(":", 1)[1]
                before = len(watchlist)
                watchlist = [i for i in watchlist if extract_asin(i["asin"]) != asin]
                state.pop(asin, None)
                if len(watchlist) < before:
                    answer_callback(cq["id"], "Stopped tracking.")
                    send_telegram(f"Stopped tracking {asin}.")
                else:
                    answer_callback(cq["id"], "Already removed.")
            elif data.startswith("adjust:"):
                asin = data.split(":", 1)[1]
                state["_pending_adjust"] = {"asin": asin, "chat_id": chat_id}
                pending_adjust = state["_pending_adjust"]
                answer_callback(cq["id"])
                send_telegram(f"Reply with the new alert price for {asin} (just the number, e.g. 49.99).")
            continue

        msg = update.get("message")
        if msg and pending_adjust and "text" in msg:
            try:
                new_price = float(msg["text"].strip().replace("$", ""))
            except ValueError:
                send_telegram("That doesn't look like a number — reply with just the price, e.g. 49.99.")
                continue
            asin = pending_adjust["asin"]
            found = False
            for item in watchlist:
                if extract_asin(item["asin"]) == asin:
                    item["max_price"] = new_price
                    found = True
            if found:
                send_telegram(f"Updated {asin} to alert at or below ${new_price:.2f}.")
            else:
                send_telegram(f"Couldn't find {asin} in your watchlist anymore.")
            state["_pending_adjust"] = None
            pending_adjust = None

    return watchlist


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

    # only shard 0 handles telegram button actions and prunes expired products —
    # both edit the SHARED watchlist.json, so only one shard should touch it
    if SHARD_INDEX == 0:
        watchlist = process_telegram_actions(watchlist, state)

        now_prune = datetime.now(timezone.utc)
        pruned = []
        for item in watchlist:
            watch_until = item.get("watch_until")
            if watch_until:
                expiry = datetime.strptime(watch_until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if now_prune > expiry:
                    print(f"[-] {item.get('name', item['asin'])} expired, removing from watchlist")
                    continue
            pruned.append(item)
        watchlist = pruned
        save_json(WATCHLIST_FILE, watchlist)

    if not watchlist:
        print("Watchlist is empty, nothing to do.")
        save_json(STATE_FILE, state)
        return

    now = datetime.now(timezone.utc)
    my_items = [item for item in watchlist if belongs_to_this_shard(extract_asin(item["asin"]))]
    print(f"[shard {SHARD_INDEX}/{SHARD_COUNT}] handling {len(my_items)} of {len(watchlist)} total product(s)")

    for item in my_items:
        asin = extract_asin(item["asin"])
        name = item.get("name") or asin
        max_price = item.get("max_price")
        watch_until = item.get("watch_until")

        # local re-check in case this shard's copy predates shard 0's pruning this same run
        if watch_until:
            expiry = datetime.strptime(watch_until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now > expiry:
                continue

        print(f"[.] checking {name} ({asin})")
        time.sleep(random.uniform(1, 3))  # small jitter, less bot-like than instant back-to-back hits
        in_stock, price, title, image_url, confirmed = check_product(asin)
        print(f"    in_stock={in_stock} price={price} image={'yes' if image_url else 'no'} confirmed={confirmed}")

        if not confirmed:
            print(f"    [!] no confirmed data this run for {asin} — skipping alert logic, state unchanged")
            continue

        condition_met = in_stock and (max_price is None or (price is not None and price <= max_price))

        prev = state.get(asin, {})
        was_alerted = prev.get("alerted", False)
        last_alert_price = prev.get("last_alert_price")
        seen_spike = prev.get("seen_spike", False)
        seen_oos = prev.get("seen_oos", False)

        if not in_stock:
            seen_oos = True
        elif was_alerted and last_alert_price is not None and price is not None:
            if price >= last_alert_price + 10:
                seen_spike = True

        can_alert = (not was_alerted) or seen_spike or seen_oos

        state[asin] = {
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
            product_url = f"https://www.amazon.com/dp/{asin}"
            stop_url = f"{PAGES_URL}?asin={asin}&action=stop"
            adjust_url = f"{PAGES_URL}?asin={asin}&action=adjust"
            price_str = f"${price:.2f}" if price is not None else "unknown price"
            found_at = now.strftime("%Y-%m-%d %H:%M UTC")
            message = f"IN STOCK: {title or name}\n{price_str}\nFound: {found_at}\n{product_url}"
            telegram_buttons = [
                {"text": "🛑 Stop tracking", "callback_data": f"stop:{asin}"},
                {"text": "✏️ Adjust price", "callback_data": f"adjust:{asin}"},
            ]
            email_links = (
                f'<p><a href="{stop_url}">Stop tracking this product</a> · '
                f'<a href="{adjust_url}">Adjust price threshold</a></p>'
            )
            print(f"    -> ALERT: {message}")
            send_telegram(message, image_url, telegram_buttons)
            send_gmail(f"Restock Alert: {title or name}", message, image_url, extra_html=email_links)
            state[asin]["alerted"] = True
            state[asin]["last_alert_price"] = price
            state[asin]["seen_spike"] = False
            state[asin]["seen_oos"] = False

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
