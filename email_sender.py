import os
import json
import html
import logging
from datetime import date
from collections import defaultdict
from urllib.parse import quote, urlparse

import boto3
from supabase import create_client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SES_SENDER_EMAIL = os.environ["SES_SENDER_EMAIL"]
SHARE_BASE_URL = "https://www.nextvoters.com"

ses_client = boto3.client("sesv2")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "email_report.html")
with open(TEMPLATE_PATH, "r") as f:
    EMAIL_TEMPLATE = f.read()

PAGE_SIZE = 1000

STYLES = {
    "heading": (
        "font-family: 'Bebas Neue', Arial, sans-serif; color: #1A1A1A; "
        "margin: 0; letter-spacing: 1px; text-transform: uppercase"
    ),
    "body": "font-family: 'DM Sans', Arial, sans-serif; font-size: 15px; color: #1A1A1A",
    "body_sm": "font-family: 'DM Sans', Arial, sans-serif; font-size: 14px; color: #444444",
    "citation": (
        "font-family: 'DM Sans', Arial, sans-serif; font-size: 12px; "
        "color: #666666; margin: 0; line-height: 1.6"
    ),
    "accent": "#E63946",
}


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _row(padding, content):
    return f'<tr><td style="padding: {padding};">{content}</td></tr>'


def _divider(padding="0 35px"):
    return (
        f'<tr><td style="padding: {padding};">'
        '<table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">'
        '<tr><td style="height: 1px; background-color: #E0E0E0;"></td></tr>'
        '</table></td></tr>'
    )


