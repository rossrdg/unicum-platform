from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from notion_client import Client
from dotenv import load_dotenv
import requests as req_lib
import os

load_dotenv()

app = Flask(__name__, static_folder='static')
CORS(app)

notion = Client(auth=os.getenv("NOTION_TOKEN"))
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL")
NOTION_DB_BRAND = os.getenv("NOTION_DB_BRAND")
NOTION_DB_HISTORY = os.getenv("NOTION_DB_HISTORY")
NOTION_DB_CALENDAR = os.getenv("NOTION_DB_CALENDAR")


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_text(prop):
    try:
        return "".join(t.get("plain_text") or t.get("text", {}).get("content", "") for t in prop.get("rich_text", []))
    except Exception:
        return ""

def get_select(prop):
    try:
        return prop.get("select", {}).get("name", "") or ""
    except Exception:
        return ""

def get_title(prop):
    try:
        return "".join(t["plain_text"] for t in prop.get("title", []))
    except Exception:
        return ""

def get_number(prop):
    try:
        return prop.get("number") or 0
    except Exception:
        return 0

def get_date(prop):
    try:
        return (prop.get("date") or {}).get("start", "") or ""
    except Exception:
        return ""

def get_url(prop):
    try:
        if not prop:
            return ""
        return prop.get("url") or ""
    except Exception:
        return ""

def get_multi_select(prop):
    try:
        return [item["name"] for item in prop.get("multi_select", [])]
    except Exception:
        return []

# Cyrillic lookalike chars (А В Е К М Н О Р С Т Х) map to their Latin equivalents.
# Notion property names typed on a BG keyboard may contain these instead of Latin chars.
_CYR_TO_LAT = str.maketrans(
    'АВЕКМНОРСТХавекмнорстх',
    'ABEKMHOPCTXabekmnopctx'
)

def _vis_lower(s):
    """Visual-normalize: replace Cyrillic lookalikes → Latin, then lowercase."""
    return s.translate(_CYR_TO_LAT).lower()

def find_prop(props, name):
    """Case-insensitive property lookup that handles Cyrillic/Latin lookalike keys."""
    target = _vis_lower(name)
    for key, val in props.items():
        if _vis_lower(key) == target:
            return val
    return {}

def get_relation_name(prop):
    try:
        relations = prop.get("relation", [])
        if not relations:
            return ""
        page = notion.pages.retrieve(relations[0]["id"])
        for p in page.get("properties", {}).values():
            if p.get("type") == "title":
                return get_title(p)
        return ""
    except Exception:
        return ""

def get_relation_id(prop):
    """Return the first related page id from a relation property, or ''."""
    try:
        relations = prop.get("relation", [])
        return relations[0]["id"] if relations else ""
    except Exception:
        return ""

def truncate(text, limit=1900):
    """Truncate to Notion's 2000-char rich_text limit with safety margin."""
    return text[:limit] if text else ""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/client_calendar.html")
def client_calendar():
    return send_from_directory("static", "client_calendar.html")


@app.route("/api/clients", methods=["GET"])
def get_clients():
    try:
        results = notion.databases.query(database_id=NOTION_DB_BRAND).get("results", [])
        clients = []
        for page in results:
            try:
                props = page.get("properties", {})
                clients.append({
                    "id": page["id"],
                    "name": get_title(props.get("Клиент", {})),
                    "niche": get_select(props.get("Ниша", {})),
                    "tone": get_select(props.get("Tone of Voice", {})),
                    "status": get_select(props.get("Статус", {})),
                    "audience": get_text(props.get("Основна аудитория", {})),
                    "message": get_text(props.get("Бранд послание", {})),
                    "forbidden": get_text(props.get("Забранени думи и теми", {})),
                    "competitors": get_text(props.get("Конкуренти", {})),
                    "color_palette": get_text(props.get("Цветова палитра", {})),
                    "sample_tone": get_text(props.get("Примерни добри текстове", {})),
                    "channels": get_multi_select(props.get("Активни канали", {})),
                    "website_url": get_url(props.get("Website URL", {})),
                    "fb_page": get_text(props.get("Facebook Page", {})),
                    "token": get_text(find_prop(props, "Token")),
                })
            except Exception as page_err:
                clients.append({"id": page.get("id","?"), "name": "ERROR: " + str(page_err),
                                 "niche":"","tone":"","status":"","audience":"","message":"",
                                 "forbidden":"","competitors":"","color_palette":"",
                                 "sample_tone":"","website_url":"","fb_page":"","token":"","channels":[]})
        return jsonify(clients)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clients", methods=["POST"])
