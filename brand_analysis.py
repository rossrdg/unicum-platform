# -*- coding: utf-8 -*-
"""brand_analysis.py - website and Facebook brand analysis, two-step approach."""

import os
import re
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
APIFY_API_KEY     = os.getenv("APIFY_API_KEY")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")

# Primary model with fallback
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "brand_schema.json")
with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    _SCHEMA = json.load(_f)
BRAND_JSON_SCHEMA = json.dumps(_SCHEMA, ensure_ascii=False, indent=2)

ANALYSIS_TXT_PATH = os.path.join(os.path.dirname(__file__), "brand_analysis_temp.txt")



# ── helpers ──────────────────────────────────────────────────────────────────

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
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Regex field-by-field fallback
    result = {}
    keys = ['tone_of_voice', 'audience', 'brand_message', 'forbidden_words',
            'niche', 'competitors', 'color_palette', 'sample_tone']
    for key in keys:
        m = re.search(r'"' + key + r'"\s*:\s*"([^"]{1,600})"', raw)
        if m:
            result[key] = m.group(1).strip()
        else:
            # Try without closing quote (truncated)
            m2 = re.search(r'"' + key + r'"\s*:\s*"([^"]{1,600})', raw)
            if m2:
                result[key] = m2.group(1).strip()
    if result:
        return result

    raise ValueError("Gemini returned invalid JSON: " + raw[:300])


