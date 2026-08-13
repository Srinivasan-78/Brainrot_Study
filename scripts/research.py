#!/usr/bin/env python3
"""topic -> grounding text (Wikipedia REST API, no key needed)."""
import os
import sys
import time
import requests

WIKI_API_URL = "https://en.wikipedia.org/w/api.php"

SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": "StudyBrainrotGenerator/1.0 (personal study project; https://github.com)"}
)


def wiki_search(topic, limit=3):
    params = {
        "action": "query",
        "list": "search",
        "srsearch": topic,
        "format": "json",
        "srlimit": limit,
    }
    r = SESSION.get(WIKI_API_URL, params=params, timeout=15)
    r.raise_for_status()
    return [item["title"] for item in r.json()["query"]["search"]]


def wiki_extract(title):
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "titles": title,
        "format": "json",
    }
    r = SESSION.get(WIKI_API_URL, params=params, timeout=15)
    r.raise_for_status()
    pages = r.json()["query"]["pages"]
    page = next(iter(pages.values()))
    return page.get("extract", "")


def main():
    topic = os.environ.get("TOPIC") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not topic:
        print("ERROR: no topic provided (set TOPIC env var or pass as arg)", file=sys.stderr)
        sys.exit(1)

    print(f"[research] searching Wikipedia for: {topic}")
    try:
        titles = wiki_search(topic)
    except requests.RequestException as e:
        print(f"ERROR: Wikipedia search failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not titles:
        print(f"ERROR: no Wikipedia results for '{topic}'", file=sys.stderr)
        sys.exit(1)

    chunks = []
    for title in titles:
        try:
            text = wiki_extract(title)
        except requests.RequestException as e:
            print(f"[research] WARNING: failed to fetch '{title}': {e}", file=sys.stderr)
            continue
        if text:
            chunks.append(f"== {title} ==\n{text}")
        time.sleep(0.3)

    if not chunks:
        print("ERROR: fetched zero usable pages", file=sys.stderr)
        sys.exit(1)

    combined = "\n\n".join(chunks)[:15000]  # bound LLM input size

    os.makedirs("build", exist_ok=True)
    with open("build/research.txt", "w", encoding="utf-8") as f:
        f.write(combined)

    print(f"[research] wrote build/research.txt ({len(combined)} chars, {len(chunks)} sources)")


if __name__ == "__main__":
    main()