def create_client():
    data = request.json
    try:
        properties = {
            "Клиент": {"title": [{"text": {"content": data.get("name", "")}}]},
            "Статус": {"select": {"name": "Активен"}},
        }
        # Select fields — skip if empty to avoid Notion API errors
        if data.get("niche"):
            properties["Ниша"] = {"select": {"name": data["niche"]}}
        if data.get("tone"):
            properties["Tone of Voice"] = {"select": {"name": data["tone"]}}
        # Multi-select: Активни канали
        if data.get("channels"):
            properties["Активни канали"] = {
                "multi_select": [{"name": ch} for ch in data["channels"]]
            }

        # Rich text fields — all optional
        rt_map = {
            "Основна аудитория": data.get("audience", ""),
            "Бранд послание": data.get("message", ""),
            "Забранени думи и теми": data.get("forbidden", ""),
            "Конкуренти": data.get("competitors", ""),
            "Цветова палитра": data.get("color_palette", ""),
            "Примерни добри текстове": data.get("sample_tone", ""),
        }
        for notion_key, value in rt_map.items():
            if value:
                properties[notion_key] = {"rich_text": [{"text": {"content": truncate(value)}}]}

        notion.pages.create(parent={"database_id": NOTION_DB_BRAND}, properties=properties)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clients/<client_id>", methods=["GET"])
