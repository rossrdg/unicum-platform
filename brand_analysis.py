"""
brand_analysis.py
Функции за автоматичен анализ на website и Facebook страница.
Връщат структуриран JSON съвместим с Notion Brand Profile полетата.
API ключовете се четат от .env.
"""

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

# Strict schema — short values prevent unterminated string errors
BRAND_JSON_SCHEMA = """{
  "tone_of_voice": "ЕДНО от: Приятелски / Вдъхновяващ / Професионален / Луксозен / Семеен / Динамичен",
  "audience": "максимум 120 знака, без кавички вътре",
  "brand_message": "максимум 160 знака, без кавички вътре",
  "forbidden_words": "максимум 80 знака, само думи разделени със запетаи",
  "niche": "ЕДНО от: Ресторант / Ритейл / Фитнес / Дентална клиника / Маркетинг агенция / Хотел / Недвижими имоти / Друго"
}"""

GEMINI_SYSTEM_PROMPT = (
    "Ти си експерт по бранд анализ. "
    "Връщай САМО валиден JSON обект — без markdown, без обяснения, без код блокове. "
    "Стойностите в JSON НЕ трябва да съдържат кавички, двойни кавички или специални символи. "
    "Спазвай СТРОГО посочените лимити за брой знаци."
)


def _sanitize(text: str, max_chars: int = 7000) -> str:
    """Remove characters that break JSON strings when embedded in prompts."""
    # Replace smart quotes, backticks, control chars
    text = text.replace('"', "'").replace('"', "'").replace('"', "'")
    text = text.replace('`', "'").replace('\\', ' ')
    # Remove control characters except newline/tab
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    return text[:max_chars]


