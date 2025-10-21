import os, time, json, re
from pathlib import Path
import feedparser
import praw

# --- Config ---
SUBREDDIT = "CebuPolitics"
# Official Freeman RSS (all sections); we'll filter to Cebu News:
FREEMAN_RSS = "https://www.philstar.com/rss/the-freeman"

# Optional: Map a flair text -> flair_id to auto-assign (leave as None to skip)
FLAIR_TEXT = "Cebu News"
FLAIR_ID = None  # Put the flair_id string here once you know it (or leave None)

STATE_FILE = Path("posted.json")
LINK_FILTER = re.compile(r"https?://(www\.)?philstar\.com/the-freeman/cebu-news/")

# --- Auth from env ---
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", "CebuNewsBot/1.0 by u/" + (REDDIT_USERNAME or "cebu-news-bot"))

def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_state(ids):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)

def main():
    # init state
    seen = load_state()

    # parse feed
    feed = feedparser.parse(FREEMAN_RSS)
    entries = feed.entries if hasattr(feed, "entries") else []

    # filter to Cebu News items and newest-first
    filtered = [e for e in entries if LINK_FILTER.match(getattr(e, "link", ""))]
    filtered.sort(key=lambda e: getattr(e, "published_parsed", time.gmtime(0)), reverse=True)

    if not filtered:
        print("No Cebu News items found in feed this run.")
        return

    # reddit auth
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent=REDDIT_USER_AGENT,
    )
    sub = reddit.subreddit(SUBREDDIT)

    # (Optional) discover flair id by text once and hardcode it next time
    global FLAIR_ID
    if FLAIR_TEXT and not FLAIR_ID:
        try:
            for flair in sub.flair.link_templates.user_selectable():
                if flair.get("text") == FLAIR_TEXT:
                    FLAIR_ID = flair.get("id")
                    print(f"Discovered flair_id for '{FLAIR_TEXT}': {FLAIR_ID}")
                    break
        except Exception as e:
            print(f"Could not fetch link flairs: {e}")

    new_seen = set(seen)
    posts_made = 0

    for e in filtered:
        url = getattr(e, "link", "")
        guid = getattr(e, "id", url)  # fallback to URL if no GUID
        if guid in seen:
            continue

        title = getattr(e, "title", "Cebu News")
        # Keep title clean; Reddit title limit is ~300 chars
        title = title.strip()[:290]

        try:
            submission = sub.submit(title=title, url=url, resubmit=False)
            # flair if available
            if FLAIR_ID:
                submission.flair.select(FLAIR_ID)
            print(f"Posted: {submission.shortlink} -> {title}")
            new_seen.add(guid)
            posts_made += 1
            time.sleep(8)  # be polite to API
        except Exception as ex:
            print(f"Failed to post '{title}': {ex}")

    if posts_made:
        save_state(new_seen)

if __name__ == "__main__":
    main()