def _heading(text, font_size=22, padding="24px 35px 8px 35px", extra_style=""):
    anchor = text.lower().replace(" ", "-")
    return _row(
        padding,
        f'<h2 id="{anchor}" style="{STYLES["heading"]}; font-size: {font_size}px; '
        f'{extra_style}">{html.escape(text)}</h2>',
    )


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_subscriptions():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    subscribers = []
    offset = 0

    while True:
        response = (
            supabase.table("subscriptions")
            .select(
                "contact, subscription_regions(city, region, country), "
                "subscription_topics(topic_id, supported_topics(topic_name))"
            )
            .eq("stripe_status", "active")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )

        for row in response.data:
            topic_ids = []
            topic_names = []
            for st in row.get("subscription_topics", []):
                topic_id = st["topic_id"]
                topic_name = st.get("supported_topics", {}).get("topic_name", "")
                if not topic_name:
                    continue
                topic_ids.append(topic_id)
                topic_names.append(topic_name)

            sr = row.get("subscription_regions") or {}
            areas = []
            for field in ("city", "region", "country"):
                val = sr.get(field)
                if val:
                    areas.append(val)

            subscribers.append({
                "contact": row["contact"],
                "areas": areas,
                "topic_ids": topic_ids,
                "topic_names": topic_names,
            })

        if len(response.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return subscribers


def fetch_todays_reports():
    """Returns a dict keyed by (region, topic_id) -> list of report items."""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    today = date.today().isoformat()
    reports_cache = {}
    offset = 0

    while True:
        response = (
            supabase.table("report_headers")
            .select("topic_id, header, bullets, sources, reports!inner(region, report_date)")
            .eq("reports.report_date", today)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )

        for row in response.data:
            region = row["reports"]["region"]
            topic_id = row["topic_id"]
            key = (region, topic_id)
            reports_cache.setdefault(key, []).append({
                "header": row["header"],
                "bullets": row.get("bullets", []),
                "sources": row.get("sources", []),
            })

        if len(response.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return reports_cache


# ── Email building ────────────────────────────────────────────────────────────

def build_topic_sections(reports_for_subscriber, topic_names_map, seen_urls=None, citations=None):
    """Build HTML for each topic. Shares citation state across calls for dedup."""
    sections = []
    if seen_urls is None:
        seen_urls = {}
    if citations is None:
        citations = []

    for topic_id, items in reports_for_subscriber.items():
        topic_name = topic_names_map.get(topic_id, "Topic")
        section_html = _heading(topic_name)

        for item in items:
            header_text = html.escape(item.get("header", ""))
            bullets = item.get("bullets", [])
            sources = item.get("sources", [])

            cite_nums = []
            for url in sources:
                if url not in seen_urls:
                    citations.append({"url": url})
                    seen_urls[url] = len(citations)
                cite_nums.append(seen_urls[url])

            refs_html = ""
            if cite_nums:
                refs = "".join(f"[{n}]" for n in cite_nums)
                refs_html = (
                    f'<sup style="font-size: 10px; color: {STYLES["accent"]}; '
                    f'margin-left: 2px;">{refs}</sup>'
                )

            bullets_html = ""
            if bullets:
                lis = "".join(
                    f'<li style="margin-bottom: 2px;">{html.escape(str(b))}</li>'
                    for b in bullets
                )
                bullets_html = (
                    f'<ul style="{STYLES["body_sm"]}; margin: 4px 0 0 0; '
                    f'padding-left: 20px; line-height: 1.5;">{lis}</ul>'
                )

            section_html += _row(
                "8px 35px",
                f'<p style="{STYLES["body"]}; margin: 0 0 4px 0; font-weight: 700;">'
                f'{header_text}{refs_html}</p>{bullets_html}',
            )

        sections.append(section_html)

    return "\n".join(sections), citations


def _display_domain(url):
    try:
        domain = urlparse(url).netloc
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or url
    except Exception:
        return url


def build_citations_section(citations):
    if not citations:
        return ""

    result = _divider() + _heading("Sources", font_size=18, padding="20px 35px 8px 35px")

    for i, cite in enumerate(citations, 1):
        url = cite.get("url", "")
        if not url:
            continue
        label = html.escape(_display_domain(url))
        safe_url = html.escape(url)
        link = (
            f'<a href="{safe_url}" target="_blank" '
            f'style="color: {STYLES["accent"]}; text-decoration: underline;">{label}</a>'
        )
        result += _row(
            "2px 35px",
            f'<p style="{STYLES["citation"]}">'
            f'<span style="color: {STYLES["accent"]}; font-weight: 600;">[{i}]</span> {link}</p>',
        )

    result += _row("0 0 12px 0", "")
    return result


def build_area_header(area_name):
    return _divider("24px 35px 0 35px") + _heading(
        area_name, font_size=26, padding="16px 35px 0 35px",
        extra_style="letter-spacing: 1.5px;",
    )


def format_areas_label(areas):
    if not areas:
        return ""
    if len(areas) == 1:
        return areas[0]
    if len(areas) == 2:
        return f"{areas[0]} and {areas[1]}"
    return ", ".join(areas[:-1]) + f", and {areas[-1]}"


def build_email_html(areas, topic_sections_html, citations_html):
    today = date.today()
    areas_label = format_areas_label(areas)
    share_text = quote("Check out today's local policy brief from Next Voters!", safe="")
    share_url = quote(f"{SHARE_BASE_URL}/local", safe="")

    replacements = {
        "{{CITY_HEADER}}": f"What's new in {areas_label}?",
        "{{HEADER_DATE}}": f"{today.strftime('%B %d, %Y').upper()} | {', '.join(a.upper() for a in areas)}",
        "{{TABLE_OF_CONTENTS}}": "",
        "{{TOPIC_SECTIONS}}": topic_sections_html,
        "{{CONTENT}}": "",
        "{{CITATIONS}}": citations_html,
        "{{TWITTER_SHARE_URL}}": f"https://twitter.com/intent/tweet?text={share_text}&url={share_url}",
        "{{FACEBOOK_SHARE_URL}}": f"https://www.facebook.com/sharer/sharer.php?u={share_url}",
        "{{LINKEDIN_SHARE_URL}}": f"https://www.linkedin.com/sharing/share-offsite/?url={share_url}",
        "{{UNSUBSCRIBE_URL}}": "https://nextvoters.com/local",
        ">Unsubscribe</a> from this list.": ">Manage Subscription</a>",
    }

    rendered = EMAIL_TEMPLATE
    for key, val in replacements.items():
        rendered = rendered.replace(key, val)
    return rendered


# ── Email sending ─────────────────────────────────────────────────────────────

def send_bulk_emails(subject, html_body, recipients):
    success_count = 0
    failures = []

    for i in range(0, len(recipients), 50):
        batch = recipients[i:i + 50]
        entries = [
            {"Destination": {"ToAddresses": [email]}}
            for email in batch
        ]

        try:
            response = ses_client.send_bulk_email(
                FromEmailAddress=SES_SENDER_EMAIL,
                DefaultContent={
                    "Template": {
                        "TemplateContent": {
                            "Subject": subject,
                            "Html": html_body,
                        },
                        "TemplateData": "{}",
                    }
                },
                BulkEmailEntries=entries,
            )

            for idx, result in enumerate(response.get("BulkEmailEntryResults", [])):
                if result["Status"] == "SUCCESS":
                    success_count += 1
                else:
                    failures.append({
                        "email": batch[idx],
                        "error": result.get("Error", "Unknown"),
                    })

        except Exception as e:
            logger.error(f"SES bulk send failed for batch starting at index {i}: {e}")
            failures.extend({"email": email, "error": str(e)} for email in batch)

    return success_count, failures


def _build_group_email(areas, topic_ids, topic_names_map, reports_cache):
    """Assemble the HTML email for a subscriber group.

    Returns (subject, html_body) or None if no reports match any area.
    """
    seen_urls = {}
    citations = []
    all_sections = []

    for area in areas:
        reports_for_area = {
            tid: reports_cache[(area, tid)]
            for tid in topic_ids
            if (area, tid) in reports_cache
        }
        if not reports_for_area:
            continue

        topic_html, citations = build_topic_sections(
            reports_for_area, topic_names_map, seen_urls, citations
        )
        all_sections.append(build_area_header(area) + topic_html)

    if not all_sections:
        return None

    citations_html = build_citations_section(citations)
    html_body = build_email_html(list(areas), "\n".join(all_sections), citations_html)
    subject = (
        f"What's new in {format_areas_label(list(areas))}? - "
        f"{date.today().strftime('%B %d, %Y')}"
    )
    return subject, html_body


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    subscribers = fetch_subscriptions()
    logger.info(f"Fetched {len(subscribers)} active subscribers")

    reports_cache = fetch_todays_reports()
    logger.info(f"Fetched {len(reports_cache)} report entries for {date.today().isoformat()}")

    if not reports_cache:
        logger.info("No reports for today, skipping email send")
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No reports for today", "sent": 0}),
        }

    groups = defaultdict(list)
    for sub in subscribers:
        key = (tuple(sub["areas"]), frozenset(sub["topic_ids"]))
        groups[key].append(sub)

    total_sent = 0
    total_failures = []

    for (areas, topic_ids_frozen), group_subs in groups.items():
        topic_names_map = {
            sub["topic_ids"][i]: sub["topic_names"][i]
            for sub in group_subs[:1]
            for i in range(len(sub["topic_ids"]))
        }

        result = _build_group_email(
            areas, sorted(topic_ids_frozen), topic_names_map, reports_cache
        )
        if not result:
            continue

        subject, html_body = result
        recipients = [sub["contact"] for sub in group_subs]
        sent, failures = send_bulk_emails(subject, html_body, recipients)
        total_sent += sent
        total_failures.extend(failures)
        logger.info(f"Sent {sent} emails for group ({list(areas)}, {sorted(topic_ids_frozen)})")

    status_code = 200 if not total_failures else 207
    return {
        "statusCode": status_code,
        "body": json.dumps({
            "total_subscribers": len(subscribers),
            "sent": total_sent,
            "failed": len(total_failures),
            "failures": total_failures[:50],
        }),
    }
