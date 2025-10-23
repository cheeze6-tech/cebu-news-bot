#!/usr/bin/env python3
"""
CebuPolitics News Bot (SunStar + CDN only)
- Cross-site de-dupe
- Auto-apply flair "Local News"
- Skip stories older than 24 hours
- Per-run cap on posts
"""

import os
import time
import json
import re
import sys
import html
import random
from pathlib import Path
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
import praw

# ----------------- Config -----------------

SUBREDDIT = "CebuPolitics"

# Reliable endpoints
SUNSTAR_TOP = "https://www.sunstar.com.ph/collection/cebu-top-stories"
CDN_NEWSINFO = "https://newsinfo.inquirer.net/category/cdn/cdn-news"

# Flair (plain text – avoid emoji in Automod YAML)
FLAIR_TEXT = "Local News"
FLAIR_ID = None  # discovered at runtime if available

STATE_FILE = Path("posted.json")

# Freshness + cap
MAX_AGE_HOURS = 24     # skip stories older than this
MAX_POSTS_PER_RUN = 5  # per-run cap

# Reddit creds via env / GitHub secrets
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD")
REDDIT_USER_AGENT    = os.getenv(
    "REDDIT_USER_AGENT",
    f"CebuNewsBot/1.0 by u/{REDDIT_USERNAME or 'bot'}"
)

# ----------------- Helpers -----------------

def load_state() -> set:
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"[WARN] Could not load posted.json: {e}")
    return set()

def save_state(ids: set) -> None:
    try:
        STATE_FILE.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] Could not save posted.json: {e}")

def norm_title(t: str) -> str:
    t = html.unescape(t or "")
    t = t.lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)  # strip punctuation
    return t.strip()

def token_set(s: str) -> set:
    return {w for w in norm_title(s).split() if w and len(w) > 2}

