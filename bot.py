import os, time, json, re, sys
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import feedparser
import praw

SUBREDDIT = "CebuPolitics"

# Sources
FREEMAN_RSS = "https://www.philstar.com/rss/the-freeman"
CEBU_NEWS_URL = "https://www.philstar.com/the-freeman/cebu-news"

# Title + (optional) flair
TITLE_PREFIX = "📰 [The Freeman] Cebu News — "
FLAIR_TEXT = "Cebu News"
FLAIR_ID = None  # will be discovered if exists

STATE_FILE = Path("posted.json")
LINK_FILTER = re.compile(r"philstar\.com/the-freeman/cebu-news/", re.IGNORECASE)

# Reddit creds
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", f"CebuNewsBot/1.0 by u/{REDDIT_USERNAME or 'bot'}")

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

def scrape_cebu_news():
    """Scrape the Cebu News page for article links under the correct path."""
    print(f"[SCRAPE] GET {CEBU_NEWS_URL}")
    headers = {"User-Agent": REDDIT_USER_AGENT}
    r = requests.get(CEBU_NEWS_URL, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # normalize to absolute
        if href.startswith("/"):
            href = "https://www.philstar.com" + href
        if LINK_FILTER.search(href):
            title = a.get_text(strip=True)
            if title and len(title) > 6:  # avoid tiny/blank anchors
                links.append((title, href))
    # dedupe while preserving order
    seen = set()
    uniq = []
    for t, u in links:
        if u not in seen:
            seen.add(u)
            uniq.append((t, u))
    print(f"[SCRAPE] Found {len(uniq)} candidate links")
    return uniq

def fetch_rss_cebu_news():
    """Fallback: read The Freeman RSS and filter to Cebu News path."""
    print(f"[RSS] Parse {FREEMAN_RSS}")
    feed = feedparser.parse(FREEMAN_RSS)
    entries = getattr(feed, "entries", [])
    out = []
    for e in entries:
        link = getattr(e, "link", "")
        title = getattr(e, "title", "").strip()
        if LINK_FILTER.search(link):
            out.append((title, link))
    print(f"[RSS] Filtered Cebu News entries: {len(out)}")
    return out

def main():
    print("=== Cebu News Bot start ===")
    print(f"Target subreddit: r/{SUBREDDIT}")

    # 1) Collect candidates (scrape first, then RSS fallback)
    items = scrape_cebu_news()
    if not items:
        items = fetch_rss_cebu_news()

    if not items:
        print("No Cebu News items found via page or RSS this run.")
        return

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
    global FLAIR_ID
    if FLAIR_TEXT and not FLAIR_ID:
        try:
            for flair in sub.flair.link_templates.user_selectable():
                if flair.get("text") == FLAIR_TEXT:
                    FLAIR_ID = flair.get("id")
                    print(f"[FLAIR] Discovered id for '{FLAIR_TEXT}': {FLAIR_ID}")
                    break
        except Exception as e:
            print(f"[WARN] Could not fetch link flairs: {e}")

    # 3) De-dupe + post newest first
    seen = load_state()
    print(f"[STATE] Seen URLs: {len(seen)}")

    # Robust de-dupe preserving order by URL
    uniq = []
    seen_urls = set()
    for title, url in items:
        if url not in seen_urls:
            uniq.append((title, url))
            seen_urls.add(url)

    items_to_post = [(t, u) for (t, u) in uniq if u not in seen]

    if not items_to_post:
        print("No new items to post (already seen).")
        return

    posted = 0
    for title, url in items_to_post:
        post_title = (TITLE_PREFIX + title)[:290]
        try:
            submission = sub.submit(title=post_title, url=url, resubmit=False)
            if FLAIR_ID:
                submission.flair.select(FLAIR_ID)
            print(f"[POSTED] {submission.shortlink} — {post_title}")
            seen.add(url)  # key on URL for state
            posted += 1
            time.sleep(8)
        except Exception as ex:
            print(f"[ERROR] Failed to post '{post_title}': {ex}")

    if posted:
        save_state(seen)
        print(f"[DONE] Posted {posted} new item(s).")
    else:
        print("[DONE] Nothing posted.")

    print("=== Cebu News Bot end ===")

if __name__ == "__main__":
    main()
