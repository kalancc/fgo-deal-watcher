"""
Fashion Group Outlet deal watcher.

Checks https://www.fashiongroupoutlet.mk/proizvodi/maski (sorted by biggest
discount) and notifies you about every product with a discount of at least
MIN_DISCOUNT % that it hasn't seen before.

Usage:
    python deal_watcher.py            # one check (use with Task Scheduler)
    python deal_watcher.py --loop 15  # keep running, check every 15 minutes
    python deal_watcher.py --test     # send a test notification

The first run only records what's already on sale (no flood of pings),
unless you pass --notify-existing.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ---------------- settings ----------------
MIN_DISCOUNT = 70  # notify for -70% and higher
BASE_URL = "https://www.fashiongroupoutlet.mk/proizvodi/maski"
SORT = "justDiscountByPriceDesc"  # site's "biggest discount first"
PER_PAGE = 48
MAX_PAGES = 60  # safety cap

# Optional phone pings via ntfy (free app for Android/iOS: https://ntfy.sh).
# Pick a long, hard-to-guess topic name, subscribe to it in the app, put it here.
# Set via the NTFY_TOPIC environment variable (a GitHub secret when run on
# GitHub Actions) so the topic never appears in the public code.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Windows pop-up notifications. Off: they seemed to trigger the AMD driver crash.
WINDOWS_TOASTS = False

# Optional Discord ping: paste a channel webhook URL here.
DISCORD_WEBHOOK = ""
# ------------------------------------------

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen_products.json"
LOG_FILE = HERE / "deal_watcher.log"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

ITEM_RE = re.compile(r'<div class="wrapper-grid-view[^"]*"((?:[^>"]|"[^"]*")*)>', re.S)
ATTR_RE = re.compile(r'data-([\w-]+)="([^"]*)"')


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_products(page_html):
    products = {}
    for m in ITEM_RE.finditer(page_html):
        attrs = {k.lower(): html.unescape(v).strip() for k, v in ATTR_RE.findall(m.group(1))}
        pid = attrs.get("productid")
        if not pid:
            continue
        try:
            discount = abs(int(attrs.get("productdiscount", "0") or 0))
        except ValueError:
            discount = 0
        # product link is the first href after this item's opening tag
        link_m = re.search(r'href="(https://www\.fashiongroupoutlet\.mk/[^"]*/' + pid + r'-[^"]*)"',
                           page_html[m.end():m.end() + 3000])
        products[pid] = {
            "id": pid,
            "name": attrs.get("productname", ""),
            "brand": attrs.get("productbrand", ""),
            "category": attrs.get("productcatbread", ""),
            "price": attrs.get("productprice", ""),
            "old_price": attrs.get("productprevprice", ""),
            "discount": discount,
            "url": link_m.group(1) if link_m else BASE_URL,
        }
    return products


def scan():
    """Return all products with discount >= MIN_DISCOUNT."""
    found = {}
    for page in range(1, MAX_PAGES + 1):
        path = BASE_URL if page == 1 else f"{BASE_URL}/page-{page}"
        url = f"{path}?sort={SORT}&limit={PER_PAGE}"
        products = parse_products(fetch(url))
        if not products:
            break
        hits = {k: v for k, v in products.items() if v["discount"] >= MIN_DISCOUNT}
        found.update(hits)
        # list is sorted by discount, so once a page has anything below the
        # threshold, we've seen every qualifying product
        if len(hits) < len(products):
            break
        time.sleep(1)  # be polite to the site
    return found


# ---------------- notifications ----------------
def toast(title, body, url):
    """Windows desktop notification; clicking it opens `url` in your browser."""
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "''"))
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast duration="long" activationType="protocol" launch="{esc(url)}"><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(body)}</text></binding></visual><actions><action content="Open" activationType="protocol" arguments="{esc(url)}"/></actions><audio src="ms-winsoundevent:Notification.Mail"/></toast>')
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       check=False, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"toast failed: {e}")


def ntfy(title, body, url):
    if not NTFY_TOPIC:
        return
    try:
        # JSON publishing keeps Cyrillic product names intact; tapping opens `url`
        payload = {"topic": NTFY_TOPIC, "title": title, "message": body, "click": url,
                   "tags": ["shopping"], "priority": 4,
                   "actions": [{"action": "view", "label": "Open", "url": url}]}
        req = urllib.request.Request(
            "https://ntfy.sh", data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log(f"ntfy failed: {e}")


def discord(text):
    if not DISCORD_WEBHOOK:
        return
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=json.dumps({"content": text[:1990]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log(f"discord failed: {e}")


def notify(new_items):
    new_items = sorted(new_items, key=lambda p: -p["discount"])
    lines = [f"-{p['discount']}% {p['brand']} {p['name']} - {p['price']} ден (was {p['old_price']})"
             for p in new_items]
    title = f"{len(new_items)} new -{MIN_DISCOUNT}%+ deal(s) at Fashion Group Outlet"
    # one clickable notification per product (max 5); if there are more,
    # one extra notification that opens the full list sorted by discount
    for p in (new_items[:5] if WINDOWS_TOASTS else []):
        toast(f"-{p['discount']}% {p['brand']} {p['name']}",
              f"{p['price']} ден (was {p['old_price']}) - click to open", p["url"])
    if WINDOWS_TOASTS and len(new_items) > 5:
        toast(f"+{len(new_items) - 5} more new deals",
              "Click to open the outlet sorted by biggest discount",
              f"{BASE_URL}?sort={SORT}&limit={PER_PAGE}")
    for p, line in list(zip(new_items, lines))[:10]:
        ntfy(f"-{p['discount']}% {p['brand']} {p['name']}",
             f"{p['price']} ден (was {p['old_price']}) - tap to open", p["url"])
    if len(new_items) > 10:
        ntfy(f"+{len(new_items) - 10} more new deals", "Tap to open the outlet sorted by biggest discount",
             f"{BASE_URL}?sort={SORT}&limit={PER_PAGE}")
    discord("**" + title + "**\n" + "\n".join(f"{l}\n<{p['url']}>" for p, l in zip(new_items, lines)))
    for p, line in zip(new_items, lines):
        log("NEW: " + line + "  " + p["url"])
# ------------------------------------------------


def check(notify_existing=False):
    first_run = not STATE_FILE.exists()
    seen = {}
    if not first_run:
        try:
            seen = json.loads(STATE_FILE.read_text("utf-8"))
        except ValueError:  # file damaged (e.g. PC crashed mid-write): start a fresh baseline
            log("state file damaged, rebuilding baseline")
            first_run = True
    current = scan()
    new = [p for pid, p in current.items() if pid not in seen]
    log(f"checked: {len(current)} products at -{MIN_DISCOUNT}%+, {len(new)} new")
    if new and (not first_run or notify_existing):
        notify(new)
    elif first_run:
        log("first run: saved current deals as baseline (no notification)")
    # remember everything we've ever seen so items don't re-ping if they flicker
    for pid, p in current.items():
        seen.setdefault(pid, {"name": p["name"], "discount": p["discount"],
                              "first_seen": time.strftime("%Y-%m-%d %H:%M")})
    tmp = STATE_FILE.with_suffix(".tmp")  # write-then-swap so a crash can't corrupt it
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(STATE_FILE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, metavar="MINUTES", help="keep running, check every N minutes")
    ap.add_argument("--notify-existing", action="store_true", help="on first run, ping for deals already up")
    ap.add_argument("--test", action="store_true", help="send a test notification and exit")
    a = ap.parse_args()
    if a.test:
        notify([{"discount": 80, "brand": "TEST", "name": "Test product", "price": "999",
                 "old_price": "4.995", "url": BASE_URL}])
        return
    while True:
        try:
            check(a.notify_existing)
        except Exception as e:
            log(f"error: {e}")
            if not a.loop:
                sys.exit(1)
        if not a.loop:
            break
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
