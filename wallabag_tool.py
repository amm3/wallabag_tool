#!/usr/bin/env python3

import sys
import os
import argparse
import logging
import configparser
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import json
import requests
from readability import Document
import re
import html as html_module

"""
Required modules:
  pip install readability-lxml requests lxml


Expected config blocks in ~/.wallabag:

[WALLABAG]
BASEURL = https://your-wallabag-instance.com
CLIENTID = your_client_id
CLIENTSECRET = your_client_secret
USERNAME = your_username
PASSWORD = your_password
LLM_PROVIDER = openai  # or "ollama"

[OPENAI]
API_KEY = sk-proj-...
TAG_MODEL = gpt-4o-mini

[OLLAMA]
URL = http://localhost:11434
MODEL = llama3.1:8b
API_KEY =  # Optional, for nginx proxy auth (sent as X-Ollama-Key header)

[TAGNOTES]
ai = Artificial Intelligence
ice = U.S. Immigration and Customs Enforcement (ICE), not frozen water
infosec = information security and computer hacking
"""

DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOGGING_FORMAT = '%(asctime)s:%(levelname)s:%(message)s'


def main():
    parser = argparse.ArgumentParser(description='Wallabag Upsert Tool')
    parser.add_argument("-v", action="store_true", default=False, help="Print extra info")
    parser.add_argument("-vv", action="store_true", default=False, help="Print (more) extra info")

    # Config path
    parser.add_argument('-c', nargs='?', type=str,
                        default=os.path.join(str(Path.home()), ".wallabag"),
                        help='Config file (Default: ~/.wallabag)')

    # Operation
    parser.add_argument("html", nargs="?", metavar="HTML_FILE", help="Path to local HTML file. Use '-' to read from stdin, or omit to read stdin (deprecated - use '-' explicitly).")
    parser.add_argument("-i", "--id", dest="id", type=int, help="Entry ID to update (PATCH). If omitted, creates a new entry.")
    parser.add_argument("-l", "--last", action="store_true", default=False,
                        help="Use the most recently created entry (equivalent to -i with the last entry's ID)")
    parser.add_argument("--url", help="URL to add or update. Strips UTM parameters and checks for existing entries.")
    parser.add_argument("--title", help="Optional custom title.")
    parser.add_argument("--tags", help="Comma-separated tags (e.g. 'manual,imported')")
    parser.add_argument("--published-at", dest="published_at",
                        help="Original publication date (e.g. '2024-03-15' or '2024-03-15T10:30:00+00:00')")
    parser.add_argument("--author", dest="author",
                        help="Author name(s) for the entry (e.g. 'Jane Doe' or 'Jane Doe, John Smith')")
    parser.add_argument("--skip-existing", action="store_true", default=False,
                        help="When used with --url, skip adding if entry already exists (only works in add mode, not update)")
    parser.add_argument("--list-tags", action="store_true", default=False, help="List tags")
    parser.add_argument("--dump-html", action="store_true", default=False,
                        help="Dump the HTML content of an entry (requires --id)")
    parser.add_argument("-r", "--retag", action="store_true", default=False,
                        help="Re-run LLM tagging on an existing entry (requires --id)")
    parser.add_argument("--list-untagged", action="store_true", default=False,
                        help="List all entries that have no tags")
    parser.add_argument("--retag-untagged", action="store_true", default=False,
                        help="Re-run LLM tagging on all entries that have no tags")
    
    # HTML processing arguments
    parser.add_argument("--clean", action="store_true", default=False,
                        help="Use readability preprocessing to extract article content (default: send raw HTML to Wallabag)")
    
    args = parser.parse_args()

    # Normalize --published-at early so all code paths see the canonical form
    if args.published_at:
        args.published_at = normalize_published_at(args.published_at)

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

    cfg = config["WALLABAG"]
    base_url = cfg.get("BASEURL", "").strip()
    client_id = cfg.get("CLIENTID", "").strip()
    client_secret = cfg.get("CLIENTSECRET", "").strip()
    username = cfg.get("USERNAME", "").strip()
    password = cfg.get("PASSWORD", "").strip()

    if not all([base_url, client_id, client_secret, username, password]):
        log_fatal("Incomplete Wallabag configuration. Required keys in [WALLABAG]: BASEURL, CLIENTID, CLIENTSECRET, USERNAME, PASSWORD", exit_code=2)

    # Load optional tag notes for LLM disambiguation
    tag_notes = dict(config["TAGNOTES"]) if "TAGNOTES" in config else None

    # LLM provider configuration
    llm_provider = cfg.get("LLM_PROVIDER", "openai").strip().lower()
    if llm_provider not in ("openai", "ollama"):
        log_fatal(f"Unknown LLM_PROVIDER: {llm_provider!r}. Must be 'openai' or 'ollama'.", exit_code=2)

    ollama_url = None
    ollama_model = None
    ollama_api_key = None
    if "OLLAMA" in config:
        ocfg = config["OLLAMA"]
        ollama_url = ocfg.get("URL", "http://localhost:11434").strip()
        ollama_model = ocfg.get("MODEL", "llama3.1:8b").strip()
        ollama_api_key = ocfg.get("API_KEY", "").strip() or None

    if llm_provider == "ollama" and not ollama_url:
        log_fatal("LLM_PROVIDER is 'ollama' but no [OLLAMA] section found in config file.", exit_code=2)

    ######################################
    # Resolve --last to an entry ID
    ######################################
    if args.last:
        if args.id is not None:
            log_fatal("--last cannot be used with --id.", exit_code=2)
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        last_entry = get_last_entry(base_url, token)
        if not last_entry:
            log_fatal("No entries found in Wallabag.", exit_code=1)
        args.id = last_entry.get('id')
        log_info(f"Resolved --last to entry id={args.id} title={last_entry.get('title')!r}")

    ######################################
    # Non-Update Actions
    ######################################
    if args.list_tags:
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        tags = get_all_tags(base_url, token)
        for tag in tags:
            print("{label}: {nbEntries}".format(**tag))
        sys.exit(0)
    
    if args.dump_html:
        if args.id is None:
            log_fatal("--dump-html requires --id to specify which entry to dump.", exit_code=2)
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        entry = get_entry_by_id(base_url, token, args.id)
        if entry:
            html_content = entry.get('content', '')
            if html_content:
                print(html_content)
            else:
                log_warning(f"Entry id={args.id} has no HTML content.")
        else:
            log_fatal(f"Entry id={args.id} not found.", exit_code=1)
        sys.exit(0)

    if args.retag:
        if args.id is None:
            log_fatal("--retag requires --id to specify which entry to retag.", exit_code=2)
        
        # Check for LLM config
        if llm_provider == "openai":
            if "OPENAI" not in config:
                log_fatal("--retag requires [OPENAI] section in config file.", exit_code=2)
            api_key = config["OPENAI"].get("API_KEY")
            if not api_key:
                log_fatal("OPENAI API_KEY not set in config file", exit_code=2)

        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")

        # Fetch existing entry
        entry = get_entry_by_id(base_url, token, args.id)
        if not entry:
            log_fatal(f"Entry id={args.id} not found.", exit_code=1)

        html_content = entry.get('content', '')
        if not html_content:
            log_fatal(f"Entry id={args.id} has no content to analyze.", exit_code=1)

        existing_entry_tags = [t.get('label') for t in entry.get('tags', []) if t.get('label')]
        log_info(f"Entry id={args.id} currently has tags: {existing_entry_tags}")

        # Get all available tags and run LLM
        allowed = [t.get("label") for t in get_all_tags(base_url, token) if t.get("label")]

        log_info("Running LLM tagging...")
        plain_text = html_to_text(html_content)
        if llm_provider == "ollama":
            llm_existing, llm_proposed = choose_tags_with_ollama(ollama_url, ollama_model, plain_text, allowed, max_tags=6, tag_notes=tag_notes, api_key=ollama_api_key)
        else:
            model = config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")
            llm_existing, llm_proposed = choose_tags_with_llm(api_key, model, plain_text, allowed, max_tags=6, tag_notes=tag_notes)
        
        log_info(f"LLM selected tags: {llm_existing}")
        if llm_proposed:
            log_info(f"LLM proposed new tags: {llm_proposed}")
        
        # Combine with manual tags if provided
        tags_to_send = args.tags or ""
        if llm_existing:
            llm_tag_csv = ",".join(llm_existing)
            if tags_to_send:
                tags_to_send += "," + llm_tag_csv
            else:
                tags_to_send = llm_tag_csv
        
        if not tags_to_send:
            log_warning("No tags to apply.")
            sys.exit(0)
        
        # Update entry with new tags
        data = {"tags": tags_to_send}
        updated = patch_entry(base_url, token, args.id, data)
        
        new_tags = [t.get('label') for t in updated.get('tags', []) if t.get('label')]
        log_info(f"Updated entry id={args.id} with tags: {new_tags}")
        write_out(f"Retagged entry id={args.id} title={updated.get('title')!r} tags={new_tags}")
        sys.exit(0)

    if args.list_untagged:
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        untagged = get_untagged_entries(base_url, token, detail="metadata")
        if not untagged:
            write_out("No untagged entries found.")
        else:
            write_out(f"Found {len(untagged)} untagged entries:")
            for entry in untagged:
                write_out(f"  id={entry.get('id', '?')} title={entry.get('title', 'Untitled')!r} url={entry.get('url', '')}")
        sys.exit(0)

    if args.retag_untagged:
        if args.id is not None:
            log_fatal("--retag-untagged operates on all untagged entries. Do not use with --id.", exit_code=2)

        # Validate LLM config
        if llm_provider == "openai":
            if "OPENAI" not in config:
                log_fatal("--retag-untagged requires [OPENAI] section in config file.", exit_code=2)
            api_key = config["OPENAI"].get("API_KEY")
            if not api_key:
                log_fatal("OPENAI API_KEY not set in config file", exit_code=2)

        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")

        # Phase 1: Discover untagged entries (lightweight metadata scan)
        write_out("Scanning for untagged entries...")
        untagged = get_untagged_entries(base_url, token, detail="metadata")

        if not untagged:
            write_out("No untagged entries found. Nothing to do.")
            sys.exit(0)

        write_out(f"Found {len(untagged)} untagged entries. Starting LLM tagging...")

        # Fetch allowed tags once (reused for all entries)
        allowed = [t.get("label") for t in get_all_tags(base_url, token) if t.get("label")]

        # Phase 2: Retag each entry
        tagged_count = 0
        skipped_count = 0
        error_count = 0

        for i, entry_meta in enumerate(untagged, 1):
            entry_id = entry_meta.get('id')
            entry_title = entry_meta.get('title', 'Untitled')
            write_out(f"[{i}/{len(untagged)}] Processing id={entry_id} title={entry_title!r}...")

            try:
                # Fetch full entry content
                entry = get_entry_by_id(base_url, token, entry_id)
                if not entry:
                    log_warning(f"Entry id={entry_id} not found. Skipping.")
                    skipped_count += 1
                    continue

                html_content = entry.get('content', '')
                if not html_content:
                    log_warning(f"Entry id={entry_id} has no content. Skipping.")
                    skipped_count += 1
                    continue

                # Run LLM tagging
                plain_text = html_to_text(html_content)
                if llm_provider == "ollama":
                    llm_existing, llm_proposed = choose_tags_with_ollama(
                        ollama_url, ollama_model, plain_text, allowed,
                        max_tags=6, tag_notes=tag_notes, api_key=ollama_api_key)
                else:
                    model = config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")
                    llm_existing, llm_proposed = choose_tags_with_llm(
                        api_key, model, plain_text, allowed,
                        max_tags=6, tag_notes=tag_notes)

                log_info(f"LLM selected tags: {llm_existing}")
                if llm_proposed:
                    log_info(f"LLM proposed new tags: {llm_proposed}")

                if not llm_existing:
                    log_warning(f"LLM returned no tags for id={entry_id}. Skipping.")
                    skipped_count += 1
                    continue

                # Patch entry with tags
                tags_csv = ",".join(llm_existing)
                data = {"tags": tags_csv}
                updated = patch_entry(base_url, token, entry_id, data)

                new_tags = [t.get('label') for t in updated.get('tags', []) if t.get('label')]
                write_out(f"  Tagged id={entry_id} with: {new_tags}")
                tagged_count += 1

            except Exception as e:
                log_error(f"Failed to retag id={entry_id}: {e}")
                error_count += 1
                continue

        write_out(f"\nDone. Tagged: {tagged_count}, Skipped: {skipped_count}, Errors: {error_count}")
        sys.exit(0)

    ######################################
    # Handle --url operation
    ######################################
    if args.url:
        if args.id is not None:
            log_fatal("--url cannot be used with --id. Use --url alone to add/update by URL.", exit_code=2)
        
        # Strip UTM parameters
        clean_url = strip_utm_parameters(args.url)
        log_info(f"Original URL: {args.url}")
        if clean_url != args.url:
            log_info(f"Cleaned URL: {clean_url}")
        
        # Get OAuth token
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        
        # Check if URL exists
        existing_entry = find_entry_by_url(base_url, token, clean_url)
        
        if existing_entry:
            entry_id = existing_entry.get('id')
            
            # If --skip-existing is set, just report and exit
            if args.skip_existing:
                log_info(f"Entry already exists (id={entry_id}). Skipping due to --skip-existing flag.")
                write_out(f"Skipped existing entry id={entry_id} title={existing_entry.get('title')!r}")
                return 0
            
            log_info(f"Found existing entry id={entry_id} for URL: {clean_url}")
            
            # Fetch the URL content
            log_info(f"Fetching content from {clean_url}")
            try:
                raw_html = fetch_url_with_requests(clean_url)
            except Exception as e:
                log_fatal(f"Failed to fetch URL: {e}", exit_code=2)

            if args.clean:
                cleaned_title, cleaned_html = clean_html_with_readability(raw_html)
                log_info("Extracted readable content from fetched HTML.")
            else:
                # Send raw HTML, let Wallabag do the processing
                cleaned_title = extract_title_from_html(raw_html)
                cleaned_html = raw_html
                log_info("Using raw HTML without readability preprocessing.")

            # Get LLM tags if available
            llm_tag_csv = None
            try:
                allowed = [t.get("label") for t in get_all_tags(base_url, token) if t.get("label")]
                plain_text = html_to_text(cleaned_html)
                if llm_provider == "ollama":
                    existing_tags, _proposed = choose_tags_with_ollama(ollama_url, ollama_model, plain_text, allowed, max_tags=6, tag_notes=tag_notes, api_key=ollama_api_key)
                else:
                    api_key = config["OPENAI"].get("API_KEY")
                    if api_key:
                        model = config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")
                        existing_tags, _proposed = choose_tags_with_llm(api_key, model, plain_text, allowed, max_tags=6, tag_notes=tag_notes)
                    else:
                        existing_tags = []
                if existing_tags:
                    llm_tag_csv = ",".join(existing_tags)
                    log_info(f"LLM selected tags: {llm_tag_csv}")
            except Exception as e:
                log_warning(f"Could not fetch LLM tags: {e}")

            # Prepare tags to send (combine user tags and LLM tags)
            tags_to_send = args.tags or ""
            if llm_tag_csv:
                if tags_to_send:
                    tags_to_send += "," + llm_tag_csv
                else:
                    tags_to_send = llm_tag_csv

            # Update existing entry
            data = {
                "title": args.title or cleaned_title or existing_entry.get('title'),
                "content": cleaned_html
            }
            if tags_to_send:
                data["tags"] = tags_to_send
            if args.published_at:
                data["published_at"] = args.published_at
            if args.author:
                data["authors"] = args.author

            updated = patch_entry(base_url, token, entry_id, data)
            log_info(f"Updated entry id={entry_id}")
            write_out(f"Updated entry id={entry_id} title={updated.get('title')!r}")
        
        else:
            # Entry doesn't exist, create new
            log_info(f"No existing entry for URL: {clean_url}. Creating new entry.")
            
            # Fetch the URL content
            log_info(f"Fetching content from {clean_url}")
            try:
                raw_html = fetch_url_with_requests(clean_url)
            except Exception as e:
                log_fatal(f"Failed to fetch URL: {e}", exit_code=2)
            
            if args.clean:
                cleaned_title, cleaned_html = clean_html_with_readability(raw_html)
                log_info("Extracted readable content from fetched HTML.")
            else:
                # Send raw HTML, let Wallabag do the processing
                cleaned_title = extract_title_from_html(raw_html)
                cleaned_html = raw_html
                log_info("Using raw HTML without readability preprocessing.")
            
            # Get LLM tags if available
            llm_tag_csv = None
            try:
                allowed = [t.get("label") for t in get_all_tags(base_url, token) if t.get("label")]
                plain_text = html_to_text(cleaned_html)
                if llm_provider == "ollama":
                    existing_tags, _proposed = choose_tags_with_ollama(ollama_url, ollama_model, plain_text, allowed, max_tags=6, tag_notes=tag_notes, api_key=ollama_api_key)
                else:
                    api_key = config["OPENAI"].get("API_KEY")
                    if api_key:
                        model = config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")
                        existing_tags, _proposed = choose_tags_with_llm(api_key, model, plain_text, allowed, max_tags=6, tag_notes=tag_notes)
                    else:
                        existing_tags = []
                if existing_tags:
                    llm_tag_csv = ",".join(existing_tags)
                    log_info(f"LLM selected tags: {llm_tag_csv}")
            except Exception as e:
                log_warning(f"Could not fetch LLM tags: {e}")

            # Prepare tags to send (combine user tags and LLM tags)
            tags_to_send = args.tags or ""
            if llm_tag_csv:
                if tags_to_send:
                    tags_to_send += "," + llm_tag_csv
                else:
                    tags_to_send = llm_tag_csv

            # Create new entry
            data = {
                "url": clean_url,
                "title": args.title or cleaned_title,
                "content": cleaned_html
            }
            if tags_to_send:
                data["tags"] = tags_to_send
            if args.published_at:
                data["published_at"] = args.published_at
            if args.author:
                data["authors"] = args.author

            new_entry = post_entry(base_url, token, data)
            entry_id = new_entry.get('id')
            log_info(f"Created new entry id={entry_id}")
            write_out(f"Created entry id={entry_id} title={new_entry.get('title')!r}")
        
        return 0

    ######################################
    # Metadata-only update (no HTML content needed)
    ######################################
    if args.id is not None and args.html is None:
        data = {}
        if args.title:
            data["title"] = args.title
        if args.tags:
            data["tags"] = args.tags
        if args.published_at:
            data["published_at"] = args.published_at
        if args.author:
            data["authors"] = args.author
        if not data:
            log_fatal("Nothing to update. Provide --title, --tags, --published-at, or --author.", exit_code=2)
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        updated = patch_entry(base_url, token, args.id, data)
        log_info(f"Updated entry id={args.id}")
        write_out(f"Updated entry id={args.id} title={updated.get('title')!r}")
        return 0

    ######################################
    # HTML File Processing
    ######################################
    html_input = None
    if args.html is None:
        # Deprecated: reading from stdin without explicit '-'
        log_warning("Reading from stdin without explicit '-' is deprecated. Please use '-' as the argument.")
        html_input = sys.stdin.read()
    elif args.html == "-":
        html_input = sys.stdin.read()
    else:
        # Read from file
        if not os.path.exists(args.html):
            log_fatal(f"HTML file not found: {args.html}", exit_code=2)
        with open(args.html, 'r', encoding='utf-8') as f:
            html_input = f.read()

    if not html_input or not html_input.strip():
        log_fatal("No HTML content provided.", exit_code=2)

    # Clean HTML (only if --clean is set, otherwise send raw HTML to Wallabag)
    if args.clean:
        title, cleaned = clean_html_with_readability(html_input)
        log_info("Extracted readable content.")
    else:
        # Send raw HTML, let Wallabag do the processing
        title = extract_title_from_html(html_input)
        cleaned = html_input
        log_info("Using raw HTML without readability preprocessing.")

    # Get OAuth token
    token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
    log_debug("Obtained access token.")

    # Get LLM tags if available
    llm_tag_csv = None
    try:
        allowed = [t.get("label") for t in get_all_tags(base_url, token) if t.get("label")]
        plain_text = html_to_text(cleaned)
        if llm_provider == "ollama":
            existing_tags, _proposed = choose_tags_with_ollama(ollama_url, ollama_model, plain_text, allowed, max_tags=6, tag_notes=tag_notes, api_key=ollama_api_key)
        else:
            api_key = config["OPENAI"].get("API_KEY")
            if api_key:
                model = config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")
                existing_tags, _proposed = choose_tags_with_llm(api_key, model, plain_text, allowed, max_tags=6, tag_notes=tag_notes)
            else:
                existing_tags = []
        if existing_tags:
            llm_tag_csv = ",".join(existing_tags)
            log_info(f"LLM selected tags: {llm_tag_csv}")
    except Exception as e:
        log_warning(f"Could not fetch LLM tags: {e}")

    # Prepare tags to send (combine user tags and LLM tags)
    tags_to_send = args.tags or ""
    if llm_tag_csv:
        if tags_to_send:
            tags_to_send += "," + llm_tag_csv
        else:
            tags_to_send = llm_tag_csv

    if args.id is not None:
        # PATCH existing entry
        data = {"content": cleaned}
        if args.title:
            data["title"] = args.title
        if tags_to_send:
            data["tags"] = tags_to_send
        if args.published_at:
            data["published_at"] = args.published_at
        if args.author:
            data["authors"] = args.author

        updated = patch_entry(base_url, token, args.id, data)
        log_info(f"Updated entry id={args.id}")
        write_out(f"Updated entry id={args.id} title={updated.get('title')!r}")
    else:
        # POST new entry
        data = {"content": cleaned}
        if args.title:
            data["title"] = args.title
        else:
            data["title"] = title or "Untitled"
        if tags_to_send:
            data["tags"] = tags_to_send
        if args.published_at:
            data["published_at"] = args.published_at
        if args.author:
            data["authors"] = args.author

        new_entry = post_entry(base_url, token, data)
        entry_id = new_entry.get('id')
        log_info(f"Created new entry id={entry_id}")
        write_out(f"Created entry id={entry_id} title={new_entry.get('title')!r}")

    return 0


