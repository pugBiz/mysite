#!/usr/bin/env python3
"""
debug_watcher.py

Debug variant of watcher.py:
- Reads config.json (repo root) and optional WATCH_TERM env var.
- Supports --debug to print detailed diagnostics to stdout.
- Quotes multiword terms for exact-phrase search.
- Uses Reddit public JSON endpoints (requests).
- Prints counts of raw posts and filtered posts and a sample post for troubleshooting.
- Writes matches.json and state.json as before.

Usage:
  python debug_watcher.py            # normal run
  python debug_watcher.py --debug    # verbose debug output
  WATCH_TERM="your term" python debug_watcher.py --debug

Notes:
- For reliable, production use consider switching to OAuth/PRAW and storing credentials as secrets.
- This script is intended for local debugging and for running in Actions to capture more logs.
"""

import os
import json
import time
import argparse
import requests
from datetime import datetime, timezone

ROOT = os.getcwd()
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")
MATCHES_PATH = os.path.join(ROOT, "matches.json")

DEFAULT_CONFIG = {
    "term": "bake croissants",
    "global_search": True,
    "subreddits": ["baking", "AskCulinary", "breadit"],
    "min_score": 1,
    "max_age_hours": 48,
    "results_limit_per_query": 25,
    "user_agent": "CroissantWatcher/1.0 (by pugBiz)"
}

def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: failed to parse JSON {path}: {e}")
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_config():
    cfg = load_json(CONFIG_PATH, {})
    merged = DEFAULT_CONFIG.copy()
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged

def load_state():
    s = load_json(STATE_PATH, {})
    if not isinstance(s, dict):
        s = {}
    s.setdefault("seen_ids", [])
    return s

def now_ts():
    return int(time.time())

