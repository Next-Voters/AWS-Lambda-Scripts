import os
import json
import html
import logging
from datetime import date
from collections import defaultdict
from urllib.parse import quote

import boto3
from supabase import create_client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SES_SENDER_EMAIL = os.environ["SES_SENDER_EMAIL"]
SHARE_BASE_URL = os.environ["SHARE_BASE_URL"]

ses_client = boto3.client("sesv2")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "email_report.html")
with open(TEMPLATE_PATH, "r") as f:
    EMAIL_TEMPLATE = f.read()

PAGE_SIZE = 1000


def fetch_subscriptions():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    subscribers = []
    offset = 0

    while True:
        response = (
            supabase.table("subscriptions")
            .select("contact, city, subscription_topics(topic_id, supported_topics(topic_name))")
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

            subscribers.append({
                "contact": row["contact"],
                "city": row["city"],
                "topic_ids": topic_ids,
                "topic_names": topic_names,
            })

        if len(response.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return subscribers


def fetch_todays_reports():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    today = date.today().isoformat()
    reports_cache = {}
    offset = 0

    while True:
        response = (
            supabase.table("reports")
            .select("city, topic_id, items")
            .eq("report_date", today)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )

        for row in response.data:
            key = (row["city"], row["topic_id"])
            reports_cache.setdefault(key, []).extend(row["items"])

        if len(response.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return reports_cache


def build_table_of_contents(topic_names):
    rows = []
    for name in topic_names:
        anchor = name.lower().replace(" ", "-")
        rows.append(
            f'<tr><td style="padding: 4px 35px;">'
            f'<a href="#{anchor}" style="font-family: \'DM Sans\', Arial, sans-serif; '
            f'font-size: 14px; color: #1A1A1A; text-decoration: none; font-weight: 500;">'
            f'&#8226; {name.title()}</a></td></tr>'
        )
    return "\n".join(rows)


def build_topic_sections(reports_for_subscriber, topic_names_map):
    sections = []
    for topic_id, items in reports_for_subscriber.items():
        topic_name = topic_names_map.get(topic_id, "Topic")
        anchor = topic_name.lower().replace(" ", "-")

        section_html = (
            f'<tr><td style="padding: 24px 35px 8px 35px;">'
            f'<h2 id="{anchor}" style="font-family: \'Bebas Neue\', Arial, sans-serif; '
            f'font-size: 22px; color: #1A1A1A; margin: 0; letter-spacing: 1px; '
            f'text-transform: uppercase;">{topic_name.title()}</h2>'
            f'</td></tr>'
        )

        for item in items:
            headline = html.escape(item.get("headline", item.get("title", "")))
            summary = html.escape(item.get("summary", item.get("body", "")))
            source = html.escape(item.get("source", ""))

            item_html = (
                f'<tr><td style="padding: 8px 35px;">'
                f'<p style="font-family: \'DM Sans\', Arial, sans-serif; font-size: 15px; '
                f'color: #1A1A1A; margin: 0 0 4px 0; font-weight: 700;">{headline}</p>'
                f'<p style="font-family: \'DM Sans\', Arial, sans-serif; font-size: 14px; '
                f'color: #444444; margin: 0; line-height: 1.5;">{summary}</p>'
            )
            if source:
                item_html += (
                    f'<p style="font-family: \'DM Sans\', Arial, sans-serif; font-size: 11px; '
                    f'color: #888888; margin: 4px 0 0 0;">Source: {source}</p>'
                )
            item_html += '</td></tr>'
            section_html += item_html

        sections.append(section_html)

    return "\n".join(sections)


def build_email_html(city, topic_sections_html, toc_html):
    today = date.today()
    header_date = f"{today.strftime('%B %d, %Y').upper()} | {city.upper()}"
    city_header = f"What's new in {city}?"

    share_text = quote(f"Check out today's local policy brief for {city} from Next Voters!", safe="")
    share_url = quote(f"{SHARE_BASE_URL}/{city.lower().replace(' ', '-')}", safe="")

    twitter_share = f"https://twitter.com/intent/tweet?text={share_text}&url={share_url}"
    facebook_share = f"https://www.facebook.com/sharer/sharer.php?u={share_url}"
    linkedin_share = f"https://www.linkedin.com/sharing/share-offsite/?url={share_url}"

    rendered = EMAIL_TEMPLATE
    rendered = rendered.replace("{{CITY_HEADER}}", city_header)
    rendered = rendered.replace("{{HEADER_DATE}}", header_date)
    rendered = rendered.replace("{{TABLE_OF_CONTENTS}}", toc_html)
    rendered = rendered.replace("{{TOPIC_SECTIONS}}", topic_sections_html)
    rendered = rendered.replace("{{CONTENT}}", "")
    rendered = rendered.replace("{{TWITTER_SHARE_URL}}", twitter_share)
    rendered = rendered.replace("{{FACEBOOK_SHARE_URL}}", facebook_share)
    rendered = rendered.replace("{{LINKEDIN_SHARE_URL}}", linkedin_share)
    rendered = rendered.replace("{{UNSUBSCRIBE_URL}}", "https://nextvoters.com/local")
    rendered = rendered.replace(
        ">Unsubscribe</a> from this list.",
        ">Manage Subscription</a>",
    )

    return rendered


def send_bulk_emails(subject, html_body, recipients):
    success_count = 0
    failures = []

    for i in range(0, len(recipients), 50):
        batch = recipients[i:i + 50]
        entries = [
            {
                "Destination": {"ToAddresses": [email]},
            }
            for email in batch
        ]

        try:
            response = ses_client.send_bulk_email(
                FromEmailAddress=SES_SENDER_EMAIL,
                DefaultContent={
                    "Template": {
                        "TemplateContent": {
                            "Subject": {"Data": subject},
                            "Html": {"Data": html_body},
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
        key = (sub["city"], frozenset(sub["topic_ids"]))
        groups[key].append(sub)

    total_sent = 0
    total_failures = []

    for (city, topic_ids_frozen), group_subs in groups.items():
        topic_ids = sorted(topic_ids_frozen)
        topic_names_map = {
            sub["topic_ids"][i]: sub["topic_names"][i]
            for sub in group_subs[:1]
            for i in range(len(sub["topic_ids"]))
        }

        reports_for_group = {}
        for tid in topic_ids:
            key = (city, tid)
            if key in reports_cache:
                reports_for_group[tid] = reports_cache[key]

        if not reports_for_group:
            continue

        topic_names = [topic_names_map[tid] for tid in reports_for_group]
        toc_html = build_table_of_contents(topic_names)
        topic_sections_html = build_topic_sections(reports_for_group, topic_names_map)
        html_body = build_email_html(city, topic_sections_html, toc_html)

        subject = f"What's new in {city}? - {date.today().strftime('%B %d, %Y')}"
        recipients = [sub["contact"] for sub in group_subs]

        sent, failures = send_bulk_emails(subject, html_body, recipients)
        total_sent += sent
        total_failures.extend(failures)
        logger.info(f"Sent {sent} emails for group ({city}, {topic_ids})")

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