######################################
# URL fetching
######################################
def fetch_url_with_requests(url):
    """
    Fetch URL content using requests library (traditional method).
    
    Args:
        url: URL to fetch
    
    Returns:
        HTML content as string
    """
    resp = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
    resp.raise_for_status()
    return resp.text


######################################
# Logging
######################################
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

def write_out(msg):
    """Write to stdout."""
    print(msg)


######################################
# Date Utilities
######################################
def normalize_published_at(value):
    """Normalize a date string to ISO 8601 format for the Wallabag API.

    Accepts bare dates like '2024-03-15' (treated as midnight UTC)
    or full ISO 8601 datetimes which are passed through."""
    value = value.strip()
    # Bare date: YYYY-MM-DD
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return value + "T00:00:00+00:00"
    # Already looks like a full datetime — validate it parses
    try:
        datetime.fromisoformat(value)
    except ValueError:
        log_fatal(f"Invalid date format for --published-at: {value!r}. Use YYYY-MM-DD or full ISO 8601.", exit_code=2)
    return value


######################################
# URL Utilities
######################################
def strip_utm_parameters(url):
    """Remove UTM tracking parameters from URL."""
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    
    # Remove UTM parameters
    filtered_params = {k: v for k, v in query_params.items() if not k.lower().startswith('utm_')}
    
    # Reconstruct URL
    new_query = urlencode(filtered_params, doseq=True)
    new_parsed = parsed._replace(query=new_query)
    return urlunparse(new_parsed)


