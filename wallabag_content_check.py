#!/usr/bin/env python3

import sys
import os
import argparse
import logging
import configparser
import smtplib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urlparse
import json
import requests

from wallabag_tool import oauth_token_password_grant, html_to_text, parse_entry_created_at

"""
Scans recently-added Wallabag entries for articles that came in without real
content (Wallabag failed to fetch the page: paywall, user-agent block, or
JS-rendered site). Flags suspects using cheap local checks first, falling
back to an LLM call only for ambiguous cases, then emails a report.

Reads the same ~/.wallabag config file as wallabag_tool.py, plus a new
[EMAIL] section for SMTP settings:

[EMAIL]
SMTP_HOST = smtp.gmail.com
SMTP_PORT = 587
SMTP_USER = you@gmail.com
SMTP_PASS = app-password-here
MAIL_FROM = you@gmail.com
MAIL_TO = you@yourdomain
"""

DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOGGING_FORMAT = '%(asctime)s:%(levelname)s:%(message)s'

# Cheap, case-insensitive substring checks for known error/paywall/placeholder
# boilerplate. Extend this list as new patterns are noticed.
INDICATOR_STRINGS = [
    "wallabag can't retrieve contents for this article",
    "please verify you are a human",
    "checking your browser before accessing",
    "enable javascript to continue",
    "please enable javascript",
    "subscribe to continue reading",
    "create a free account to continue reading",
    "sign in to continue reading",
    "access to this page has been denied",
]

# Domains for short-form content (tweets/posts) that wallabag_tool.py saves
# deliberately short via its --twitter/--facebook/--linkedin modes. These are
# expected to be brief, so the word-count heuristic shouldn't apply to them.
SHORT_FORM_DOMAINS = {"x.com", "twitter.com", "facebook.com", "linkedin.com"}


def main():
    parser = argparse.ArgumentParser(description='Wallabag Missing-Content Checker')
    parser.add_argument("-v", action="store_true", default=False, help="Print extra info")
    parser.add_argument("-vv", action="store_true", default=False, help="Print (more) extra info")
    parser.add_argument('-c', nargs='?', type=str,
                        default=os.path.join(str(Path.home()), ".wallabag"),
                        help='Config file (Default: ~/.wallabag)')
    parser.add_argument("--days", type=int, default=2,
                        help="Lookback window in days for 'recently added' entries (default: 2)")
    parser.add_argument("--word-threshold", type=int, default=200, dest="word_threshold",
                        help="Articles below this word count are flagged without an LLM call (default: 200)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Print the report to stdout instead of emailing it")
    parser.add_argument("--model", type=str, default=None,
                        help="Override the OpenAI model used for classification")

    args = parser.parse_args()

    ######################################
    # Establish LOGLEVEL
    ######################################
    if args.vv:
        logging.basicConfig(format=LOGGING_FORMAT, datefmt=DEFAULT_TIME_FORMAT, level=logging.DEBUG)
    elif args.v:
        logging.basicConfig(format=LOGGING_FORMAT, datefmt=DEFAULT_TIME_FORMAT, level=logging.INFO)
    else:
        logging.basicConfig(format=LOGGING_FORMAT, datefmt=DEFAULT_TIME_FORMAT, level=logging.WARNING)

    ######################################
    # Read Configuration File
    ######################################
    if not os.path.exists(args.c):
        log_fatal("Config file not found: {}\n\n{}".format(args.c, parser.format_help()), exit_code=2)
    config = configparser.ConfigParser()
    config.read(args.c)

    if "WALLABAG" not in config:
        log_fatal("Missing [WALLABAG] section in config file.", exit_code=2)

    wcfg = config["WALLABAG"]
    base_url = wcfg.get("BASEURL", "").strip()
    client_id = wcfg.get("CLIENTID", "").strip()
    client_secret = wcfg.get("CLIENTSECRET", "").strip()
    username = wcfg.get("USERNAME", "").strip()
    password = wcfg.get("PASSWORD", "").strip()

    if not all([base_url, client_id, client_secret, username, password]):
        log_fatal("Incomplete Wallabag configuration. Required keys in [WALLABAG]: BASEURL, CLIENTID, CLIENTSECRET, USERNAME, PASSWORD", exit_code=2)

    if "OPENAI" not in config:
        log_fatal("Missing [OPENAI] section in config file (required for content classification).", exit_code=2)

    api_key = config["OPENAI"].get("API_KEY", "").strip()
    if not api_key:
        log_fatal("OPENAI API_KEY not set in config file", exit_code=2)

    model = args.model or config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")

    email_cfg = None
    if not args.dry_run:
        if "EMAIL" not in config:
            log_fatal("Missing [EMAIL] section in config file (required unless --dry-run). "
                      "See this script's module docstring for the expected format.", exit_code=2)
        ecfg = config["EMAIL"]
        required_keys = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]
        missing_keys = [k for k in required_keys if not ecfg.get(k, "").strip()]
        if missing_keys:
            log_fatal(f"Incomplete [EMAIL] configuration. Missing: {', '.join(missing_keys)}", exit_code=2)
        email_cfg = {
            "host": ecfg.get("SMTP_HOST").strip(),
            "port": int(ecfg.get("SMTP_PORT").strip()),
            "user": ecfg.get("SMTP_USER").strip(),
            "password": ecfg.get("SMTP_PASS").strip(),
            "mail_from": ecfg.get("MAIL_FROM").strip(),
            "mail_to": ecfg.get("MAIL_TO").strip(),
        }

    ######################################
    # Fetch and evaluate recent entries
    ######################################
    log_info("Authenticating with Wallabag...")
    token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    log_info(f"Scanning entries created after {cutoff.isoformat()} ({args.days} day(s) back)...")
    entries = get_recent_entries(base_url, token, cutoff)
    log_info(f"Found {len(entries)} entries in window.")

    flagged = []
    for entry in entries:
        result = evaluate_entry(entry, api_key, model, args.word_threshold)
        if result is not None:
            flagged.append(result)

    ######################################
    # Report
    ######################################
    if args.dry_run:
        write_out(render_report_text(flagged, args.days))
        return 0

    if not flagged:
        log_info("No suspect articles found; not sending an email.")
        return 0

    log_info(f"Sending report of {len(flagged)} suspect article(s) to {email_cfg['mail_to']}...")
    send_report_email(email_cfg, flagged, args.days)
    return 0


