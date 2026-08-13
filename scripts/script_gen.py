#!/usr/bin/env python3
"""research text -> JSON of concept chunks, with Gemini -> DeepSeek fallback."""
import os
import sys
import json
import time

SYSTEM_PROMPT = """You are a study-content scriptwriter for short vertical educational videos.
Given source material on a topic, produce 12 concept chunks that teach the topic
in a punchy, fast-paced "brainrot style" — short sentences, high energy, no fluff.

Each chunk must be 25-35 words: these are spoken aloud at speed, and chunks are packed
into videos of under a minute each, so brevity matters more than completeness.
Chunk 1 must open with a hook that makes someone stop scrolling.

Return ONLY valid JSON, no markdown fences, no commentary, matching this schema exactly:
{
  "topic": "<string>",
  "chunks": [
    {"text": "<25-35 word explanation, spoken style>"}
  ]
}
"""


def build_prompt(topic, research_text):
    return f"Topic: {topic}\n\nSource material:\n{research_text}\n\nProduce the JSON now."


def validate(data):
    if not isinstance(data, dict) or "chunks" not in data:
        raise ValueError("missing 'chunks' key")
    chunks = data["chunks"]
    if not isinstance(chunks, list) or not (6 <= len(chunks) <= 16):
        got = len(chunks) if isinstance(chunks, list) else "non-list"
        raise ValueError(f"expected 6-16 chunks, got {got}")
    for i, c in enumerate(chunks):
        if "text" not in c or not isinstance(c["text"], str) or not c["text"].strip():
            raise ValueError(f"chunk {i} missing/empty 'text'")
        words = len(c["text"].split())
        if words > 60:
            raise ValueError(f"chunk {i} is {words} words — far over the 25-35 target")
    return data


def strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


EXCLUDE_TOKENS = (
    "preview", "-exp", "experimental", "omni", "thinking", "tts",
    "audio", "image", "live", "embedding", "learnlm", "gemma", "vision",
)


def rank_gemini_models(client):
    """Return candidate model names, best free-tier bet first.

    Preview/experimental models frequently report a free-tier quota of 0, so they
    are ranked last and only used if nothing stable is available.
    """
    override = os.environ.get("GEMINI_MODEL")
    if override:
        return [override]

    try:
        names = [m.name.replace("models/", "") for m in client.models.list()]
    except Exception as e:
        print(f"[script_gen] WARNING: could not list Gemini models ({e})", file=sys.stderr)
        names = []

    names = [n for n in names if n.startswith("gemini")]

    def score(name):
        stable = not any(t in name for t in EXCLUDE_TOKENS)
        is_flash = "flash" in name
        is_lite = "lite" in name
        is_alias = name.endswith("-latest")
        # lower sorts first
        return (
            0 if stable else 1,
            0 if is_flash else 1,
            0 if is_lite else 1,   # lite has the most generous free RPM
            0 if is_alias else 1,
            name,
        )

    ranked = sorted(set(names), key=score)
    fallbacks = ["gemini-flash-lite-latest", "gemini-flash-latest"]
    for fb in fallbacks:
        if fb not in ranked:
            ranked.append(fb)

    if not ranked:
        ranked = fallbacks
    print(f"[script_gen] Gemini candidates: {ranked[:5]}")
    return ranked[:5]


def call_gemini(prompt, api_key):
    from google import genai
    from google.genai import types, errors

    client = genai.Client(api_key=api_key)
    candidates = rank_gemini_models(client)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
    )

    last_err = None
    for attempt in range(1, 3):
        for model_name in candidates:
            try:
                resp = client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                print(f"[script_gen] Gemini succeeded with {model_name}")
                return strip_fences(resp.text)
            except errors.ClientError as e:
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code == 429:
                    last_err = e
                    print(f"[script_gen] {model_name}: quota exhausted, trying next model", file=sys.stderr)
                    continue
                if code == 404:
                    last_err = e
                    print(f"[script_gen] {model_name}: not available, trying next model", file=sys.stderr)
                    continue
                raise  # 400/403 are config errors — fail fast
        if attempt < 2:
            print(f"[script_gen] all Gemini models exhausted (pass {attempt}/2), waiting 40s", file=sys.stderr)
            time.sleep(40)
    raise last_err


def call_deepseek(prompt, api_key):
    from openai import OpenAI, RateLimitError, APIStatusError

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    last_err = None
    for attempt in range(1, 3):
        try:
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            return strip_fences(resp.choices[0].message.content)
        except RateLimitError as e:
            last_err = e
            print(f"[script_gen] DeepSeek rate/quota limit hit (attempt {attempt}/2): {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(5)
        except APIStatusError as e:
            if e.status_code == 402:
                raise RuntimeError(
                    "DeepSeek returned 402 Insufficient Balance — the account has no credit. "
                    "Top it up at platform.deepseek.com or unset DEEPSEEK_API_KEY."
                ) from e
            raise
    raise last_err


def main():
    topic = os.environ.get("TOPIC")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")

    if not topic:
        print("ERROR: TOPIC env var not set", file=sys.stderr)
        sys.exit(1)
    if not gemini_key and not deepseek_key:
        print("ERROR: neither GEMINI_API_KEY nor DEEPSEEK_API_KEY set", file=sys.stderr)
        sys.exit(1)

    with open("build/research.txt", encoding="utf-8") as f:
        research_text = f.read()

    prompt = build_prompt(topic, research_text)

    raw = None
    provider_used = None

    if gemini_key:
        try:
            print("[script_gen] trying Gemini...")
            raw = call_gemini(prompt, gemini_key)
            provider_used = "gemini"
        except Exception as e:
            print(f"[script_gen] Gemini failed after retries, falling back to DeepSeek: {e}", file=sys.stderr)

    if raw is None:
        if not deepseek_key:
            print("ERROR: Gemini failed and no DEEPSEEK_API_KEY configured for fallback", file=sys.stderr)
            sys.exit(1)
        try:
            print("[script_gen] trying DeepSeek...")
            raw = call_deepseek(prompt, deepseek_key)
            provider_used = "deepseek"
        except Exception as e:
            print(f"ERROR: both Gemini and DeepSeek failed. Last error: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        data = json.loads(raw)
        data = validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: {provider_used} returned invalid JSON: {e}\nRaw output:\n{raw}", file=sys.stderr)
        sys.exit(1)

    data.setdefault("topic", topic)

    chunks = data["chunks"]
    os.makedirs("build", exist_ok=True)
    with open("build/script.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    words = sum(len(c["text"].split()) for c in chunks)
    print(
        f"[script_gen] wrote build/script.json via {provider_used}: "
        f"{len(chunks)} chunks, ~{words} words (~{words / 2.5:.0f}s of speech)"
    )


if __name__ == "__main__":
    main()