def iso8601(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def quote_if_needed(term):
    # If term contains whitespace, quote it for exact-phrase search
    if not term:
        return term
    if " " in term.strip():
        # If already quoted, leave as-is
        t = term.strip()
        if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
            return t
        return f'"{t}"'
    return term

def reddit_search_sitewide(query, limit, user_agent, after=None, debug=False):
    url = "https://www.reddit.com/search.json"
    params = {"q": query, "sort": "new", "limit": limit}
    if after:
        params["after"] = after
    headers = {"User-Agent": user_agent}
    if debug:
        print(f"[debug] GET {url} params={params}")
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def reddit_search_subreddit(subreddit, query, limit, user_agent, debug=False):
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {"q": query, "sort": "new", "limit": limit, "restrict_sr": 1}
    headers = {"User-Agent": user_agent}
    if debug:
        print(f"[debug] GET {url} params={params}")
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def extract_posts_from_listing(listing_json):
    posts = []
    children = listing_json.get("data", {}).get("children", [])
    for c in children:
        d = c.get("data", {})
        posts.append({
            "id": d.get("id"),
            "fullname": d.get("name"),  # t3_<id>
            "title": d.get("title"),
            "url": "https://www.reddit.com" + d.get("permalink", "") if d.get("permalink") else d.get("url"),
            "excerpt": d.get("selftext") or "",
            "subreddit": d.get("subreddit"),
            "author": d.get("author"),
            "score": d.get("score", 0),
            "created_utc": d.get("created_utc"),
            "type": "post"
        })
    return posts

def filter_posts(posts, term_lower, min_score, max_age_hours):
    out = []
    cutoff_ts = now_ts() - int(max_age_hours * 3600)
    for p in posts:
        if not p.get("id"):
            continue
        if p.get("score", 0) < min_score:
            continue
        created = int(p.get("created_utc") or 0)
        if created and created < cutoff_ts:
            continue
        title = (p.get("title") or "").lower()
        excerpt = (p.get("excerpt") or "").lower()
        subreddit = (p.get("subreddit") or "").lower()
        if term_lower in title or term_lower in excerpt or term_lower in subreddit:
            p["found_at"] = iso8601(now_ts())
            out.append(p)
    return out

def main(debug=False):
    cfg = load_config()
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    # Priority: env WATCH_TERM -> config term -> default
    watch_term = os.getenv("WATCH_TERM") or cfg.get("term") or DEFAULT_CONFIG["term"]
    if isinstance(watch_term, str):
        watch_term = watch_term.strip()
    else:
        watch_term = str(watch_term or DEFAULT_CONFIG["term"])

    # Prepare query (quote multiword terms)
    query = quote_if_needed(watch_term)

    user_agent = cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]
    limit = int(cfg.get("results_limit_per_query", 25) or 25)
    min_score = int(cfg.get("min_score", 1) or 0)
    max_age_hours = int(cfg.get("max_age_hours", 48) or 48)

    if debug:
        print("=== Debug Watcher Start ===")
        print("Config:", json.dumps(cfg, indent=2))
        print("Effective watch_term:", repr(watch_term))
        print("Query used for Reddit:", repr(query))
        print("min_score:", min_score, "max_age_hours:", max_age_hours, "limit:", limit)
        print("Seen IDs count:", len(seen_ids))

    matches = []
    try:
        if cfg.get("global_search"):
            if debug:
                print("[debug] Performing site-wide search")
            listing = reddit_search_sitewide(query, limit, user_agent, debug=debug)
            posts = extract_posts_from_listing(listing)
            if debug:
                print(f"[debug] site-wide raw posts: {len(posts)}")
            filtered = filter_posts(posts, watch_term.lower(), min_score, max_age_hours)
            if debug:
                print(f"[debug] site-wide filtered posts: {len(filtered)}")
            matches.extend(filtered)
        else:
            subs = cfg.get("subreddits") or []
            if debug:
                print("[debug] Performing per-subreddit search on:", subs)
            for sub in subs:
                try:
                    listing = reddit_search_subreddit(sub, query, limit, user_agent, debug=debug)
                    posts = extract_posts_from_listing(listing)
                    if debug:
                        print(f"[debug] /r/{sub} raw posts: {len(posts)}")
                    filtered = filter_posts(posts, watch_term.lower(), min_score, max_age_hours)
                    if debug:
                        print(f"[debug] /r/{sub} filtered posts: {len(filtered)}")
                    matches.extend(filtered)
                except requests.HTTPError as e:
                    print(f"Warning: subreddit {sub} search failed: {e}")
    except requests.HTTPError as e:
        print("HTTP error during Reddit search:", e)
    except Exception as e:
        print("Unexpected error during Reddit search:", e)

    # Debug: show sample raw match info if any raw matches were found before dedupe
    if debug:
        print(f"[debug] Total matches before dedupe: {len(matches)}")
        if matches:
            sample = matches[0]
            print("[debug] Sample match (pre-dedupe):", sample.get("id"), sample.get("title")[:120])

    # Deduplicate by id and exclude already seen
    new_matches = []
    for m in matches:
        mid = m.get("id")
        if not mid:
            continue
        if mid in seen_ids:
            if debug:
                print(f"[debug] Skipping seen id: {mid}")
            continue
        new_matches.append(m)
        seen_ids.add(mid)

    if debug:
        print(f"[debug] New matches after dedupe: {len(new_matches)}")

    # Load existing matches.json to preserve history
    existing = load_json(MATCHES_PATH, {"matches": []})
    existing_matches = existing.get("matches", []) if isinstance(existing, dict) else []

    combined = new_matches + existing_matches
    MAX_KEEP = 200
    combined = combined[:MAX_KEEP]

    out = {"matches": combined, "generated_at": iso8601(now_ts()), "watch_term": watch_term}
    try:
        save_json(MATCHES_PATH, out)
        if debug:
            print(f"[debug] Wrote matches.json with {len(combined)} total matches (new: {len(new_matches)})")
    except Exception as e:
        print("Error writing matches.json:", e)

    # Update state
    state["seen_ids"] = list(seen_ids)[-1000:]
    state["last_run"] = iso8601(now_ts())
    state["watch_term"] = watch_term
    try:
        save_json(STATE_PATH, state)
        if debug:
            print(f"[debug] Updated state.json (seen_ids: {len(state['seen_ids'])})")
    except Exception as e:
        print("Error writing state.json:", e)

    print(f"Wrote {len(new_matches)} new matches, total stored: {len(combined)}")
    if debug:
        print("=== Debug Watcher End ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Debug watcher for Reddit matches")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
    args = parser.parse_args()
    main(debug=args.debug)