def near_duplicate(a: str, b: str) -> bool:
    """Return True if titles are likely the same story."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return False
    # token overlap (Jaccard)
    ta, tb = token_set(a), token_set(b)
    if ta and tb:
        jacc = len(ta & tb) / max(1, len(ta | tb))
        if jacc >= 0.75:
            return True
    # fuzzy ratio (fallback)
    ratio = SequenceMatcher(None, na, nb).ratio()
    return ratio >= 0.82

def dedupe_by_similarity(items: list) -> list:
    """items: list of dicts with keys: title, url, source. Keep first occurrence."""
    kept = []
    for it in items:
        if not any(near_duplicate(it["title"], k["title"]) for k in kept):
            kept.append(it)
    return kept

def parse_any_datetime(s: str):
    """Parse ISO or RFC-like datetimes to aware UTC datetime if possible."""
    if not s:
        return None
    s = s.strip()
    try:
        # ISO 8601 (with 'Z' or offset)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        pass
    try:
        # RFC 2822 (e.g., "Tue, 22 Oct 2025 09:15:00 +0800")
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
    except Exception:
        pass
    return None

def infer_date_from_url(url: str):
    """Try to infer date from url patterns like /YYYY/MM/DD/ or -YYYY-MM-DD-"""
    m = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", url)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except Exception:
            pass
    m = re.search(r"-(20\d{2})-(\d{1,2})-(\d{1,2})(?:-|/|$)", url)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except Exception:
            pass
    return None

def fetch(url: str) -> str:
    """GET with robust headers + light retry for 403/429/5xx."""
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    ]
    headers = {
        "User-Agent": random.choice(uas),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Cache-Control": "no-cache",
    }
    last_exc = None
    for attempt in range(3):  # a couple of retries w/ UA rotation
        try:
            r = requests.get(url, headers=headers, timeout=25)
            if r.status_code in (403, 429) and attempt < 2:
                headers["User-Agent"] = uas[(uas.index(headers["User-Agent"]) + 1) % len(uas)]
                time.sleep(1.2)
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_exc = e
            time.sleep(0.8)
    raise last_exc

def extract_published_datetime(article_url: str, source_name: str):
    """Fetch article page and try common meta tags for published time; fallback to URL date."""
    try:
        html_text = fetch(article_url)
        soup = BeautifulSoup(html_text, "html.parser")

        # Common meta/time patterns across sites
        meta_selectors = [
            ('meta[property="article:published_time"]', "content"),
            ('meta[name="article:published_time"]', "content"),
            ('meta[property="og:pubdate"]', "content"),
            ('meta[name="pubdate"]', "content"),
            ('meta[itemprop="datePublished"]', "content"),
            ('time[datetime]', "datetime"),
            ('span[itemprop="datePublished"]', "content"),
        ]
        for sel, attr in meta_selectors:
            tag = soup.select_one(sel)
            if tag and tag.get(attr):
                dt = parse_any_datetime(tag.get(attr))
                if dt:
                    return dt

        # Try visible time tag text if datetime attr is missing
        ttag = soup.find("time")
        if ttag and ttag.get_text(strip=True):
            dt = parse_any_datetime(ttag.get_text(strip=True))
            if dt:
                return dt

    except Exception as e:
        print(f"[DATE][{source_name}] meta fetch failed for {article_url}: {e}")

    # Fallback: try to infer from URL (works if date embedded)
    dt = infer_date_from_url(article_url)
    if dt:
        return dt

    return None

def is_fresh(article_url: str, source_name: str, max_age_hours: int) -> bool:
    """Return True if article appears to be within max_age_hours."""
    dt = extract_published_datetime(article_url, source_name)
    if not dt:
        # If date unknown, be conservative: treat as fresh to avoid missing posts.
        # (You may flip this default to False if you prefer stricter behavior.)
        print(f"[DATE][{source_name}] No date found; treating as fresh -> {article_url}")
        return True
    age = datetime.now(timezone.utc) - dt
    ok = age <= timedelta(hours=max_age_hours)
    print(f"[DATE][{source_name}] {article_url} -> {dt.isoformat()} (age {age}) fresh={ok}")
    return ok

# ----------------- Scrapers -----------------

def scrape_sunstar_local() -> list:
    """Scrape SunStar Cebu Top Stories collection page for article links."""
    print(f"[SUNSTAR] GET {SUNSTAR_TOP}")
    html_text = fetch(SUNSTAR_TOP)
    soup = BeautifulSoup(html_text, "html.parser")
    out = []

    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.sunstar.com.ph" + href
        if "sunstar.com.ph/cebu" not in href:
            continue
        title = a.get_text(strip=True)
        if title and len(title) > 6:
            out.append({"title": title, "url": href, "source": "SunStar Cebu"})

    # de-dupe by URL
    seen = set(); uniq = []
    for it in out:
        if it["url"] not in seen:
            uniq.append(it); seen.add(it["url"])

    print(f"[SUNSTAR] Found {len(uniq)}")
    return uniq

def scrape_cdn_latest() -> list:
    """Scrape Inquirer Newsinfo CDN index for recent CDN stories."""
    print(f"[CDN] GET {CDN_NEWSINFO}")
    html_text = fetch(CDN_NEWSINFO)
    soup = BeautifulSoup(html_text, "html.parser")
    out = []

    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://newsinfo.inquirer.net" + href

        if ("cebudailynews.inquirer.net" in href) or ("/cdn/" in href or "-cdn-" in href):
            title = a.get_text(strip=True)
            if title and len(title) > 6 and not title.lower().startswith(("read more", "watch", "listen")):
                out.append({"title": title, "url": href, "source": "CDN Digital (via Inquirer)"})

    # de-dupe by URL
    seen = set(); uniq = []
    for it in out:
        if it["url"] not in seen:
            uniq.append(it); seen.add(it["url"])

    print(f"[CDN] Found {len(uniq)}")
    return uniq

# ----------------- Main -----------------

def main():
    print("=== Cebu News Bot start ===")
    print(f"Target subreddit: r/{SUBREDDIT}")

    # 1) Gather from both sources (order sets preference for near-dupes)
    items = []
    try:
        items += scrape_sunstar_local()
    except Exception as e:
        print(f"[SUNSTAR][ERR] {e}")
    try:
        items += scrape_cdn_latest()
    except Exception as e:
        print(f"[CDN][ERR] {e}")

    if not items:
        print("No items found from either source this run.")
        return

    print(f"[AGG] Total before de-dupe: {len(items)}")
    items = dedupe_by_similarity(items)
    print(f"[AGG] After cross-source de-dupe: {len(items)}")

    # 2) Reddit auth
    try:
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            username=REDDIT_USERNAME,
            password=REDDIT_PASSWORD,
            user_agent=REDDIT_USER_AGENT,
        )
        me = reddit.user.me()
        print(f"[AUTH] Reddit OK as u/{me}")
    except Exception as e:
        print("[ERROR] Reddit auth failed:", e)
        sys.exit(1)

    sub = reddit.subreddit(SUBREDDIT)

    # 3) Flair discovery (optional)
    global FLAIR_ID
    if FLAIR_TEXT and not FLAIR_ID:
        try:
            for flair in sub.flair.link_templates.user_selectable():
                if flair.get("text") == FLAIR_TEXT:
                    FLAIR_ID = flair.get("id")
                    print(f"[FLAIR] id for '{FLAIR_TEXT}': {FLAIR_ID}")
                    break
        except Exception as e:
            print(f"[WARN] Could not fetch link flairs: {e}")

    # 4) State & freshness + cap
    seen_urls = load_state()
    print(f"[STATE] Seen URLs: {len(seen_urls)}")

    # Only unseen + fresh
    filtered = []
    for it in items:
        if it["url"] in seen_urls:
            continue
        if is_fresh(it["url"], it["source"], MAX_AGE_HOURS):
            filtered.append(it)

    if not filtered:
        print("No fresh unseen items to post.")
        return

    # Cap per run
    to_post = filtered[:MAX_POSTS_PER_RUN]
    print(f"[POST] Will post {len(to_post)} item(s) (cap {MAX_POSTS_PER_RUN})")

    posted = 0
    for it in to_post:
        post_title = it["title"][:290]  # raw headline only
        try:
            submission = sub.submit(title=post_title, url=it["url"], resubmit=False)
            if FLAIR_ID:
                submission.flair.select(FLAIR_ID)
            print(f"[POSTED] {submission.shortlink} — {post_title} [{it['source']}]")
            seen_urls.add(it["url"])
            posted += 1
            time.sleep(8)  # be polite to API
        except Exception as ex:
            print(f"[ERROR] Failed to post '{post_title}': {ex}")

    if posted:
        save_state(seen_urls)
        print(f"[DONE] Posted {posted} new item(s).")
    else:
        print("[DONE] Nothing posted.")

    print("=== Cebu News Bot end ===")

if __name__ == "__main__":
    main()