######################################
# Wallabag API
######################################
def oauth_token_password_grant(base_url, client_id, client_secret, username, password):
    """Get OAuth token using password grant."""
    url = f"{base_url}/oauth/v2/token"
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_all_tags(base_url, token):
    """Get all tags from Wallabag."""
    url = f"{base_url}/api/tags.json"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_entry_by_id(base_url, token, entry_id):
    """Get a specific entry by ID."""
    url = f"{base_url}/api/entries/{entry_id}.json"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def find_entry_by_url(base_url, token, url):
    """Find entry by URL. Returns entry dict or None."""
    api_url = f"{base_url}/api/entries/exists.json"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"url": url}
    
    resp = requests.get(api_url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    
    # The API returns {"exists": true/false, ...}
    if result.get("exists"):
        # If exists, fetch the full entry
        # The exists endpoint may return entry data directly or we need to search
        # Try to get entry from result or search
        if "id" in result:
            return get_entry_by_id(base_url, token, result["id"])
        else:
            # Fallback: search for entry
            search_url = f"{base_url}/api/entries.json"
            search_params = {"url": url}
            search_resp = requests.get(search_url, headers=headers, params=search_params, timeout=30)
            search_resp.raise_for_status()
            entries = search_resp.json().get("_embedded", {}).get("items", [])
            if entries:
                return entries[0]
    
    return None


def get_last_entry(base_url, token):
    """Get the most recently created entry."""
    url = f"{base_url}/api/entries.json"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"sort": "created", "order": "desc", "perPage": 1}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("_embedded", {}).get("items", [])
    if items:
        return items[0]
    return None


