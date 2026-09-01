# watcher.py
import feedparser
import json
import time
from pathlib import Path
from datetime import datetime, timezone
import re

# Config
SUBREDDITS = ["baking", "AskCulinary", "breadit"]  # add the subreddits you want
KEYWORD = "bake croissants"
STATE_FILE = Path("state.json")
OUTPUT_FILE = Path("matches.json")
MAX_MATCHES = 50

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state))

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def normalize_id(link):
    # create a stable id from link
    return re.sub(r'[^a-zA-Z0-9]', '_', link)

def check_feed(subreddit, keyword, seen_ids):
    url = f"https://www.reddit.com/r/{subreddit}/new/.rss"
    feed = feedparser.parse(url)
    matches = []
    for entry in feed.entries:
        link = entry.get('link') or entry.get('id') or ''
        entry_id = normalize_id(link)
        title = entry.get('title', '')
        summary = entry.get('summary', '')
        content = (title + " " + summary).lower()
        if keyword.lower() in content and entry_id not in seen_ids:
            matches.append({
                "id": entry_id,
                "title": title,
                "excerpt": summary,
                "url": link,
                "subreddit": subreddit,
                "author": entry.get('author', ''),
                "type": "post",
                "found_at": now_iso()
            })
            seen_ids.add(entry_id)
    return matches

def main():
    state = load_state()
    seen = set(state.get("seen_ids", []))
    all_matches = []

    for sub in SUBREDDITS:
        try:
            matches = check_feed(sub, KEYWORD, seen)
            all_matches.extend(matches)
        except Exception as e:
            print(f"Error checking {sub}: {e}")

    # Keep most recent matches
    all_matches = sorted(all_matches, key=lambda x: x.get("found_at"), reverse=True)[:MAX_MATCHES]

    # Write output file
    OUTPUT_FILE.write_text(json.dumps({"matches": all_matches}, indent=2))

    # Persist seen ids (keep last 200)
    state["seen_ids"] = list(sorted(seen, reverse=True))[:200]
    save_state(state)
    print(f"Wrote {len(all_matches)} matches to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
