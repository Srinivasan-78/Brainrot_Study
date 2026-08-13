#!/usr/bin/env python3
"""research text -> JSON of concept chunks, with Gemini -> DeepSeek fallback."""
import os
import sys
import json
import time

SYSTEM_PROMPT = """You are a study-content scriptwriter for short vertical educational videos.
Given source material on a topic, produce 8 to 12 short concept chunks that teach the topic
in a punchy, fast-paced "brainrot style" — short sentences, high energy, no fluff.

Return ONLY valid JSON, no markdown fences, no commentary, matching this schema exactly:
{
  "topic": "<string>",
  "chunks": [
    {"text": "<40-60 word explanation, spoken style>", "quiz": "<optional short quiz question, or null>"}
  ]
}
"""


def build_prompt(topic, research_text):
    return f"Topic: {topic}\n\nSource material:\n{research_text}\n\nProduce the JSON now."


def validate(data):
    if not isinstance(data, dict) or "chunks" not in data:
        raise ValueError("missing 'chunks' key")
    chunks = data["chunks"]
    if not isinstance(chunks, list) or not (8 <= len(chunks) <= 12):
        got = len(chunks) if isinstance(chunks, list) else "non-list"
        raise ValueError(f"expected 8-12 chunks, got {got}")
    for i, c in enumerate(chunks):
        if "text" not in c or not isinstance(c["text"], str) or not c["text"].strip():
            raise ValueError(f"chunk {i} missing/empty 'text'")
    return data


def strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


def call_gemini(prompt, api_key):
    import google.generativeai as genai
    from google.api_core import exceptions as gexc

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=SYSTEM_PROMPT)

    last_err = None
    for attempt in range(1, 3):
        try:
            resp = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"},
            )
            return strip_fences(resp.text)
        except gexc.ResourceExhausted as e:
            # specific rate-limit / quota-exceeded error only
            last_err = e
            print(f"[script_gen] Gemini rate/quota limit hit (attempt {attempt}/2): {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(5)
    raise last_err


def call_deepseek(prompt, api_key):
    from openai import OpenAI, RateLimitError

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
    os.makedirs("build", exist_ok=True)
    with open("build/script.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"[script_gen] wrote build/script.json via {provider_used} ({len(data['chunks'])} chunks)")


if __name__ == "__main__":
    main()