def get_recent_entries(base_url, token, cutoff):
    """Fetch entries (with full content) created at or after `cutoff`, newest first."""
    url = f"{base_url}/api/entries.json"
    headers = {"Authorization": f"Bearer {token}"}
    per_page = 30
    page = 1
    results = []

    while True:
        params = {"perPage": per_page, "page": page, "detail": "full", "sort": "created", "order": "desc"}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        total_pages = body.get("pages", 1)
        items = body.get("_embedded", {}).get("items", [])

        stop = False
        for entry in items:
            created_at = parse_entry_created_at(entry)
            if created_at is not None and created_at < cutoff:
                stop = True
                break
            results.append(entry)

        if stop or page >= total_pages:
            break
        page += 1

    return results


def evaluate_entry(entry, api_key, model, word_threshold):
    """Return a flagged-article dict if the entry looks broken, else None."""
    content = entry.get("content") or ""
    text = html_to_text(content)
    word_count = len(text.split())
    lower_text = text.lower()

    for indicator in INDICATOR_STRINGS:
        if indicator in lower_text:
            return _make_flag(entry, word_count, f"matched indicator string: '{indicator}'")

    domain = urlparse(entry.get("url") or "").netloc.lower().removeprefix("www.")
    if domain in SHORT_FORM_DOMAINS:
        if word_count == 0:
            return _make_flag(entry, word_count, "empty content")
        return None

    if word_count < word_threshold:
        return _make_flag(entry, word_count, f"only {word_count} words")

    try:
        verdict, reason = classify_with_llm(api_key, model, text)
    except Exception as e:
        log_warning(f"LLM classification failed for entry id={entry.get('id')}: {e}")
        return _make_flag(entry, word_count, "LLM classification failed — needs manual check")

    if verdict == "error_or_incomplete":
        return _make_flag(entry, word_count, reason or "LLM judged this an error/placeholder page")

    return None


def _make_flag(entry, word_count, reason):
    return {
        "id": entry.get("id"),
        "title": entry.get("title") or "Untitled",
        "url": entry.get("url") or "",
        "word_count": word_count,
        "reason": reason,
    }


def classify_with_llm(api_key, model, text):
    """Ask the LLM whether this looks like a full article or an error/placeholder page.

    Returns (verdict, reason) where verdict is "full_article" or "error_or_incomplete".
    """
    system_prompt = (
        "You are reviewing text extracted from a web page that was saved to a read-it-later "
        "app. Determine whether it is a genuine full article or an error/paywall/placeholder "
        "page (e.g. cookie-consent walls, 'enable JavaScript' notices, login/subscribe prompts, "
        "bot-check pages, or a truncated stub). Respond with your verdict and a brief reason."
    )

    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["full_article", "error_or_incomplete"]},
            "reason": {"type": "string"}
        },
        "required": ["verdict", "reason"],
        "additionalProperties": False
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:3000]}
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "content_verdict",
                "schema": schema,
                "strict": True
            }
        }
    }

    log_debug(f"Calling OpenAI API with model: {model}")

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )

    if not resp.ok:
        log_error(f"OpenAI API error: {resp.status_code}\n{resp.text}")
    resp.raise_for_status()

    parsed = resp.json()
    message = parsed["choices"][0]["message"]
    parsed_json = json.loads(message["content"])
    return parsed_json.get("verdict"), parsed_json.get("reason")


def render_report_text(flagged, days):
    if not flagged:
        return f"No suspect articles found in the last {days} day(s)."

    lines = [f"{len(flagged)} article(s) may be missing content (last {days} day(s)):", ""]
    for item in flagged:
        lines.append(f"- {item['title']!r} (id={item['id']}, {item['word_count']} words)")
        lines.append(f"    {item['url']}")
        lines.append(f"    reason: {item['reason']}")
        lines.append("")
    return "\n".join(lines)


def send_report_email(email_cfg, flagged, days):
    """Send the flagged-article report via SMTP."""
    msg = MIMEMultipart()
    msg['From'] = email_cfg['mail_from']
    msg['To'] = email_cfg['mail_to']
    msg['Subject'] = f"Wallabag: {len(flagged)} article(s) need attention ({datetime.now().strftime('%Y-%m-%d')})"

    msg.attach(MIMEText(render_report_text(flagged, days), 'plain'))

    server = smtplib.SMTP(host=email_cfg['host'], port=email_cfg['port'])
    try:
        server.starttls()
        server.login(email_cfg['user'], email_cfg['password'])
        server.sendmail(email_cfg['mail_from'], [email_cfg['mail_to']], msg.as_string())
        log_info("Sent report email")
    finally:
        server.quit()


##############################################################################
#
# Output and Logging Message Functions
#
##############################################################################
def write_out(msg):
    """Write to stdout."""
    print(msg)

def log_debug(msg):
    logging.debug(msg)

def log_info(msg):
    logging.info(msg)

def log_warning(msg):
    logging.warning(msg)

def log_error(msg):
    logging.error(msg)

def log_fatal(msg, exit_code=1):
    logging.error(msg)
    sys.exit(exit_code)


#
# Initial Setup and call to main()
#
if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
