"""Moor alert-sink — a real webhook receiver.

Moor's alerter posts Slack incoming-webhook format cards. In production
that URL is a Slack/Teams/PagerDuty endpoint; in the demo it is this
sink, which receives the exact same HTTP payloads and renders them the
way a chat client would. Nothing about the alerting path is mocked —
swap the webhook URL and the same cards land in your channel.
"""
from __future__ import annotations

import html
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Moor alert sink", version="1.0.0")

ALERTS: list[dict] = []
MAX_ALERTS = 200


@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    ALERTS.append({"received_at": time.time(), "payload": payload})
    if len(ALERTS) > MAX_ALERTS:
        del ALERTS[:-MAX_ALERTS]
    return {"ok": True, "received": len(ALERTS)}


@app.get("/api/alerts")
async def list_alerts():
    return {"count": len(ALERTS), "alerts": ALERTS}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    cards = []
    for alert in reversed(ALERTS[-40:]):
        payload = alert["payload"]
        received = time.strftime("%H:%M:%S", time.localtime(alert["received_at"]))
        attachments = payload.get("attachments") or []
        body = ""
        for att in attachments:
            color = att.get("color", "#4da8da")
            title = html.escape(str(att.get("title", "")))
            text = html.escape(str(att.get("text", ""))).replace("\n", "<br>")
            fields = "".join(
                f'<div class="field"><span class="fk">{html.escape(str(f.get("title", "")))}</span>'
                f'<span class="fv">{html.escape(str(f.get("value", "")))}</span></div>'
                for f in att.get("fields", [])
            )
            body += f"""
            <div class="card">
              <div class="bar" style="background:{html.escape(color)}"></div>
              <div class="content">
                <div class="card-top">
                  <span class="bot">{html.escape(str(payload.get("username", "Moor")))}</span>
                  <span class="ts">{received}</span>
                </div>
                <div class="card-title">{html.escape(str(payload.get("text", "")))}</div>
                <div class="card-text">{text}</div>
                <div class="fields">{fields}</div>
              </div>
            </div>"""
    if not body:
        body = '<div class="empty">No alerts received yet.<br>Inject drift with <code>make chaos</code> and watch cards land here.</div>'
    return PAGE.replace("<!--CARDS-->", body)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Moor Alert Sink</title>
<style>
  :root { --bg:#0a1628; --panel:#101d31; --line:#1e3350; --accent:#4da8da;
          --text:#e8f0f8; --muted:#7a9bb8;
          --mono: ui-monospace, Menlo, Consolas, monospace; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text);
         font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; padding: 26px; }
  h1 { font-size: 18px; letter-spacing: 3px; }
  h1 span { color: var(--muted); font-size: 12px; font-weight: 400; letter-spacing: 1px; }
  p.sub { color: var(--muted); font-size: 12.5px; margin: 8px 0 22px; line-height: 1.6; }
  code { font-family: var(--mono); color: var(--accent); }
  .card { display: flex; background: var(--panel); border: 1px solid var(--line);
          border-radius: 8px; margin-bottom: 14px; overflow: hidden; max-width: 720px; }
  .bar { width: 6px; flex-shrink: 0; }
  .content { padding: 14px 18px; flex: 1; }
  .card-top { display: flex; justify-content: space-between; margin-bottom: 6px; }
  .bot { font-weight: 700; font-size: 13px; }
  .ts { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .card-title { font-weight: 700; margin-bottom: 6px; font-size: 14px; }
  .card-text { color: #b8ccde; font-size: 13px; line-height: 1.6; }
  .fields { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
  .field { min-width: 140px; }
  .fk { display: block; color: var(--muted); font-size: 10px; text-transform: uppercase;
        letter-spacing: 1px; margin-bottom: 2px; }
  .fv { font-family: var(--mono); font-size: 12px; }
  .empty { color: var(--muted); font-size: 13px; line-height: 1.8; }
  a { color: var(--accent); }
</style>
</head>
<body>
  <h1>ALERT SINK <span>· webhook receiver</span></h1>
  <p class="sub">
    This endpoint receives the exact Slack-format webhook cards Moor emits
    (<code>POST /webhook</code>). In production you would point
    <code>MOOR_ALERT_WEBHOOK</code> at your Slack/Teams/PagerDuty URL —
    the payload is identical. This page auto-refreshes every 3s.
  </p>
  <!--CARDS-->
  <script>setTimeout(() => location.reload(), 3000);</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9099, log_level="warning")
