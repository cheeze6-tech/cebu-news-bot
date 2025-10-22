import os, time, json, re, sys, html
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import praw
from difflib import SequenceMatcher

SUBREDDIT = "CebuPolitics"

# Sources
SUNSTAR_LOCAL = "https://www.sunstar.com.ph/cebu/local-news"
CDN_LATEST   = "https://cebudailynews.inquirer.net/category/latest-news"

FLAIR_TEXT = "Local News"
FLAIR_ID = None
STATE_FILE = Path("posted.json")

REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", f"CebuNewsBot/1.0 by u/{REDDIT_USERNAME or 'bot'}")

# ---------- Helpers ----------

def load_state():
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"[WARN] Could not load posted.json: {e}")
    return set()

def save_state(ids):
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
    return set([w for w in norm_title(s).split() if w and len(w) > 2])

def near_duplicate(a: str, b: str) -> bool:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return False
    # token overlap (Jaccard)
    ta, tb = token_set(a), token_set(b)
    if ta and tb:
        jacc = len(ta & tb) / max(1, len(ta | tb))
        if jacc >= 0.75:
            return True
    # fuzzy ratio
    ratio = SequenceMatcher(None, na, nb).ratio()
    return ratio >= 0.82

def dedupe_by_similarity(items):
    kept = []
    for it in items:
        if not any(near_duplicate(it["title"], k["title"]) for k in kept):
            kept.append(it)
    return kept

def fetch(url):
    headers = {"User-Agent": REDDIT_USER_AGENT}
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text

# ---------- Scrapers ----------

def scrape_sunstar_local():
    print(f"[SUNSTAR] GET {SUNSTAR_LOCAL}")
    html_text = fetch(SUNSTAR_LOCAL)
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for h in soup.select("h2 a, h3 a"):
        href = h.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.sunstar.com.ph" + href
        if "sunstar.com.ph/cebu" in href:
            title = h.get_text(strip=True)
            if title and len(title) > 6:
                out.append({"title": title, "url": href, "source": "SunStar Cebu"})
    # de-dupe by URL
    seen = set()
    uniq = []
    for it in out:
        if it["url"] not in seen:
            uniq.append(it); seen.add(it["url"])
    print(f"[SUNSTAR] Found {len(uniq)}")
    return uniq

def scrape_cdn_latest():
    print(f"[CDN] GET {CDN_LATEST}")
    html_text = fetch(CDN_LATEST)
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = "https://cebudailynews.inquirer.net" + href
        if "cebudailynews.inquirer.net" in href and any(seg in href for seg in ["/202", "/latest-news/","/news/","/cebu"]):
            title = a.get_text(strip=True)
            if title and len(title) > 6 and not title.lower().startswith(("read more","watch","listen")):
                out.append({"title": title, "url": href, "source": "CDN Digital"})
    # de-dupe by URL
    seen = set(); uniq = []
    for it in out:
        if it["url"] not in seen:
            uniq.append(it); seen.add(it["url"])
    print(f"[CDN] Found {len(uniq)}")
    return uniq

# ---------- Main ----------

def main():
    print("=== Cebu News Bot start ===")
    print(f"Target subreddit: r/{SUBREDDIT}")

    # Gather from both sources
    items = []
    try: items += scrape_sunstar_local()
    except Exception as e: print(f"[SUNSTAR][ERR] {e}")
    try: items += scrape_cdn_latest()
    except Exception as e: print(f"[CDN][ERR] {e}")

    if not items:
        print("No items found from either source this run.")
        return

    print(f"[AGG] Total before de-dupe: {len(items)}")
    items = dedupe_by_similarity(items)
    print(f"[AGG] After cross-source de-dupe: {len(items)}")

    # Reddit auth
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

    seen_urls = load_state()
    print(f"[STATE] Seen URLs: {len(seen_urls)}")

    to_post = [it for it in items if it["url"] not in seen_urls]
    if not to_post:
        print("No new items to post (already seen).")
        return

    posted = 0
    for it in to_post:
        post_title = it["title"][:290]
        try:
            submission = sub.submit(title=post_title, url=it["url"], resubmit=False)
            if FLAIR_ID:
                submission.flair.select(FLAIR_ID)
            print(f"[POSTED] {submission.shortlink} — {post_title} [{it['source']}]")
            seen_urls.add(it["url"])
            posted += 1
            time.sleep(8)
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
