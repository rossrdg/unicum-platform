# -*- coding: utf-8 -*-
"""brand_analysis.py - website and Facebook brand analysis."""

import os
import re
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
APIFY_API_KEY = os.getenv("APIFY_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

BRAND_JSON_SCHEMA = """{
  "tone_of_voice": "EDNO ot: Priqtelski / Vdahnovqvasht / Profesionalen / Luksozen / Semeen / Dinamichen",
  "audience": "maksimum 120 znaka",
  "brand_message": "maksimum 160 znaka",
  "forbidden_words": "maksimum 80 znaka, dumi razdeleni sas zapetai",
  "niche": "EDNO ot: Restorant / Ritel / Fitnes / Dentalna klinika / Marketing agenciq / Hotel / Nedvijimi imoti / Drugo"
}"""

GEMINI_SYSTEM_PROMPT = (
    "You are a brand analysis expert. "
    "Return ONLY a valid JSON object - no markdown, no explanations, no code blocks. "
    "Values in JSON must NOT contain quotes or special characters. "
    "Strictly follow the character limits specified."
)


def _sanitize(text, max_chars=6000):
    text = text.replace('"', "'").replace('\u201c', "'").replace('\u201d', "'")
    text = text.replace('`', "'").replace('\\', ' ')
    text = text.replace('\u2014', '-').replace('\u2013', '-').replace('\u2192', '->')
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    return text[:max_chars]


def _extract_json(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start, end = raw.find('{'), raw.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end+1])
        except json.JSONDecodeError:
            pass

    result = {}
    for key in ['tone_of_voice', 'audience', 'brand_message', 'forbidden_words', 'niche']:
        m = re.search(r'"' + key + r'"\s*:\s*"([^"]*)', raw)
        if m:
            result[key] = m.group(1)
        else:
            m2 = re.search(r'"' + key + r'"\s*:\s*([^,\n\}]+)', raw)
            if m2:
                result[key] = m2.group(1).strip().strip('"').strip("'")

    if result:
        return result

    raise ValueError("Gemini върна невалиден JSON: " + raw[:200])


def _call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не е намерен в .env")

    payload = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 600,
        }
    }

    resp = requests.post(
        GEMINI_ENDPOINT + "?key=" + GEMINI_API_KEY,
        json=payload,
        timeout=60
    )
    resp.raise_for_status()

    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise ValueError("Gemini не върна candidates")

    raw_text = candidates[0]["content"]["parts"][0]["text"]
    return _extract_json(raw_text)


def analyze_website_for_brand(website_url):
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY не е намерен в .env")

    headers = {
        "Authorization": "Bearer " + FIRECRAWL_API_KEY,
        "Content-Type": "application/json"
    }

    crawl_resp = requests.post(
        "https://api.firecrawl.dev/v1/crawl",
        json={"url": website_url, "limit": 10, "scrapeOptions": {"formats": ["markdown"]}},
        headers=headers,
        timeout=30
    )
    crawl_resp.raise_for_status()
    job_id = crawl_resp.json().get("id")
    if not job_id:
        raise ValueError("Firecrawl не върна job ID: " + crawl_resp.text)

    status_url = "https://api.firecrawl.dev/v1/crawl/" + job_id
    status_data = {}
    for _ in range(60):
        time.sleep(3)
        status_resp = requests.get(status_url, headers=headers, timeout=15)
        status_data = status_resp.json()
        if status_data.get("status") == "completed":
            break
        if status_data.get("status") == "failed":
            raise RuntimeError("Firecrawl задачата пропадна")
    else:
        raise TimeoutError("Firecrawl не завърши в рамките на 3 минути")

    parts = []
    for page in status_data.get("data", []):
        title = page.get("metadata", {}).get("title", "")
        markdown = page.get("markdown", "")[:1200]
        parts.append("=== " + title + " ===\n" + markdown)
    combined = _sanitize("\n".join(parts), max_chars=6000)

    if not combined.strip():
        raise ValueError("Firecrawl върна празен резултат")

    prompt = (
        "Analyze this website and extract brand identity information. "
        "Ignore seasonal promotions and contests. "
        "Focus on: services, communication style, brand values.\n\n"
        "EXPECTED FORMAT (JSON only):\n" + BRAND_JSON_SCHEMA + "\n\n"
        "Fill in Bulgarian language where appropriate.\n\n"
        "WEBSITE DATA:\n" + combined
    )

    return _call_gemini(prompt)


def analyze_facebook_for_brand(fb_page):
    if not APIFY_API_KEY:
        raise ValueError("APIFY_API_KEY не е намерен в .env")

    if fb_page.startswith("http"):
        base_url = fb_page
    else:
        base_url = "https://www.facebook.com/" + fb_page.strip('/')

    def run_actor(actor_id, run_input):
        resp = requests.post(
            "https://api.apify.com/v2/acts/" + actor_id + "/runs?token=" + APIFY_API_KEY + "&waitForFinish=120",
            json=run_input,
            timeout=150
        )
        resp.raise_for_status()
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
        if not dataset_id:
            return []
        items_resp = requests.get(
            "https://api.apify.com/v2/datasets/" + dataset_id + "/items?token=" + APIFY_API_KEY + "&format=json",
            timeout=30
        )
        items_resp.raise_for_status()
        return items_resp.json()

    page_items = run_actor("apify/facebook-pages-scraper", {"startUrls": [{"url": base_url}]})
    posts_items = run_actor("apify/facebook-posts-scraper", {
        "startUrls": [{"url": base_url}],
        "resultsLimit": 10
    })

    page_info = {}
    if page_items:
        p = page_items[0]
        page_info = {
            "name": p.get("name", ""),
            "category": p.get("category", ""),
            "description": p.get("description", "")[:400],
            "about": p.get("about", "")[:250],
        }

    posts_text = []
    for item in posts_items[:10]:
        text = (item.get("text") or item.get("message") or "")[:300]
        if text:
            posts_text.append(text)

    page_str = _sanitize(json.dumps(page_info, ensure_ascii=False), 1500)
    posts_str = _sanitize("\n---\n".join(posts_text), 3500)

    prompt = (
        "Analyze this Facebook page and extract brand identity information. "
        "Ignore seasonal greetings and contests. "
        "Focus on tone, style, emojis, how they address their audience.\n\n"
        "EXPECTED FORMAT (JSON only):\n" + BRAND_JSON_SCHEMA + "\n\n"
        "Fill in Bulgarian language where appropriate.\n\n"
        "PAGE INFO:\n" + page_str + "\n\n"
        "RECENT POSTS:\n" + posts_str
    )

    return _call_gemini(prompt)