def get_client_detail(client_id):
    try:
        page = notion.pages.retrieve(client_id)
        props = page.get("properties", {})
        return jsonify({
            "id": page["id"],
            "name": get_title(props.get("Клиент", {})),
            "niche": get_select(props.get("Ниша", {})),
            "tone": get_select(props.get("Tone of Voice", {})),
            "status": get_select(props.get("Статус", {})),
            "audience": get_text(props.get("Основна аудитория", {})),
            "message": get_text(props.get("Бранд послание", {})),
            "forbidden": get_text(props.get("Забранени думи и теми", {})),
            "competitors": get_text(props.get("Конкуренти", {})),
            "color_palette": get_text(props.get("Цветова палитра", {})),
            "sample_tone": get_text(props.get("Примерни добри текстове", {})),
            "website_url": get_url(props.get("Website URL", {})),
            "fb_page": get_text(props.get("Facebook Page", {})),
            "token": get_text(find_prop(props, "Token")),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clients/<client_id>", methods=["PATCH"])
def update_client(client_id):
    data = request.json
    try:
        properties = {}
        # Select fields
        if data.get("niche"):
            properties["Ниша"] = {"select": {"name": data["niche"]}}
        if data.get("tone"):
            properties["Tone of Voice"] = {"select": {"name": data["tone"]}}
        # Multi-select channels
        if data.get("channels") is not None:
            properties["Активни канали"] = {
                "multi_select": [{"name": ch} for ch in data["channels"]]
            }
        # Rich text fields
        rt_map = {
            "Основна аудитория": data.get("audience", ""),
            "Бранд послание": data.get("message", ""),
            "Забранени думи и теми": data.get("forbidden", ""),
            "Конкуренти": data.get("competitors", ""),
            "Цветова палитра": data.get("color_palette", ""),
            "Примерни добри текстове": data.get("sample_tone", ""),
        }
        for notion_key, value in rt_map.items():
            if value is not None:
                properties[notion_key] = {"rich_text": [{"text": {"content": truncate(str(value))}}]}

        notion.pages.update(page_id=client_id, properties=properties)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Brand Auto-Analysis Endpoints ─────────────────────────────────────────────

@app.route("/api/brand/analyze-website", methods=["POST"])
def analyze_website():
    """Crawl website -> prose -> записва в .txt. Връща статус."""
    data = request.json
    website_url = data.get("website_url", "").strip()
    if not website_url:
        return jsonify({"error": "website_url е задължителен"}), 400

    from brand_analysis import analyze_website_for_brand
    try:
        result = analyze_website_for_brand(website_url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/brand/analyze-facebook", methods=["POST"])
def analyze_facebook():
    """Scrape FB -> prose -> append към .txt. Връща статус."""
    data = request.json
    fb_page = data.get("fb_page", "").strip()
    if not fb_page:
        return jsonify({"error": "fb_page е задължителен"}), 400

    from brand_analysis import analyze_facebook_for_brand
    try:
        result = analyze_facebook_for_brand(fb_page)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/brand/analyze-combined", methods=["POST"])
def analyze_combined():
    """Чете .txt с натрупаните анализи -> финален JSON extraction."""
    from brand_analysis import extract_combined_brand_json
    try:
        result = extract_combined_brand_json()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/brand/clear-analysis", methods=["POST"])
def clear_analysis():
    """Изтрива временния .txt файл (при отказ от онбординга)."""
    from brand_analysis import clear_analysis_file
    clear_analysis_file()
    return jsonify({"success": True})


@app.route("/api/brand/apply-analysis", methods=["POST"])
def apply_analysis():
    """Write analysis JSON fields to an existing Notion Brand Profile page."""
    data = request.json
    client_id = data.get("client_id")
    analysis = data.get("analysis", {})
    if not client_id or not analysis:
        return jsonify({"error": "client_id и analysis са задължителни"}), 400

    try:
        properties = {}
        if analysis.get("tone_of_voice"):
            properties["Tone of Voice"] = {"select": {"name": analysis["tone_of_voice"][:100]}}
        if analysis.get("niche"):
            properties["Ниша"] = {"select": {"name": analysis["niche"][:100]}}
        rt_map = {
            "Основна аудитория": analysis.get("audience", ""),
            "Бранд послание": analysis.get("brand_message", ""),
            "Забранени думи и теми": analysis.get("forbidden_words", ""),
            "Конкуренти": analysis.get("competitors", ""),
            "Цветова палитра": analysis.get("color_palette", ""),
            "Примерни добри текстове": analysis.get("sample_tone", ""),
        }
        for notion_key, value in rt_map.items():
            if value:
                properties[notion_key] = {"rich_text": [{"text": {"content": truncate(value)}}]}

        notion.pages.update(page_id=client_id, properties=properties)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Campaigns ─────────────────────────────────────────────────────────────────

@app.route("/api/campaigns", methods=["GET"])
def get_campaigns():
    try:
        results = notion.databases.query(
            database_id=NOTION_DB_HISTORY,
            sorts=[{"property": "Дата на генериране", "direction": "descending"}]
        ).get("results", [])
        campaigns = []
        for page in results:
            props = page.get("properties", {})
            campaigns.append({
                "id": page["id"],
                "title": get_title(props.get("Campaign Title", {})),
                "client": get_relation_name(props.get("Клиент", {})),
                "qa_score": get_number(props.get("QA Score", {})),
                "status": get_select(props.get("Статус", {})),
                "date": get_date(props.get("Дата на генериране", {})),
            })
        return jsonify(campaigns)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/campaigns/<campaign_id>", methods=["GET"])
def get_campaign_detail(campaign_id):
    try:
        page = notion.pages.retrieve(campaign_id)
        props = page.get("properties", {})
        return jsonify({
            "id": page["id"],
            "title": get_title(props.get("Campaign Title", {})),
            "client": get_relation_name(props.get("Клиент", {})),
            "client_id": get_relation_id(props.get("Клиент", {})),
            "qa_score": get_number(props.get("QA Score", {})),
            "status": get_select(props.get("Статус", {})),
            "date": get_date(props.get("Дата на генериране", {})),
            "strategy": get_text(props.get("Strategy", {})),
            "copy": get_text(props.get("Copy", {})),
            "visual": get_text(props.get("Visual", {})),
            "qa_report": get_text(props.get("QA Report", {})),
            "campaign_type": get_select(props.get("Campaign Type", {})) or get_text(props.get("Campaign Type", {})),
            "campaign_notes": get_text(props.get("Campaign Notes", {})),
            "channels": get_multi_select(props.get("Channels", {})) or get_multi_select(props.get("Канали", {})),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/campaigns/<campaign_id>/approve", methods=["POST"])
def approve_campaign(campaign_id):
    try:
        notion.pages.update(
            page_id=campaign_id,
            properties={"Статус": {"select": {"name": "Одобрено"}}}
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/campaigns/launch", methods=["POST"])
def launch_campaign():
    data = request.json
    try:
        resp = req_lib.post(MAKE_WEBHOOK_URL, json=data, timeout=10)
        return jsonify({"success": True, "status": resp.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Calendar Posts ───────────────────────────────────────────────────────────

def _serialize_calendar_post(page):
    props = page.get("properties", {})
    return {
        "id": page["id"],
        "title": get_title(props.get("Post Title", {})),
        "client": get_relation_name(props.get("Клиент", {})),
        "client_id": get_relation_id(props.get("Клиент", {})),
        "campaign_id": get_relation_id(props.get("Кампания", {})),
        "channel": get_select(props.get("Канал", {})),
        "date": get_date(props.get("Дата на публикуване", {})),
        "final_text": get_text(props.get("Финален текст", {})),
        "visual_prompt": get_text(props.get("Visual Prompt", {})),
        "status": get_select(props.get("Статус", {})),
        "client_comment": get_text(props.get("Клиентски коментар", {})),
    }


@app.route("/api/calendar-posts", methods=["GET"])
def get_calendar_posts():
    """List all calendar posts. Optional filters: ?month=YYYY-MM&client_id=..."""
    try:
        results = notion.databases.query(database_id=NOTION_DB_CALENDAR).get("results", [])
        posts = [_serialize_calendar_post(p) for p in results]

        month = request.args.get("month")
        if month:
            posts = [p for p in posts if p["date"] and p["date"].startswith(month)]

        client_id = request.args.get("client_id")
        if client_id:
            posts = [p for p in posts if p["client_id"] == client_id]

        return jsonify(posts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar-posts", methods=["POST"])
def create_calendar_posts():
    """
    Create one or more Calendar Posts from a Campaign (the 'Добави в календара' button).
    Payload: {
      client_id, campaign_id,
      posts: [ {channel, date, final_text, visual_prompt, title?}, ... ]
    }
    """
    data = request.json or {}
    client_id = data.get("client_id")
    campaign_id = data.get("campaign_id")
    posts = data.get("posts", [])
    if not client_id or not posts:
        return jsonify({"error": "client_id и posts са задължителни"}), 400

    created = []
    try:
        for p in posts:
            channel = p.get("channel", "")
            date_str = p.get("date", "")
            post_title = p.get("title") or f"{channel} — {date_str}".strip(" —")

            properties = {
                "Post Title": {"title": [{"text": {"content": post_title[:200]}}]},
                "Клиент": {"relation": [{"id": client_id}]},
                "Статус": {"select": {"name": "Чернова"}},
            }
            if campaign_id:
                properties["Кампания"] = {"relation": [{"id": campaign_id}]}
            if channel:
                properties["Канал"] = {"select": {"name": channel}}
            if date_str:
                properties["Дата на публикуване"] = {"date": {"start": date_str}}
            if p.get("final_text"):
                properties["Финален текст"] = {"rich_text": [{"text": {"content": truncate(p["final_text"])}}]}
            if p.get("visual_prompt"):
                properties["Visual Prompt"] = {"rich_text": [{"text": {"content": truncate(p["visual_prompt"])}}]}

            new_page = notion.pages.create(parent={"database_id": NOTION_DB_CALENDAR}, properties=properties)
            created.append(new_page["id"])

        return jsonify({"success": True, "created_ids": created})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar-posts/<post_id>", methods=["GET"])
def get_calendar_post_detail(post_id):
    try:
        page = notion.pages.retrieve(post_id)
        return jsonify(_serialize_calendar_post(page))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar-posts/<post_id>", methods=["PATCH"])
def update_calendar_post(post_id):
    """Edit text / date / channel from the Admin Calendar drawer."""
    data = request.json or {}
    try:
        properties = {}
        if data.get("channel"):
            properties["Канал"] = {"select": {"name": data["channel"]}}
        if data.get("date"):
            properties["Дата на публикуване"] = {"date": {"start": data["date"]}}
        if data.get("final_text") is not None:
            properties["Финален текст"] = {"rich_text": [{"text": {"content": truncate(data["final_text"])}}]}
        if data.get("visual_prompt") is not None:
            properties["Visual Prompt"] = {"rich_text": [{"text": {"content": truncate(data["visual_prompt"])}}]}
        if data.get("status"):
            properties["Статус"] = {"select": {"name": data["status"]}}

        notion.pages.update(page_id=post_id, properties=properties)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar-posts/send-for-approval", methods=["POST"])
def bulk_send_for_approval():
    """Bulk: payload {ids: [...]} -> Статус = 'Чака клиентско одобрение'."""
    data = request.json or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids е задължителен"}), 400
    try:
        for pid in ids:
            notion.pages.update(
                page_id=pid,
                properties={"Статус": {"select": {"name": "Чака клиентско одобрение"}}}
            )
        return jsonify({"success": True, "count": len(ids)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar-posts/<post_id>/send-for-approval", methods=["POST"])
def send_for_approval(post_id):
    try:
        notion.pages.update(
            page_id=post_id,
            properties={"Статус": {"select": {"name": "Чака клиентско одобрение"}}}
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Client Calendar (token-based, public) ───────────────────────────────────

def _resolve_client_by_token(token):
    """Find the Brand Profile page matching the given Token. Returns (id, name) or (None, None)."""
    if not token:
        return None, None
    try:
        results = notion.databases.query(
            database_id=NOTION_DB_BRAND,
            filter={"property": "Token", "rich_text": {"equals": token}}
        ).get("results", [])
        if not results:
            return None, None
        page = results[0]
        return page["id"], get_title(page.get("properties", {}).get("Клиент", {}))
    except Exception:
        return None, None


@app.route("/api/client/<token>/posts", methods=["GET"])
def client_get_posts(token):
    client_id, client_name = _resolve_client_by_token(token)
    if not client_id:
        return jsonify({"error": "Невалиден или липсващ token"}), 403
    try:
        results = notion.databases.query(database_id=NOTION_DB_CALENDAR).get("results", [])
        posts = [_serialize_calendar_post(p) for p in results]
        visible_statuses = {"Чака клиентско одобрение", "Одобрено"}
        posts = [p for p in posts if p["client_id"] == client_id and p["status"] in visible_statuses]
        return jsonify({"client_name": client_name, "posts": posts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/client/<token>/posts/<post_id>/approve", methods=["POST"])
def client_approve_post(token, post_id):
    client_id, _ = _resolve_client_by_token(token)
    if not client_id:
        return jsonify({"error": "Невалиден или липсващ token"}), 403
    try:
        page = notion.pages.retrieve(post_id)
        post_client_id = get_relation_id(page.get("properties", {}).get("Клиент", {}))
        if post_client_id != client_id:
            return jsonify({"error": "Достъпът отказан"}), 403

        notion.pages.update(
            page_id=post_id,
            properties={"Статус": {"select": {"name": "Одобрено"}}}
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/client/<token>/posts/<post_id>/request-fix", methods=["POST"])
def client_request_fix(token, post_id):
    client_id, _ = _resolve_client_by_token(token)
    if not client_id:
        return jsonify({"error": "Невалиден или липсващ token"}), 403
    data = request.json or {}
    comment = data.get("comment", "").strip()
    if not comment:
        return jsonify({"error": "comment е задължителен"}), 400
    try:
        page = notion.pages.retrieve(post_id)
        post_client_id = get_relation_id(page.get("properties", {}).get("Клиент", {}))
        if post_client_id != client_id:
            return jsonify({"error": "Достъпът отказан"}), 403

        notion.pages.update(
            page_id=post_id,
            properties={
                "Статус": {"select": {"name": "Иска корекция"}},
                "Клиентски коментар": {"rich_text": [{"text": {"content": truncate(comment)}}]},
            }
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Debug ─────────────────────────────────────────────────────────────────────

@app.route("/api/brand/debug-website", methods=["POST"])
def debug_website_analysis():
    """Returns both prose and JSON for debugging."""
    data = request.json
    website_url = data.get("website_url", "").strip()
    if not website_url:
        return jsonify({"error": "website_url required"}), 400

    from brand_analysis import (_sanitize, _step1_prose_analysis,
                                 _step2_extract_json, FIRECRAWL_API_KEY)
    import requests as r
    import time

    headers = {
        "Authorization": "Bearer " + FIRECRAWL_API_KEY,
        "Content-Type": "application/json"
    }
    try:
        crawl = r.post("https://api.firecrawl.dev/v1/crawl",
            json={"url": website_url, "limit": 10,
                  "scrapeOptions": {"formats": ["markdown"]}},
            headers=headers, timeout=30)
        crawl.raise_for_status()
        job_id = crawl.json().get("id")
        status_url = "https://api.firecrawl.dev/v1/crawl/" + job_id
        status_data = {}
        for _ in range(60):
            time.sleep(3)
            status_data = r.get(status_url, headers=headers, timeout=15).json()
            if status_data.get("status") == "completed":
                break

        combined = ""
        for page in status_data.get("data", []):
            title = page.get("metadata", {}).get("title", "")
            markdown = page.get("markdown", "")[:1500]
            combined += "\n--- " + title + " ---\n" + markdown
        combined = _sanitize(combined, max_chars=7000)

        prose = _step1_prose_analysis(combined, "sayt")
        json_result = _step2_extract_json(prose)

        return jsonify({
            "raw_text_length": len(combined),
            "prose_analysis": prose,
            "json_result": json_result
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug/<page_id>", methods=["GET"])
def debug_page(page_id):
    try:
        page = notion.pages.retrieve(page_id)
        props = page.get("properties", {})
        debug = {k: {"type": v.get("type"), "preview": str(v)[:300]} for k, v in props.items()}
        return jsonify(debug)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/clients-token", methods=["GET"])
def debug_clients_token():
    """Quick check: returns name + Token value for every Brand Profile."""
    try:
        results = notion.databases.query(database_id=NOTION_DB_BRAND).get("results", [])
        out = []
        for page in results:
            props = page.get("properties", {})
            raw = find_prop(props, "Token")
            out.append({
                "name": get_title(props.get("Клиент", {})),
                "token_parsed": get_text(raw),
                "prop_keys": list(props.keys())[:8],
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
