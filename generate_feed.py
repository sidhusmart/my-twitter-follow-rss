#!/usr/bin/env python3
"""
Fetch recent tweets from followed accounts via X API
and generate/update an RSS feed with embedded tweet cards and images.

Config is read from environment variables, falling back to .env for local dev.
Designed to run as a GitHub Actions cron job.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone


MEDIA_NS = "http://search.yahoo.com/mrss/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
FEED_PATH = "feed.xml"
LAST_SEEN_ID_PATH = "last_seen_id.txt"
MAX_ITEMS = 500


def get_config():
    """Read config from env vars, falling back to .env file for local dev."""
    bearer = os.environ.get("X_BEARER_TOKEN")
    accounts = os.environ.get("X_FOLLOW_ACCOUNTS")

    if bearer and accounts:
        return bearer, accounts

    # Fallback: load from .env file
    env_path = os.path.join(os.path.dirname(__file__) or ".", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "X_BEARER_TOKEN" and not bearer:
                    bearer = value
                elif key == "X_FOLLOW_ACCOUNTS" and not accounts:
                    accounts = value

    if not bearer:
        print("ERROR: X_BEARER_TOKEN not set")
        sys.exit(1)
    if not accounts:
        print("ERROR: X_FOLLOW_ACCOUNTS not set")
        sys.exit(1)

    return bearer, accounts


def read_last_seen_id():
    """Read the last seen tweet ID from file, or return None."""
    if os.path.exists(LAST_SEEN_ID_PATH):
        with open(LAST_SEEN_ID_PATH) as f:
            text = f.read().strip()
            if text:
                return text
    return None


def write_last_seen_id(tweet_id):
    """Write the latest tweet ID to file."""
    with open(LAST_SEEN_ID_PATH, "w") as f:
        f.write(tweet_id)


def search_recent_tweets(bearer_token, usernames, max_results=20, since_id=None):
    """Call X API v2 recent search for tweets from the given usernames."""
    query = "(" + " OR ".join(f"from:{u}" for u in usernames) + ") -is:retweet"
    params = {
        "query": query,
        "max_results": max_results,
        "tweet.fields": "created_at,author_id,text,note_tweet,attachments,referenced_tweets",
        "expansions": "author_id,attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id",
        "user.fields": "username,name",
        "media.fields": "url,preview_image_url,type",
    }
    if since_id:
        params["since_id"] = since_id

    url = f"https://api.x.com/2/tweets/search/recent?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {bearer_token}")

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
        print(f"API response status: {resp.status}")
        return data


def escape_xml(text):
    """Escape text for use inside XML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_description_html(tweet, username, name, tweet_id, created_at_str,
                           media_urls, ref_tweets, users):
    """Build rich HTML for the RSS item description."""
    note = tweet.get("note_tweet", {})
    tweet_text = note.get("text", tweet["text"]) if note else tweet["text"]
    html_text = escape_xml(tweet_text).replace("\n", "<br>")
    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

    parts = []

    parts.append(f'<p>{html_text}</p>')

    parts.append(
        f'<blockquote class="twitter-tweet" data-width="550">'
        f'<p lang="en" dir="ltr">{html_text}</p>'
        f'&mdash; {escape_xml(name)} (@{escape_xml(username)}) '
        f'<a href="{tweet_url}">{created_at_str}</a>'
        f'</blockquote>'
        f'<script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>'
    )

    for ref in tweet.get("referenced_tweets", []):
        ref_type = ref.get("type", "")
        ref_id = ref["id"]
        if ref_id in ref_tweets:
            rt = ref_tweets[ref_id]
            rt_author_id = rt.get("author_id", "")
            rt_user = users.get(rt_author_id, {})
            rt_username = rt_user.get("username", "unknown")
            rt_name = rt_user.get("name", rt_username)
            rt_text = escape_xml(rt.get("text", "")).replace("\n", "<br>")
            rt_url = f"https://twitter.com/{rt_username}/status/{ref_id}"

            if ref_type == "replied_to":
                label = "Replying to"
            elif ref_type == "quoted":
                label = "Quoting"
            elif ref_type == "retweeted":
                label = "Retweeted"
            else:
                label = "Referenced"

            parts.append(
                f'<p style="color:#666; font-size:0.9em;">{label}:</p>'
                f'<blockquote style="border-left:3px solid #ccc; padding-left:12px; margin-left:0;">'
                f'<p><strong>{escape_xml(rt_name)}</strong> '
                f'<a href="{rt_url}" style="color:#1da1f2;">@{escape_xml(rt_username)}</a></p>'
                f'<p>{rt_text}</p>'
                f'</blockquote>'
            )

    return "".join(parts)


