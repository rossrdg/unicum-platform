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
        return prop.get("url") or ""
    except Exception:
        return ""

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

def truncate(text, limit=1900):
    """Truncate to Notion's 2000-char rich_text limit with safety margin."""
    return text[:limit] if text else ""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/clients", methods=["GET"])
def get_clients():
    try:
        results = notion.databases.query(database_id=NOTION_DB_BRAND).get("results", [])
        clients = []
        for page in results:
            props = page.get("properties", {})
            clients.append({
                "id": page["id"],
                "name": get_title(props.get("Клиент", {})),
                "niche": get_select(props.get("Ниша", {})),
                "tone": get_select(props.get("Tone of Voice", {})),
                "status": get_select(props.get("Статус", {})),
                "audience": get_text(props.get("Основна аудитория", {})),
                "message": get_text(props.get("Бранд послание", {})),
                "website_url": get_url(props.get("Website URL", {})),
                "fb_page": get_text(props.get("Facebook Page", {})),
            })
        return jsonify(clients)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clients", methods=["POST"])
def create_client():
    data = request.json
    try:
        properties = {
            "Клиент": {"title": [{"text": {"content": data.get("name", "")}}]},
            "Ниша": {"select": {"name": data.get("niche", "")}},
            "Tone of Voice": {"select": {"name": data.get("tone", "")}},
            "Основна аудитория": {"rich_text": [{"text": {"content": data.get("audience", "")}}]},
            "Бранд послание": {"rich_text": [{"text": {"content": data.get("message", "")}}]},
            "Забранени думи и теми": {"rich_text": [{"text": {"content": data.get("forbidden", "")}}]},
            "Статус": {"select": {"name": "Активен"}},
        }
        # Add optional URL fields only if provided
        if data.get("website_url"):
            properties["Website URL"] = {"url": data["website_url"]}
        if data.get("fb_page"):
            properties["Facebook Page"] = {"rich_text": [{"text": {"content": data["fb_page"]}}]}

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
            "website_url": get_url(props.get("Website URL", {})),
            "fb_page": get_text(props.get("Facebook Page", {})),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clients/<client_id>", methods=["PATCH"])
def update_client(client_id):
    data = request.json
    try:
        properties = {}
        field_map = {
            "tone": ("Tone of Voice", "select"),
            "niche": ("Ниша", "select"),
            "audience": ("Основна аудитория", "rich_text"),
            "message": ("Бранд послание", "rich_text"),
            "forbidden": ("Забранени думи и теми", "rich_text"),
        }
        for key, (notion_key, prop_type) in field_map.items():
            if key in data:
                if prop_type == "select":
                    properties[notion_key] = {"select": {"name": data[key]}}
                else:
                    properties[notion_key] = {"rich_text": [{"text": {"content": truncate(data[key])}}]}

        if "website_url" in data:
            properties["Website URL"] = {"url": data["website_url"] or None}
        if "fb_page" in data:
            properties["Facebook Page"] = {"rich_text": [{"text": {"content": data["fb_page"]}}]}

        notion.pages.update(page_id=client_id, properties=properties)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Brand Auto-Analysis Endpoints ─────────────────────────────────────────────

@app.route("/api/brand/analyze-website", methods=["POST"])
def analyze_website():
    """Crawl website with Firecrawl, analyze with Gemini, return brand JSON."""
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
    """Scrape FB page with Apify, analyze with Gemini, return brand JSON."""
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
            # Map to select — use first word if free-form
            properties["Tone of Voice"] = {"select": {"name": analysis["tone_of_voice"][:100]}}
        if analysis.get("audience"):
            properties["Основна аудитория"] = {"rich_text": [{"text": {"content": truncate(analysis["audience"])}}]}
        if analysis.get("brand_message"):
            properties["Бранд послание"] = {"rich_text": [{"text": {"content": truncate(analysis["brand_message"])}}]}
        if analysis.get("forbidden_words"):
            properties["Забранени думи и теми"] = {"rich_text": [{"text": {"content": truncate(analysis["forbidden_words"])}}]}
        if analysis.get("niche"):
            properties["Ниша"] = {"select": {"name": analysis["niche"][:100]}}

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
            "qa_score": get_number(props.get("QA Score", {})),
            "status": get_select(props.get("Статус", {})),
            "date": get_date(props.get("Дата на генериране", {})),
            "strategy": get_text(props.get("Strategy", {})),
            "copy": get_text(props.get("Copy", {})),
            "visual": get_text(props.get("Visual", {})),
            "qa_report": get_text(props.get("QA Report", {})),
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


# ── Debug ─────────────────────────────────────────────────────────────────────

@app.route("/api/debug/<page_id>", methods=["GET"])
def debug_page(page_id):
    try:
        page = notion.pages.retrieve(page_id)
        props = page.get("properties", {})
        debug = {k: {"type": v.get("type"), "preview": str(v)[:300]} for k, v in props.items()}
        return jsonify(debug)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