def _extract_json(raw: str) -> dict:
    """Robustly extract JSON from Gemini response, handling common failure modes."""
    # Strip markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Find first { ... } block
    start = raw.find('{')
    end = raw.rfind('}')
    if start != -1 and end != -1 and end > start:
        candidate = raw[start:end+1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Last resort: extract key-value pairs with regex
    result = {}
    for key in ['tone_of_voice', 'audience', 'brand_message', 'forbidden_words', 'niche']:
        # Match "key": "value" or "key": value
        pattern = rf'"{key}"\s*:\s*"([^"]*)"'
        m = re.search(pattern, raw)
        if m:
            result[key] = m.group(1)
        else:
            # Try without quotes on value
            pattern2 = rf'"{key}"\s*:\s*([^,\n\}}]+)'
            m2 = re.search(pattern2, raw)
            if m2:
                result[key] = m2.group(1).strip().strip('"').strip("'")

    if result:
        return result

    raise ValueError(f"Gemini върна невалиден JSON. Суров отговор (първи 300 знака): {raw[:300]}")


def _call_gemini(prompt: str) -> dict:
    """Call Gemini REST API and parse JSON response."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не е намерен в .env")

    payload = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        }
    }

    resp = requests.post(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        json=payload,
        timeout=60
    )
    resp.raise_for_status()

    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise ValueError("Gemini не върна candidates")

    raw_text = candidates[0]["content"]["parts"][0]["text"]
    return _extract_json(raw_text)


def analyze_website_for_brand(website_url: str) -> dict:
    """Crawl website → Gemini analysis → brand JSON."""
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY не е намерен в .env")

    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
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
        raise ValueError(f"Firecrawl не върна job ID: {crawl_resp.text}")

    # Poll (max 3 min)
    status_url = f"https://api.firecrawl.dev/v1/crawl/{job_id}"
    status_data = {}
    for _ in range(60):
        time.sleep(3)
        status_resp = requests.get(status_url, headers=headers, timeout=15)
        status_data = status_resp.json()
        if status_data.get("status") == "completed":
            break
        if status_data.get("status") == "failed":
            raise RuntimeError("Firecrawl crawl задачата пропадна")
    else:
        raise TimeoutError("Firecrawl не завърши в рамките на 3 минути")

    # Combine and sanitize (critical: removes chars that break JSON)
    parts = []
    for page in status_data.get("data", []):
        title = page.get("metadata", {}).get("title", "")
        markdown = page.get("markdown", "")[:1200]
        parts.append(f"=== {title} ===\n{markdown}")
    combined = _sanitize("\n".join(parts), max_chars=6000)

    if not combined.strip():
        raise ValueError("Firecrawl върна празен резултат")

    prompt = (
        "Анализирай следния уебсайт и извлечи brand identity информация.\n"
        "Игнорирай сезонни промоции и конкурси. "
        "Фокусирай се върху: услуги, начин на комуникация, ценности на бранда.\n\n"
        f"ОЧАКВАН ФОРМАТ (само JSON):\n{BRAND_JSON_SCHEMA}\n\n"
        f"ДАННИ ОТ САЙТА:\n{combined}"
    )

    return _call_gemini(prompt)


def analyze_facebook_for_brand(fb_page: str) -> dict:
    """Scrape FB page + posts → Gemini analysis → brand JSON."""
    if not APIFY_API_KEY:
        raise ValueError("APIFY_API_KEY не е намерен в .env")

    # Normalize: accept full URL or slug
    if fb_page.startswith("http"):
        base_url = fb_page
    else:
        base_url = f"https://www.facebook.com/{fb_page.strip('/')}"

    def run_actor(actor_id: str, run_input: dict) -> list:
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{actor_id}/runs?token={APIFY_API_KEY}&waitForFinish=120",
            json=run_input, timeout=150
        )
        resp.raise_for_status()
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
        if not dataset_id:
            return []
        items_resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_KEY}&format=json",
            timeout=30
        )
        items_resp.raise_for_status()
        return items_resp.json()

    page_items = run_actor("apify/facebook-pages-scraper", {"startUrls": [{"url": base_url}]})
    posts_items = run_actor("apify/facebook-posts-scraper", {
        "startUrls": [{"url": base_url}], "resultsLimit": 10
    })

    # Extract minimal fields
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

    # Sanitize before embedding in prompt
    page_str = _sanitize(json.dumps(page_info, ensure_ascii=False), 1500)
    posts_str = _sanitize("\n---\n".join(posts_text), 3500)

    prompt = (
        "Анализирай следната Facebook страница и извлечи brand identity информация.\n"
        "Игнорирай сезонни поздравления и конкурси. "
        "Фокусирай се върху тон, стил, емоджита, обръщения към аудиторията.\n\n"
        f"ОЧАКВАН ФОРМАТ (само JSON):\n{BRAND_JSON_SCHEMA}\n\n"
        f"ИНФОРМАЦИЯ ЗА СТРАНИЦАТА:\n{page_str}\n\n"
        f"ПОСЛЕДНИ ПОСТОВЕ:\n{posts_str}"
    )

    return _call_gemini(prompt)

Функции за автоматичен анализ на website и Facebook страница.
Връщат структуриран JSON съвместим с Notion Brand Profile полетата.
API ключовете се четат от .env.
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
APIFY_API_KEY = os.getenv("APIFY_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini REST endpoint (no SDK dependency)
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# Структура на JSON изхода — съвпада с Notion Brand Profile полетата
BRAND_JSON_SCHEMA = """{
  "tone_of_voice": "едно от: Приятелски / Вдъхновяващ / Професионален / Луксозен / Семеен / Динамичен",
  "audience": "до 150 знака — напр. Жени 25-45г. в София, търсещи здравословен начин на живот",
  "brand_message": "до 200 знака — основното послание на бранда",
  "forbidden_words": "до 100 знака — думи/фрази, които брандът НЕ използва, разделени със запетаи",
  "niche": "едно от: Ресторант / Ритейл / Фитнес / Дентална клиника / Маркетинг агенция / Хотел / Недвижими имоти / Друго"
}"""

GEMINI_SYSTEM_PROMPT = """Ти си експерт по бранд анализ. 
Анализираш предоставените данни и извличаш brand identity информация.
ЗАДЪЛЖИТЕЛНО връщай САМО валиден JSON обект — без markdown, без обяснения, без код блокове.
Придържай се СТРОГО към посочените лимити за брой знаци."""


def _call_gemini(prompt: str) -> dict:
    """Call Gemini REST API and parse JSON response."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не е намерен в .env")

    payload = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
        }
    }

    resp = requests.post(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        json=payload,
        timeout=60
    )
    resp.raise_for_status()

    raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Strip markdown fences if model adds them despite instructions
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip()

    return json.loads(raw_text)