def get_untagged_entries(base_url, token, detail="metadata"):
    """Fetch all entries that have no tags.

    Paginates through all entries and filters client-side since the
    Wallabag API has no server-side filter for untagged entries.

    Args:
        base_url: Wallabag instance base URL.
        token: OAuth access token.
        detail: "metadata" for lightweight listing, "full" to include HTML content.

    Returns:
        List of entry dicts with zero tags.
    """
    url = f"{base_url}/api/entries.json"
    headers = {"Authorization": f"Bearer {token}"}
    per_page = 30
    page = 1
    results = []

    # First request to discover total pages
    params = {"perPage": per_page, "page": page, "detail": detail}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    total_pages = body.get("pages", 1)
    log_info(f"Scanning entries: {body.get('total', 0)} total across {total_pages} pages")

    while True:
        if page > 1:
            params = {"perPage": per_page, "page": page, "detail": detail}
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()

        items = body.get("_embedded", {}).get("items", [])
        for entry in items:
            if not entry.get("tags", []):
                results.append(entry)

        print(f"  Scanned page {page}/{total_pages}...", file=sys.stderr)

        if page >= total_pages:
            break
        page += 1

    return results


def post_entry(base_url, token, data):
    """Create new entry."""
    url = f"{base_url}/api/entries.json"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_entry(base_url, token, entry_id, data):
    """Update existing entry."""
    url = f"{base_url}/api/entries/{entry_id}.json"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    resp = requests.patch(url, headers=headers, json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


######################################
# HTML Processing
######################################
def extract_title_from_html(html_input):
    """Extract title from HTML without full readability processing."""
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html_input)
        
        # Try to get title from <title> tag first
        title_elements = tree.xpath('//title/text()')
        if title_elements:
            return title_elements[0].strip()
        
        # Fallback to first <h1> tag
        h1_elements = tree.xpath('//h1/text()')
        if h1_elements:
            return h1_elements[0].strip()
        
        # Last resort: use Document to extract title
        doc = Document(html_input)
        return doc.title()
    except Exception as e:
        log_warning(f"Failed to extract title from HTML: {e}")
        return "Untitled"