def build_items_xml(tweets_data):
    """Build a list of RSS <item> XML strings from API response data."""
    users = {}
    if "includes" in tweets_data and "users" in tweets_data["includes"]:
        for u in tweets_data["includes"]["users"]:
            users[u["id"]] = {"username": u["username"], "name": u["name"]}

    media = {}
    if "includes" in tweets_data and "media" in tweets_data["includes"]:
        for m in tweets_data["includes"]["media"]:
            url = m.get("url") or m.get("preview_image_url")
            if url:
                media[m["media_key"]] = {"url": url, "type": m.get("type", "photo")}

    ref_tweets = {}
    if "includes" in tweets_data and "tweets" in tweets_data["includes"]:
        for t in tweets_data["includes"]["tweets"]:
            ref_tweets[t["id"]] = t

    tweets = tweets_data.get("data", [])
    items = []

    for tweet in tweets:
        author_id = tweet.get("author_id", "")
        user = users.get(author_id, {})
        username = user.get("username", "unknown")
        name = user.get("name", username)
        tweet_url = f"https://x.com/{username}/status/{tweet['id']}"

        pub_date = ""
        created_at_display = ""
        if "created_at" in tweet:
            dt = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            created_at_display = dt.strftime("%B %d, %Y")

        media_keys = tweet.get("attachments", {}).get("media_keys", [])
        media_urls = []
        for mk in media_keys:
            if mk in media:
                media_urls.append(media[mk]["url"])

        description_html = build_description_html(
            tweet, username, name, tweet["id"], created_at_display,
            media_urls, ref_tweets, users
        )

        lines = []
        lines.append("    <item>")
        lines.append(f"      <title><![CDATA[{name} (@{username})]]></title>")
        lines.append(f"      <description><![CDATA[{description_html}]]></description>")
        lines.append(f"      <content:encoded><![CDATA[{description_html}]]></content:encoded>")
        lines.append(f"      <link>{tweet_url}</link>")
        lines.append(f"      <guid>{tweet['id']}</guid>")
        if pub_date:
            lines.append(f"      <pubDate>{pub_date}</pubDate>")

        for mk in media_keys:
            if mk in media:
                m = media[mk]
                medium = "video" if m["type"] == "video" else "image"
                lines.append(
                    f'      <media:content medium="{medium}" url="{escape_xml(m["url"])}" />'
                )

        lines.append("    </item>")
        items.append("\n".join(lines))

    return items


def parse_existing_items(feed_path):
    """Parse existing feed.xml and return list of (guid, item_xml) tuples."""
    if not os.path.exists(feed_path):
        return []

    with open(feed_path, encoding="utf-8") as f:
        content = f.read()

    items = []
    for match in re.finditer(r"(    <item>.*?    </item>)", content, re.DOTALL):
        item_xml = match.group(1)
        guid_match = re.search(r"<guid>(.*?)</guid>", item_xml)
        guid = guid_match.group(1) if guid_match else None
        items.append((guid, item_xml))

    return items


def write_feed(items_xml, feed_path=FEED_PATH):
    """Write the complete RSS feed with the given item XML strings."""
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<rss version="2.0" xmlns:media="{MEDIA_NS}" xmlns:content="{CONTENT_NS}">',
        "  <channel>",
        "    <title>Twitter Feed</title>",
        "    <description>Tweets from followed accounts</description>",
        "    <link>https://x.com</link>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
    ]
    for item in items_xml:
        lines.append(item)
    lines.append("  </channel>")
    lines.append("</rss>")

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    bearer_token, accounts_raw = get_config()
    usernames = [a.strip().lstrip("@") for a in accounts_raw.split(",")]

    since_id = read_last_seen_id()
    if since_id:
        print(f"Fetching tweets since ID: {since_id}")
    else:
        print("No last seen ID found, fetching latest tweets")

    print(f"Searching tweets from: {usernames}")

    try:
        data = search_recent_tweets(bearer_token, usernames, since_id=since_id)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("Rate limited (429). Skipping this run to preserve existing feed.")
            sys.exit(0)
        print(f"API error {e.code}: {e.read().decode()}")
        sys.exit(1)

    # Opt-in debug dump
    if os.environ.get("DEBUG"):
        with open("api_response.json", "w") as f:
            json.dump(data, f, indent=2)
        print("Raw API response saved to api_response.json")

    tweets = data.get("data", [])
    if not tweets:
        print("No new tweets found.")
        return

    # Update last seen ID (first tweet in response is the newest)
    newest_id = tweets[0]["id"]
    write_last_seen_id(newest_id)
    print(f"Updated last seen ID to: {newest_id}")

    # Build new items
    new_items = build_items_xml(data)
    new_guids = set()
    for item in new_items:
        guid_match = re.search(r"<guid>(.*?)</guid>", item)
        if guid_match:
            new_guids.add(guid_match.group(1))

    # Load existing items and deduplicate
    existing = parse_existing_items(FEED_PATH)
    existing_items = [xml for guid, xml in existing if guid not in new_guids]

    # Merge: new items first, then existing, capped at MAX_ITEMS
    all_items = new_items + existing_items
    all_items = all_items[:MAX_ITEMS]

    write_feed(all_items)
    print(f"Feed updated: {len(new_items)} new + {len(existing_items)} existing = {len(all_items)} total items")


if __name__ == "__main__":
    main()