def analyze_website_for_brand(website_url: str) -> dict:
    """
    1. Crawl website with Firecrawl (async polling)
    2. Combine markdown from up to 10 pages
    3. Analyze with Gemini → return brand JSON
    """
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY не е намерен в .env")

    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json"
    }

    # 1. Start crawl job
    crawl_resp = requests.post(
        "https://api.firecrawl.dev/v1/crawl",
        json={
            "url": website_url,
            "limit": 10,
            "scrapeOptions": {"formats": ["markdown"]}
        },
        headers=headers,
        timeout=30
    )
    crawl_resp.raise_for_status()
    job_id = crawl_resp.json().get("id")
    if not job_id:
        raise ValueError(f"Firecrawl не върна job ID: {crawl_resp.text}")

    # 2. Poll for completion (max 3 minutes)
    status_url = f"https://api.firecrawl.dev/v1/crawl/{job_id}"
    for _ in range(60):
        time.sleep(3)
        status_resp = requests.get(status_url, headers=headers, timeout=15)
        status_data = status_resp.json()
        if status_data.get("status") == "completed":
            break
        if status_data.get("status") == "failed":
            raise RuntimeError("Firecrawl crawl задачата пропадна")
    else:
        raise TimeoutError("Firecrawl не завърши в рамките на 3 минути")

    # 3. Combine markdown (1500 chars per page)
    combined = ""
    for page in status_data.get("data", []):
        title = page.get("metadata", {}).get("title", "")
        markdown = page.get("markdown", "")[:1500]
        combined += f"\n--- {title} ---\n{markdown}"

    if not combined.strip():
        raise ValueError("Firecrawl върна празен резултат")

    # 4. Gemini analysis
    prompt = f"""Анализирай следния уебсайт и извлечи brand identity информация.

ВАЖНО: Игнорирай сезонни промоции, конкурси и временен контент.
Фокусирай се върху: услуги/продукти, начин на комуникация, ценностите на бранда.

ОЧАКВАН ФОРМАТ (само JSON, нищо друго):
{BRAND_JSON_SCHEMA}

ДАННИ ОТ САЙТА:
{combined[:8000]}"""

    return _call_gemini(prompt)


def analyze_facebook_for_brand(fb_page: str) -> dict:
    """
    1. Scrape FB page + last 10 posts with Apify
    2. Analyze with Gemini → return brand JSON
    """
    if not APIFY_API_KEY:
        raise ValueError("APIFY_API_KEY не е намерен в .env")

    base_url = f"https://www.facebook.com/{fb_page.strip('/')}"

    def run_actor(actor_id: str, run_input: dict) -> list:
        """Run Apify actor synchronously and return dataset items."""
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{actor_id}/runs?token={APIFY_API_KEY}&waitForFinish=120",
            json=run_input,
            timeout=150
        )
        resp.raise_for_status()
        run_data = resp.json().get("data", {})
        dataset_id = run_data.get("defaultDatasetId")
        if not dataset_id:
            return []

        items_resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_API_KEY}&format=json",
            timeout=30
        )
        items_resp.raise_for_status()
        return items_resp.json()

    # Scrape page info and last 10 posts
    page_items = run_actor("apify/facebook-pages-scraper", {
        "startUrls": [{"url": base_url}]
    })
    posts_items = run_actor("apify/facebook-posts-scraper", {
        "startUrls": [{"url": base_url}],
        "resultsLimit": 10
    })

    # Extract relevant fields only (avoid token bloat)
    def extract_page_info(items):
        if not items:
            return {}
        p = items[0]
        return {
            "name": p.get("name"),
            "category": p.get("category"),
            "description": p.get("description", "")[:500],
            "about": p.get("about", "")[:300],
            "likes": p.get("likes"),
        }

    def extract_posts(items):
        posts = []
        for item in items[:10]:
            text = item.get("text") or item.get("message") or ""
            # Skip pure holiday/contest posts
            posts.append({
                "text": text[:400],
                "likes": item.get("likes"),
                "type": item.get("type"),
            })
        return posts

    page_info = extract_page_info(page_items)
    posts = extract_posts(posts_items)

    combined = json.dumps({
        "page": page_info,
        "recent_posts": posts
    }, ensure_ascii=False)

    prompt = f"""Анализирай следната Facebook страница и последните 10 поста и извлечи brand identity информация.

ВАЖНО: 
- Игнорирай сезонни поздравления (Коледа, Великден, 8-ми март) и конкурси при анализа на тона.
- Facebook данните дават емоцията и стила — фокусирай се на структурата на изреченията, емоджита, обръщения към аудиторията.
- Ако постовете са твърде малко или неинформативни, използвай page description/about.

ОЧАКВАН ФОРМАТ (само JSON, нищо друго):
{BRAND_JSON_SCHEMA}

ДАННИ:
{combined[:6000]}"""

    return _call_gemini(prompt)