def clean_html_with_readability(html_input):
    """Extract readable content using readability-lxml."""
    doc = Document(html_input)
    title = doc.title()
    cleaned = doc.summary()
    return title, cleaned


######################################
# LLM helpers
######################################
def html_to_text(html_content: str) -> str:
    """Strip HTML tags and normalize whitespace to produce plain text."""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_module.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def _build_tagging_system_prompt(tag_notes: dict | None = None) -> str:
    """Build the system prompt used by both OpenAI and Ollama tagging."""
    tag_notes_block = ""
    if tag_notes:
        lines = [f'- "{tag}" refers to {description}' for tag, description in tag_notes.items()]
        tag_notes_block = "TAG NOTES (use these to resolve ambiguous tag names):\n" + "\n".join(lines) + "\n\n"

    return f"""You are a tagging assistant for a personal reading archive. Your job is to select tags that describe what an article is ABOUT, not what it merely MENTIONS.

IMPORTANT GUIDELINES:
- Only select tags for topics that are CENTRAL to the article's main thesis or subject matter
- Do NOT tag based on passing mentions, background context, or tangential references
- Ask yourself: "Is this article primarily about [tag topic]?" If not, don't use that tag
- For people (e.g., politicians, celebrities): only tag if the article is specifically ABOUT that person, not just mentioning them in context
- For broad/abstract tags (e.g., "culture-war", "politics"): only use if the article is explicitly analyzing or discussing that phenomenon as its main subject
- Prefer specific tags over vague ones when both apply
- It's better to select fewer, highly-relevant tags than many loosely-related ones
- Select 1-4 tags typically; only use more if the article genuinely covers multiple distinct topics in depth

{tag_notes_block}Select tags from the allowed list ("existing").
Only put non-duplicates into "proposed_new" if a new tag would be clearly valuable and nothing in the allowed list fits."""


