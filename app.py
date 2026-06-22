from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from notion_client import Client
from dotenv import load_dotenv
import requests
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
    """Extract plain text from a Notion rich_text property (handles multi-block)."""
    try:
        parts = []
        for t in prop.get("rich_text", []):
            # Use plain_text if available, otherwise content
            text = t.get("plain_text") or t.get("text", {}).get("content", "")
            parts.append(text)
        return "".join(parts)
    except Exception:
        return ""

def get_select(prop):
    """Extract name from a Notion select property."""
    try:
        return prop.get("select", {}).get("name", "") or ""
    except Exception:
        return ""

def get_title(prop):
    """Extract plain text from a Notion title property."""
    try:
        return "".join(t["plain_text"] for t in prop.get("title", []))
    except Exception:
        return ""

def get_number(prop):
    """Extract number from a Notion number property."""
    try:
        return prop.get("number") or 0
    except Exception:
        return 0

def get_date(prop):
    """Extract start date string from a Notion date property."""
    try:
        return (prop.get("date") or {}).get("start", "") or ""
    except Exception:
        return ""

def get_relation_name(prop):
    """Get display name from a relation property (returns first related page title)."""
    try:
        relations = prop.get("relation", [])
        if not relations:
            return ""
        page_id = relations[0]["id"]
        page = notion.pages.retrieve(page_id)
        props = page.get("properties", {})
        for p in props.values():
            if p.get("type") == "title":
                return get_title(p)
        return ""
    except Exception:
        return ""


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
            })
        return jsonify(clients)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clients", methods=["POST"])
def create_client():
    data = request.json
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DB_BRAND},
            properties={
                "Клиент": {"title": [{"text": {"content": data.get("name", "")}}]},
                "Ниша": {"select": {"name": data.get("niche", "")}},
                "Tone of Voice": {"select": {"name": data.get("tone", "")}},
                "Основна аудитория": {"rich_text": [{"text": {"content": data.get("audience", "")}}]},
                "Бранд послание": {"rich_text": [{"text": {"content": data.get("message", "")}}]},
                "Забранени думи и теми": {"rich_text": [{"text": {"content": data.get("forbidden", "")}}]},
                "Статус": {"select": {"name": "Активен"}},
            }
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        resp = requests.post(MAKE_WEBHOOK_URL, json=data, timeout=10)
        return jsonify({"success": True, "status": resp.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Run ──────────────────────────────────────────────────────────────────────
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