def _gemini_call(prompt, system_prompt, max_tokens=4000):
    """Gemini REST call with model fallback."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not found in .env")

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}
    }

    last_error = None
    for model in GEMINI_MODELS:
        url = GEMINI_BASE + model + ":generateContent?key=" + GEMINI_API_KEY
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code in (503, 429, 500):
                last_error = str(resp.status_code) + " " + resp.text[:100]
                continue
            resp.raise_for_status()
            body = resp.json()
            candidates = body.get("candidates", [])
            if not candidates:
                last_error = body.get("error", {}).get("message", "no candidates")
                continue
            return candidates[0]["content"]["parts"][0]["text"]
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue

    raise ValueError("All Gemini models failed. Last error: " + str(last_error))


def _step1_prose_analysis(raw_content, source_type):
    """Step 1: Structured key-value brand summary — compact, never truncated."""
    system = (
        "You are a brand analyst. Extract brand information concisely. "
        "Answer each point in 1-3 sentences maximum. Write in Bulgarian Cyrillic. "
        "Never write more than 350 words total."
    )
    prompt = (
        "Analyze this " + source_type + " content. Answer each point in 1-3 sentences in Bulgarian:\n\n"
        "УСЛУГИ: (какво точно предлага бизнесът)\n"
        "ТОН: (как комуникира с клиентите, стил)\n"
        "АУДИТОРИЯ: (възраст, интереси, локация)\n"
        "ПОСЛАНИЕ: (основното обещание към клиентите)\n"
        "ЗАБРАНЕНИ ДУМИ: (думи/фрази, неподходящи за бранда)\n"
        "КОНКУРЕНТИ: (конкретни конкуренти на този тип бизнес)\n"
        "ВИЗУАЛЕН СТИЛ: (цветове, визуален стил ако е видимо)\n\n"
        "CONTENT:\n" + raw_content[:6000]
    )
    return _gemini_call(prompt, system, max_tokens=4000)


# Exact allowed values for select fields
TONE_OPTIONS = ["Приятелски", "Вдъхновяващ", "Професионален", "Луксозен", "Семеен", "Динамичен"]
NICHE_OPTIONS = ["Ресторант", "Ритейл", "Фитнес", "Футболна Академия", "Дентална клиника", "Маркетинг агенция", "Хотел", "Недвижими имоти", "Друго"]

def _step2_extract_json(prose_analysis):
    """Step 2: Extract structured JSON from the prose analysis."""
    system = (
        "You are a data extraction assistant. "
        "Return ONLY a valid raw JSON object. "
        "Start with { and end with }. "
        "No markdown, no code blocks, no explanation before or after. "
        "Fill every single field."
    )

    tone_list = " / ".join(TONE_OPTIONS)
    niche_list = " / ".join(NICHE_OPTIONS)

    prompt = (
        "Extract brand identity from the analysis below into this exact JSON.\n"
        "Rules:\n"
        "- tone_of_voice: pick EXACTLY one of these (copy exactly): " + tone_list + "\n"
        "- niche: pick EXACTLY one of these (copy exactly): " + niche_list + "\n"
        "- audience: Bulgarian Cyrillic, max 120 chars\n"
        "- brand_message: Bulgarian Cyrillic, max 160 chars\n"
        "- forbidden_words: Bulgarian Cyrillic, comma-separated words/phrases, max 120 chars\n"
        "- competitors: Bulgarian Cyrillic, comma-separated names, max 120 chars\n"
        "- color_palette: hex codes with descriptions, max 120 chars\n"
        "- sample_tone: one example sentence in brand voice, Bulgarian Cyrillic, max 160 chars\n"
        "- If a field cannot be determined, make a reasonable inference\n"
        "- niche: if business is sport school, academy, club or any activity NOT in the list -> use Друго\n\n"
        "Return ONLY this JSON (no other text):\n"
        "{\n"
        "  \"tone_of_voice\": \"<one of: " + tone_list + ">\",\n"
        "  \"niche\": \"<one of: " + niche_list + ">\",\n"
        "  \"audience\": \"<target audience in Bulgarian>\",\n"
        "  \"brand_message\": \"<core message in Bulgarian>\",\n"
        "  \"forbidden_words\": \"<forbidden words in Bulgarian>\",\n"
        "  \"competitors\": \"<competitors in Bulgarian>\",\n"
        "  \"color_palette\": \"<colors>\",\n"
        "  \"sample_tone\": \"<example sentence in Bulgarian>\"\n"
        "}\n\n"
        "BRAND ANALYSIS:\n" + prose_analysis
    )
    raw = _gemini_call(prompt, system, max_tokens=4000)
    result = _extract_json(raw)

    # Post-process: normalize tone_of_voice and niche to exact allowed values
    if result.get("tone_of_voice"):
        val = result["tone_of_voice"].strip()
        match = next((o for o in TONE_OPTIONS if o.lower() in val.lower() or val.lower() in o.lower()), None)
        if match:
            result["tone_of_voice"] = match

    if result.get("niche"):
        val = result["niche"].strip()
        match = next((o for o in NICHE_OPTIONS if o.lower() in val.lower() or val.lower() in o.lower()), None)
        if match:
            result["niche"] = match

    return result


# ── public functions ──────────────────────────────────────────────────────────

def analyze_website_for_brand(website_url):
    """Crawl website -> prose analysis -> structured JSON."""
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY not found in .env")

    headers = {
        "Authorization": "Bearer " + FIRECRAWL_API_KEY,
        "Content-Type": "application/json"
    }

    # 1. Start crawl
    crawl_resp = requests.post(
        "https://api.firecrawl.dev/v1/crawl",
        json={"url": website_url, "limit": 10,
              "scrapeOptions": {"formats": ["markdown"]}},
        headers=headers, timeout=30
    )
    crawl_resp.raise_for_status()
    job_id = crawl_resp.json().get("id")
    if not job_id:
        raise ValueError("Firecrawl did not return job ID: " + crawl_resp.text)

    # 2. Poll
    status_url = "https://api.firecrawl.dev/v1/crawl/" + job_id
    status_data = {}
    for _ in range(60):
        time.sleep(3)
        status_data = requests.get(status_url, headers=headers, timeout=15).json()
        if status_data.get("status") == "completed":
            break
        if status_data.get("status") == "failed":
            raise RuntimeError("Firecrawl job failed")
    else:
        raise TimeoutError("Firecrawl did not finish within 3 minutes")

    # 3. Combine markdown (same approach as website_summary.py)
    combined = ""
    for page in status_data.get("data", []):
        title = page.get("metadata", {}).get("title", "")
        markdown = page.get("markdown", "")[:1500]
        combined += "\n--- " + title + " ---\n" + markdown

    combined = _sanitize(combined, max_chars=7000)
    if not combined.strip():
        raise ValueError("Firecrawl returned empty result")

    # 4. Step 1: prose analysis
    prose = _step1_prose_analysis(combined, "уебсайт")

    # 5. Write to .txt (overwrite — fresh start per client)
    with open(ANALYSIS_TXT_PATH, "w", encoding="utf-8") as f:
        f.write("=== АНАЛИЗ НА УЕБСАЙТ ===\n\n")
        f.write(prose)
        f.write("\n\n")

    return {"status": "website_done", "chars": len(prose)}


def analyze_facebook_for_brand(fb_page):
    """Scrape FB page + posts -> prose analysis -> structured JSON."""
    if not APIFY_API_KEY:
        raise ValueError("APIFY_API_KEY not found in .env")

    if fb_page.startswith("http"):
        base_url = fb_page
    else:
        base_url = "https://www.facebook.com/" + fb_page.strip('/')

    def run_actor(actor_id, run_input):
        resp = requests.post(
            "https://api.apify.com/v2/acts/" + actor_id + "/runs"
            + "?token=" + APIFY_API_KEY + "&waitForFinish=120",
            json=run_input, timeout=150
        )
        resp.raise_for_status()
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
        if not dataset_id:
            return []
        items = requests.get(
            "https://api.apify.com/v2/datasets/" + dataset_id
            + "/items?token=" + APIFY_API_KEY + "&format=json",
            timeout=30
        )
        items.raise_for_status()
        return items.json()

    # KoJrdxJCTtpon81KY = официалният Apify Facebook Posts Scraper actor ID
    posts_items = run_actor("KoJrdxJCTtpon81KY",
                            {"startUrls": [{"url": base_url}], "resultsLimit": 15})

    # Build combined text — posts actor включва page metadata в резултатите
    page_info = {}
    if posts_items:
        p = posts_items[0]
        page_data = p.get("page") or p.get("pageInfo") or {}
        page_info = {
            "name":        page_data.get("name") or p.get("pageName", ""),
            "category":    page_data.get("category", ""),
            "description": (page_data.get("description") or p.get("pageDescription", ""))[:500],
            "about":       (page_data.get("about") or "")[:300],
            "likes":       page_data.get("likes") or page_data.get("followers"),
        }

    posts_text = []
    for item in posts_items[:15]:
        text = (item.get("text") or item.get("message") or item.get("postText") or "")[:300]
        if text:
            posts_text.append(text)

    combined = (
        "PAGE INFO:\n" + _sanitize(json.dumps(page_info, ensure_ascii=False), 1500)
        + "\n\nRECENT POSTS (last 10):\n"
        + _sanitize("\n---\n".join(posts_text), 4000)
    )

    # Step 1: prose analysis
    prose = _step1_prose_analysis(combined, "Facebook страница")

    # Append to .txt (след website анализа ако го има, иначе нов файл)
    mode = "a" if os.path.exists(ANALYSIS_TXT_PATH) else "w"
    with open(ANALYSIS_TXT_PATH, mode, encoding="utf-8") as f:
        f.write("=== АНАЛИЗ НА FACEBOOK СТРАНИЦА ===\n\n")
        f.write(prose)
        f.write("\n\n")

    return {"status": "facebook_done", "chars": len(prose)}


def extract_combined_brand_json():
    """
    Чете brand_analysis_temp.txt и прави финален JSON extraction
    от обединения анализ на website + Facebook.
    Изтрива .txt файла след успешна екстракция.
    """
    if not os.path.exists(ANALYSIS_TXT_PATH):
        raise ValueError("Няма записан анализ. Пуснете поне един от анализите.")

    with open(ANALYSIS_TXT_PATH, "r", encoding="utf-8") as f:
        full_analysis = f.read()

    if not full_analysis.strip():
        raise ValueError("Файлът с анализ е празен.")

    result = _step2_extract_json(full_analysis[:10000])

    try:
        os.remove(ANALYSIS_TXT_PATH)
    except Exception:
        pass

    return result


def clear_analysis_file():
    """Изтрива временния .txt файл (при отказ от онбординга)."""
    try:
        if os.path.exists(ANALYSIS_TXT_PATH):
            os.remove(ANALYSIS_TXT_PATH)
    except Exception:
        pass