def _parse_json_response(text: str) -> dict:
    """Parse JSON from LLM response text, with fallback extraction."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to extract JSON object from surrounding text
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    log_warning(f"Failed to parse JSON from LLM response: {text[:200]}")
    return {}


def _ollama_request(ollama_url: str, model: str, prompt: str, api_key: str | None = None, num_predict: int = 4000) -> str:
    """Make a request to the Ollama /api/generate endpoint."""
    base_url = ollama_url.rstrip('/')
    api_url = f"{base_url}/api/generate"

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": num_predict
        }
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Ollama-Key"] = api_key

    log_debug(f"Calling Ollama API at {api_url} with model: {model}, api_key={'set' if api_key else 'not set'}")

    resp = requests.post(api_url, headers=headers, json=payload, timeout=300)

    if not resp.ok:
        log_error(f"Ollama API error: {resp.status_code}\n{resp.text}")
    resp.raise_for_status()

    return resp.json().get("response", "")


def choose_tags_with_llm(api_key: str, model: str, article_text: str, allowed_tags: list[str], max_tags: int = 6, tag_notes: dict | None = None):
    """Select tags using OpenAI chat completions API."""
    system_prompt = _build_tagging_system_prompt(tag_notes)

    schema = {
        "type": "object",
        "properties": {
            "existing": {
                "type": "array",
                "items": {"type": "string", "enum": allowed_tags},
                "maxItems": max_tags
            },
            "proposed_new": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3
            }
        },
        "required": ["existing", "proposed_new"],
        "additionalProperties": False
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Tag the following article:\n\n{article_text[:12000]}"}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tag_selection",
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

    return parsed_json.get("existing", []), parsed_json.get("proposed_new", [])


def choose_tags_with_ollama(ollama_url: str, model: str, article_text: str, allowed_tags: list[str], max_tags: int = 6, tag_notes: dict | None = None, api_key: str | None = None):
    """Select tags using Ollama /api/generate endpoint."""
    system_prompt = _build_tagging_system_prompt(tag_notes)

    tags_list = ", ".join(f'"{t}"' for t in allowed_tags)

    prompt = f"""{system_prompt}

Allowed tags: [{tags_list}]

Respond with ONLY valid JSON in this exact format (no other text):
{{"existing": ["tag1", "tag2"], "proposed_new": ["new_tag"]}}

"existing" must only contain tags from the allowed list above (max {max_tags}).
"proposed_new" may contain up to 3 new tags only if nothing in the allowed list fits.

Tag the following article:

{article_text[:12000]}"""

    response_text = _ollama_request(ollama_url, model, prompt, api_key=api_key)
    parsed = _parse_json_response(response_text)

    # Validate existing tags against allowed list
    existing = [t for t in parsed.get("existing", []) if t in allowed_tags][:max_tags]
    proposed = parsed.get("proposed_new", [])[:3]

    return existing, proposed



#
# Initial Setup and call to main()
#
if __name__ == '__main__':
    #sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 1)  # reopen STDOUT unbuffered
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
