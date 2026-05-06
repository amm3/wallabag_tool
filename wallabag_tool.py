#!/usr/bin/env python3

import sys
import os
import argparse
import logging
import configparser
from pathlib import Path
from datetime import datetime, timezone
import zoneinfo
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
TIMEZONE = America/New_York  # optional; IANA timezone for --published-at (default: system local)

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
    parser.add_argument("--published-at", "-p", dest="published_at",
                        help="Original publication date (e.g. '2024-03-15', '2024-03-15 4:43 PM', '2024-03-15 16:43', or full ISO 8601)")
    parser.add_argument("--author", dest="author",
                        help="Author name(s) for the entry (e.g. 'Jane Doe' or 'Jane Doe, John Smith')")
    parser.add_argument("--skip-existing", action="store_true", default=False,
                        help="When used with --url, skip adding if entry already exists (only works in add mode, not update)")
    parser.add_argument("--list-tags", action="store_true", default=False, help="List tags")
    parser.add_argument("--dump-html", action="store_true", default=False,
                        help="Dump the HTML content of an entry (requires --id)")
    parser.add_argument("--save-article", action="store_true", default=False,
                        help="Save article as a self-contained HTML file (requires --id)")
    parser.add_argument("-o", "--output", dest="output",
                        help="Output filename for --save-article (default: <id>-<slug>.html). Use '-' for stdout.")
    parser.add_argument("-r", "--retag", action="store_true", default=False,
                        help="Re-run LLM tagging on an existing entry (requires --id)")
    parser.add_argument("--list-untagged", action="store_true", default=False,
                        help="List all entries that have no tags")
    parser.add_argument("--untagged-exhaustive", action="store_true", default=False,
                        help="With --list-untagged/--retag-untagged: scan every page instead of stopping after 20 consecutive tagged entries")
    parser.add_argument("--retag-untagged", action="store_true", default=False,
                        help="Re-run LLM tagging on all entries that have no tags")
    parser.add_argument("--consolidate-tag", dest="consolidate_tag", metavar="SOURCE",
                        help="Merge all entries from SOURCE (label or numeric id) into --into TARGET, then delete SOURCE.")
    parser.add_argument("--into", dest="consolidate_into", metavar="TARGET",
                        help="Target tag label or numeric id for --consolidate-tag.")

    # HTML processing arguments
    parser.add_argument("--clean", action="store_true", default=False,
                        help="Use readability preprocessing to extract article content (default: send raw HTML to Wallabag)")
    parser.add_argument("--twitter", action="store_true", default=False,
                        help="Clean Twitter/X HTML, preserving paragraph breaks in tweet text (for HTML copied from browser dev tools)")
    parser.add_argument("--facebook", action="store_true", default=False,
                        help="Clean Facebook HTML, extracting post content and author (for HTML saved from browser dev tools)")
    parser.add_argument("--linkedin", action="store_true", default=False,
                        help="Clean LinkedIn HTML, extracting post content and author (for HTML saved from browser dev tools)")

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

    cfg = config["WALLABAG"]
    base_url = cfg.get("BASEURL", "").strip()
    client_id = cfg.get("CLIENTID", "").strip()
    client_secret = cfg.get("CLIENTSECRET", "").strip()
    username = cfg.get("USERNAME", "").strip()
    password = cfg.get("PASSWORD", "").strip()

    if not all([base_url, client_id, client_secret, username, password]):
        log_fatal("Incomplete Wallabag configuration. Required keys in [WALLABAG]: BASEURL, CLIENTID, CLIENTSECRET, USERNAME, PASSWORD", exit_code=2)

    # Resolve timezone for --published-at (before normalization)
    tz_name = cfg.get("TIMEZONE", "").strip()
    if tz_name:
        try:
            pub_tz = zoneinfo.ZoneInfo(tz_name)
        except zoneinfo.ZoneInfoNotFoundError:
            log_fatal(f"Unknown TIMEZONE in config: {tz_name!r}. Use an IANA name like 'America/New_York'.", exit_code=2)
    else:
        pub_tz = datetime.now().astimezone().tzinfo  # system local timezone

    # Normalize --published-at now that we have the timezone
    if args.published_at:
        args.published_at = normalize_published_at(args.published_at, tz=pub_tz)

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
            print("{label} (id={id}): {nbEntries}".format(**tag))
        sys.exit(0)
    
    if args.consolidate_tag or args.consolidate_into:
        if not args.consolidate_tag:
            log_fatal("--into requires --consolidate-tag.", exit_code=2)
        if not args.consolidate_into:
            log_fatal("--consolidate-tag requires --into.", exit_code=2)
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        all_tags = get_all_tags(base_url, token)

        def resolve_tag(value, role):
            if value.isdigit():
                tag = next((t for t in all_tags if t["id"] == int(value)), None)
                if tag is None:
                    log_fatal(f"{role} tag id={value} not found.", exit_code=1)
                return tag
            matches = [t for t in all_tags if t["label"] == value]
            if not matches:
                log_fatal(f"{role} tag not found: '{value}'", exit_code=1)
            if len(matches) > 1:
                lines = "\n".join(f"  id={t['id']}  '{t['label']}': {t['nbEntries']} entries" for t in matches)
                log_fatal(
                    f"Multiple tags share the label '{value}'. Specify by id instead:\n{lines}",
                    exit_code=1,
                )
            return matches[0]

        source_tag = resolve_tag(args.consolidate_tag, "Source")
        target_tag = resolve_tag(args.consolidate_into, "Target")
        source_label = source_tag["label"]
        target_label = target_tag["label"]
        if source_tag["id"] == target_tag["id"]:
            log_fatal("Source and target are the same tag.", exit_code=2)
        entries = get_entries_for_tag(base_url, token, source_label)
        write_out(f"Source tag '{source_label}' (id={source_tag['id']}): {len(entries)} entries")
        write_out(f"Target tag '{target_label}' (id={target_tag['id']}): {target_tag.get('nbEntries', '?')} entries")
        if not entries:
            write_out("Source tag has no entries. Only the tag itself will be deleted.")
        else:
            write_out("\nEntries to migrate:")
            for entry in entries:
                write_out(f"  id={entry['id']} {entry.get('title', 'Untitled')!r}")
        answer = input(f"\nMigrate {len(entries)} entries from '{source_label}' to '{target_label}', then delete '{source_label}'? [y/N] ")
        if answer.strip().lower() != "y":
            write_out("Aborted.")
            sys.exit(0)
        migrated = 0
        errors = 0
        for entry in entries:
            current_tags = [t["label"] for t in entry.get("tags", []) if t.get("label")]
            new_tags = []
            seen = set()
            for label in current_tags:
                canonical = target_label if label == source_label else label
                if canonical not in seen:
                    new_tags.append(canonical)
                    seen.add(canonical)
            try:
                patch_entry(base_url, token, entry["id"], {"tags": ",".join(new_tags)})
                write_out(f"  Migrated id={entry['id']} tags={new_tags}")
                migrated += 1
            except Exception as e:
                log_error(f"  Failed to update id={entry['id']}: {e}")
                errors += 1
        try:
            delete_tag(base_url, token, source_tag["id"])
            write_out(f"Deleted tag '{source_label}' (id={source_tag['id']}).")
        except Exception as e:
            log_error(f"Failed to delete tag '{source_label}': {e}")
            write_out("Migration complete but tag deletion failed. You may need to delete it manually.")
        write_out(f"\nDone. Migrated {migrated}/{len(entries)} entries.{f' {errors} error(s).' if errors else ''}")
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

    if args.save_article:
        if args.id is None:
            log_fatal("--save-article requires --id to specify which entry to save.", exit_code=2)
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        entry = get_entry_by_id(base_url, token, args.id)
        if not entry:
            log_fatal(f"Entry id={args.id} not found.", exit_code=1)
        html_out = render_article_html(entry)
        output_path = args.output
        if output_path == "-":
            print(html_out)
        else:
            if not output_path:
                slug = _slugify(entry.get("title") or str(args.id))
                output_path = f"{args.id}-{slug}.html"
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(html_out)
            log_warning(f"Saved article to {output_path}")
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
        
        # Update entry with tags and any other metadata flags
        data = {"tags": tags_to_send}
        if args.title:
            data["title"] = args.title
        if args.published_at:
            data["published_at"] = args.published_at
        if args.author:
            data["authors"] = args.author
        updated = patch_entry(base_url, token, args.id, data)

        new_tags = [t.get('label') for t in updated.get('tags', []) if t.get('label')]
        log_info(f"Updated entry id={args.id} with tags: {new_tags}")
        write_out(f"Retagged entry id={args.id} title={updated.get('title')!r} tags={new_tags}")
        sys.exit(0)

    if args.list_untagged:
        token = oauth_token_password_grant(base_url, client_id, client_secret, username, password)
        log_debug("Obtained access token.")
        untagged = get_untagged_entries(base_url, token, detail="metadata", exhaustive=args.untagged_exhaustive)
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
        untagged = get_untagged_entries(base_url, token, detail="metadata", exhaustive=args.untagged_exhaustive)

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

    # Extract date from JSON script tags (runs for all modes, before mode-specific processing)
    if not args.published_at:
        json_date = extract_date_from_json_scripts(html_input)
        if json_date:
            args.published_at = json_date
            log_info(f"Extracted published_at from JSON script data: {json_date}")

    # Fallback: extract date from <article> tag attributes (e.g. data-last-updated)
    if not args.published_at:
        article_date = extract_date_from_article_tag(html_input)
        if article_date:
            args.published_at = article_date
            log_info(f"Extracted published_at from <article> attribute: {article_date}")

    # Fallback: extract date from <time datetime="..."> elements
    if not args.published_at:
        time_date = extract_date_from_time_element(html_input)
        if time_date:
            args.published_at = time_date
            log_info(f"Extracted published_at from <time datetime> element: {time_date}")

    # Clean HTML based on selected mode
    if args.twitter:
        title, cleaned, tweet_time, tweet_author = clean_twitter_html(html_input)
        log_info("Extracted tweet content with paragraph preservation.")
        if tweet_time and not args.published_at:
            args.published_at = tweet_time
            log_info(f"Extracted published_at from <time> tag: {tweet_time}")
        if tweet_author and not args.author:
            args.author = tweet_author
            log_info(f"Extracted author from tweet: {tweet_author}")
    elif args.facebook:
        title, cleaned, post_time, post_author = clean_facebook_html(html_input)
        log_info("Extracted Facebook post content.")
        if post_time and not args.published_at:
            args.published_at = post_time
        if post_author and not args.author:
            args.author = post_author
            log_info(f"Extracted author from Facebook post: {post_author}")
    elif args.linkedin:
        title, cleaned, post_time, post_author = clean_linkedin_html(html_input)
        log_info("Extracted LinkedIn post content.")
        if post_time and not args.published_at:
            args.published_at = post_time
        if post_author and not args.author:
            args.author = post_author
            log_info(f"Extracted author from LinkedIn post: {post_author}")
    elif _is_nyt_birdkit(html_input):
        title, cleaned, article_time, article_author = clean_nyt_birdkit_html(html_input)
        log_info("Auto-detected NYT birdkit interactive article; extracted structured content.")
        if article_time and not args.published_at:
            args.published_at = article_time
        if article_author and not args.author:
            args.author = article_author
    elif args.clean:
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

    # Get LLM tags if available; also generate headline for twitter mode
    llm_tag_csv = None
    llm_title = None
    try:
        allowed = [t.get("label") for t in get_all_tags(base_url, token) if t.get("label")]
        plain_text = html_to_text(cleaned)
        if (args.twitter or args.facebook or args.linkedin) and not args.title:
            try:
                llm_title = generate_twitter_headline_with_llm(config, plain_text)
                if llm_title:
                    log_info(f"LLM-generated headline: {llm_title}")
            except Exception as e:
                log_warning(f"Could not generate LLM headline: {e}")
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
        elif llm_title:
            data["title"] = llm_title
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
            data["title"] = llm_title or title or "Untitled"
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
def normalize_published_at(value, tz=None):
    """Normalize a date string to ISO 8601 format for the Wallabag API.

    Accepts:
      - Bare date:              '2024-03-15'             → midnight local/configured tz
      - Date + 12-hour time:    '2024-03-15 4:43 PM'    → local/configured tz
      - Date + 24-hour time:    '2024-03-15 16:43'      → local/configured tz
      - Full ISO 8601:          '2024-03-15T10:30:00+00:00' → passed through as-is

    tz: a tzinfo object. Defaults to the system local timezone.
    """
    value = value.strip()

    if tz is None:
        tz = datetime.now().astimezone().tzinfo

    # Bare date: YYYY-MM-DD
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        y, m, d = value.split('-')
        dt = datetime(int(y), int(m), int(d), tzinfo=tz)
        return dt.isoformat()

    # Human-friendly formats (no timezone — attach tz)
    human_formats = [
        "%Y-%m-%d %I:%M %p",   # 2026-02-25 4:43 PM
        "%Y-%m-%d %I:%M%p",    # 2026-02-25 4:43PM
        "%Y-%m-%d %H:%M",      # 2026-02-25 16:43
        "%Y-%m-%d %H:%M:%S",   # 2026-02-25 16:43:00
    ]
    for fmt in human_formats:
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=tz)
            return dt.isoformat()
        except ValueError:
            continue

    # Already looks like a full datetime — validate it parses
    try:
        datetime.fromisoformat(value)
    except ValueError:
        log_fatal(
            f"Invalid date format for --published-at: {value!r}. "
            "Use YYYY-MM-DD, 'YYYY-MM-DD HH:MM AM/PM', 'YYYY-MM-DD HH:MM', or full ISO 8601.",
            exit_code=2,
        )
    return value


def _try_extract_json_at_pos(text, start):
    """Extract a balanced JSON object from text starting at position start (must be '{').
    Returns parsed dict/list or None."""
    if start >= len(text) or text[start] != '{':
        return None
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        return None
        i += 1
    return None


def _extract_json_objects_from_script(text):
    """Extract JSON objects from a script tag's text content.

    Strategy 1: treat entire text as JSON (covers application/ld+json).
    Strategy 2: find '= {' assignment patterns and parse the value.
    Returns list of parsed dicts.
    """
    results = []
    seen_starts = set()

    # Strategy 1: whole text
    stripped = text.strip()
    if stripped.startswith('{') or stripped.startswith('['):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                results.append(obj)
            elif isinstance(obj, list):
                results.extend(o for o in obj if isinstance(o, dict))
            return results  # Clean JSON — no need to dig further
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 2: variable assignments  var x = {...};
    for m in re.finditer(r'=\s*(\{)', text):
        start = m.start(1)
        if start in seen_starts:
            continue
        seen_starts.add(start)
        obj = _try_extract_json_at_pos(text, start)
        if isinstance(obj, dict):
            results.append(obj)

    return results


def _find_key_in_obj(obj, key, depth=0):
    """Recursively search a nested dict/list for the first occurrence of key."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = _find_key_in_obj(v, key, depth + 1)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_key_in_obj(item, key, depth + 1)
            if result is not None:
                return result
    return None


def _epoch_to_iso(val):
    """Convert an epoch timestamp (seconds or milliseconds) to ISO 8601 string (UTC).
    Returns None if val is not a recognisable epoch."""
    try:
        ts = int(val)
    except (ValueError, TypeError):
        return None
    # Detect milliseconds vs seconds
    if ts > 1_000_000_000_000:
        ts = ts / 1000
    # Sanity: must fall between 2000-01-01 and 2100-01-01
    if not (946_684_800 <= ts <= 4_102_444_800):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def _parse_date_string_to_iso(val):
    """Parse a date string to ISO 8601.  Handles ISO 8601 and bare YYYY-MM-DD."""
    if not isinstance(val, str):
        return None
    val = val.strip()
    # Bare date YYYY-MM-DD → midnight UTC (check before fromisoformat, which returns naive datetime)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', val):
        return val + 'T00:00:00+00:00'
    # ISO 8601 (with or without timezone)
    try:
        return datetime.fromisoformat(val.replace('Z', '+00:00')).isoformat()
    except ValueError:
        pass
    return None


# Priority groups for JSON date extraction: (label, epoch_keys, date_string_keys)
# Searched in order; within each group epoch keys are tried before date string keys.
_JSON_DATE_PRIORITY = [
    (
        'published',
        ['time_published'],
        ['date_published', 'datePublished', 'publishedAt', 'published_at'],
    ),
    (
        'updated',
        ['time_updated'],
        ['date_updated', 'dateModified', 'updatedAt', 'updated_at'],
    ),
    (
        'created',
        ['time_created'],
        ['date_created', 'dateCreated', 'createdAt', 'created_at'],
    ),
]


def extract_date_from_json_scripts(html_input):
    """Search <script> tags for JSON data containing timestamp fields.

    Returns an ISO 8601 string (UTC) if a date is found, or None.
    Priority: published > updated > created; epoch timestamps > date strings.
    """
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html_input)
    except Exception:
        return None

    json_objects = []
    for script in doc.xpath('//script'):
        text = script.text or ''
        if not text.strip():
            continue
        json_objects.extend(_extract_json_objects_from_script(text))

    if not json_objects:
        return None

    for label, epoch_keys, date_keys in _JSON_DATE_PRIORITY:
        # Prefer epoch timestamps (more granular)
        for key in epoch_keys:
            for obj in json_objects:
                val = _find_key_in_obj(obj, key)
                if val is not None:
                    iso = _epoch_to_iso(val)
                    if iso:
                        log_debug(f"Extracted {label} date from JSON key '{key}': {iso}")
                        return iso
        # Fall back to date strings
        for key in date_keys:
            for obj in json_objects:
                val = _find_key_in_obj(obj, key)
                if val is not None:
                    iso = _parse_date_string_to_iso(val)
                    if iso:
                        log_debug(f"Extracted {label} date from JSON key '{key}': {iso}")
                        return iso

    return None


# Attribute priority for <article> tag date extraction: (label, attr_names)
_ARTICLE_DATE_PRIORITY = [
    ('published',  ['data-published', 'data-published-at', 'data-publish-date', 'data-publish-time', 'data-publication-date']),
    ('updated',    ['data-last-updated', 'data-updated', 'data-updated-at', 'data-update-date', 'data-modified', 'data-last-modified']),
    ('created',    ['data-created', 'data-created-at', 'data-create-date']),
]


def _parse_rfc2822_or_iso(val):
    """Parse an RFC 2822 date (e.g. 'Mon, 02 Mar 2026 00:53:31 GMT') or ISO 8601 string.
    Returns ISO 8601 string or None."""
    if not isinstance(val, str):
        return None
    val = val.strip()
    # Try RFC 2822 (handles HTTP-date and email Date headers)
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(val)
        return dt.isoformat()
    except Exception:
        pass
    # Fall back to ISO / bare date handling
    return _parse_date_string_to_iso(val)


def extract_date_from_article_tag(html_input):
    """Search <article> tag attributes for date/time metadata.

    Returns an ISO 8601 string if found, or None.
    Priority: published > updated > created.
    """
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html_input)
    except Exception:
        return None

    articles = doc.xpath('//article')
    if not articles:
        return None

    for label, attr_names in _ARTICLE_DATE_PRIORITY:
        for attr in attr_names:
            for article in articles:
                val = article.get(attr)
                if val:
                    iso = _parse_rfc2822_or_iso(val)
                    if iso:
                        log_debug(f"Extracted {label} date from <article> attribute '{attr}': {iso}")
                        return iso

    return None


def extract_date_from_time_element(html_input):
    """Search for <time datetime="..."> elements containing a publication date.

    Prefers elements inside <header> or with publication-related class names,
    then falls back to any <time datetime> in the document.

    Returns an ISO 8601 string if found, or None.
    """
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html_input)
    except Exception:
        return None

    # Prefer <time> elements that look like article publish timestamps:
    # inside <header>, or whose class name contains publication-related terms.
    pub_classes = ('pubdate', 'publish', 'published', 'date', 'timestamp', 'post-date')
    candidates = []
    for el in doc.xpath('//time[@datetime]'):
        dt_val = el.get('datetime', '').strip()
        if not dt_val:
            continue
        cls = (el.get('class') or '').lower()
        # Score: higher = more likely to be a publish date
        score = 0
        if any(term in cls for term in pub_classes):
            score += 2
        # Inside a <header> element?
        parent = el.getparent()
        while parent is not None:
            if parent.tag == 'header':
                score += 1
                break
            parent = parent.getparent()
        candidates.append((score, dt_val))

    # Sort by score descending, try each until one parses
    candidates.sort(key=lambda x: x[0], reverse=True)
    for score, dt_val in candidates:
        iso = _parse_date_string_to_iso(dt_val)
        if iso:
            log_debug(f"Extracted date from <time datetime> element: {iso}")
            return iso

    return None


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


def get_untagged_entries(base_url, token, detail="metadata", exhaustive=False):
    """Fetch all entries that have no tags.

    Paginates through entries (newest first) and filters client-side since the
    Wallabag API has no server-side filter for untagged entries.

    Args:
        base_url: Wallabag instance base URL.
        token: OAuth access token.
        detail: "metadata" for lightweight listing, "full" to include HTML content.
        exhaustive: If False (default), stop after 20 consecutive tagged entries.

    Returns:
        List of entry dicts with zero tags.
    """
    CONSECUTIVE_TAGGED_THRESHOLD = 20

    url = f"{base_url}/api/entries.json"
    headers = {"Authorization": f"Bearer {token}"}
    per_page = 30
    page = 1
    results = []
    consecutive_tagged = 0

    # First request to discover total pages
    params = {"perPage": per_page, "page": page, "detail": detail, "sort": "created", "order": "desc"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    total_pages = body.get("pages", 1)
    log_info(f"Scanning entries: {body.get('total', 0)} total across {total_pages} pages")

    while True:
        if page > 1:
            params = {"perPage": per_page, "page": page, "detail": detail, "sort": "created", "order": "desc"}
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()

        items = body.get("_embedded", {}).get("items", [])
        for entry in items:
            if not entry.get("tags", []):
                results.append(entry)
                consecutive_tagged = 0
            else:
                consecutive_tagged += 1

        print(f"  Scanned page {page}/{total_pages}, {len(results)} untagged so far...", file=sys.stderr)

        if not exhaustive and consecutive_tagged >= CONSECUTIVE_TAGGED_THRESHOLD:
            log_info(f"Stopping early: {consecutive_tagged} consecutive tagged entries seen.")
            break

        if page >= total_pages:
            break
        page += 1

    return results


def get_entries_for_tag(base_url, token, tag_label):
    """Fetch all entries that have a specific tag label."""
    url = f"{base_url}/api/entries.json"
    headers = {"Authorization": f"Bearer {token}"}
    per_page = 30
    page = 1
    results = []
    while True:
        params = {"tags": tag_label, "perPage": per_page, "page": page,
                  "detail": "metadata", "sort": "created", "order": "desc"}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        total_pages = body.get("pages", 1)
        results.extend(body.get("_embedded", {}).get("items", []))
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


def delete_tag(base_url, token, tag_id):
    """Delete a tag by its numeric ID."""
    url = f"{base_url}/api/tags/{tag_id}.json"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(url, headers=headers, timeout=30)
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


def _extract_author_from_user_name_div(user_name_div):
    """Extract display name and handle from a single User-Name element.

    Handles both main-tweet format (display name/handle in <a> tags) and
    quoted-tweet format (display name/handle in <div> tags with no links).
    Scans spans for text: first non-@ text = display name, first @-prefixed = handle.
    """
    display_name = None
    handle = None
    for span in user_name_div.xpath('.//span[not(ancestor::svg)]'):
        text = span.text_content().strip()
        if not text:
            continue
        if text.startswith('@') and handle is None:
            handle = text
        elif not text.startswith('@') and display_name is None:
            display_name = text
        if display_name and handle:
            break
    if display_name and handle:
        return f"{display_name} ({handle})"
    return display_name or handle


def _extract_handle_from_article(article):
    """Get tweet author's handle from UserAvatar-Container-{handle} data-testid in an article."""
    avatars = article.xpath('.//*[starts-with(@data-testid, "UserAvatar-Container-")]')
    if not avatars:
        return None
    testid = avatars[0].get('data-testid', '')
    prefix = 'UserAvatar-Container-'
    return testid[len(prefix):] if testid.startswith(prefix) else None


def _extract_twitter_author(doc):
    """Extract the tweet author's display name and handle from a parsed Twitter/X document."""
    user_name_divs = doc.xpath('//*[@data-testid="User-Name"]')
    if not user_name_divs:
        return None
    return _extract_author_from_user_name_div(user_name_divs[0])


def _extract_article_card(article):
    """Extract linked article URL and headline from a tweet's card preview, if present.

    Returns (url, headline) tuple, or (None, None) if no card found.
    """
    cards = article.xpath('.//*[@data-testid="card.wrapper"]')
    if not cards:
        return None, None
    card = cards[0]
    anchors = card.xpath('.//a[@role="link"]')
    if not anchors:
        return None, None
    anchor = anchors[0]
    url = anchor.get('href', '').strip() or None
    # Primary: parse headline from aria-label "domain.com Headline text"
    headline = None
    aria = anchor.get('aria-label', '')
    parts = aria.split(' ', 1)
    if len(parts) == 2 and parts[1].strip():
        headline = parts[1].strip()
    # Fallback: first non-empty innermost span text
    if not headline:
        for span in anchor.xpath('.//span[not(ancestor::svg)][not(.//span)]'):
            text = span.text_content().strip()
            if text:
                headline = text
                break
    return url, headline


def _extract_tweet_photos(article):
    """Return a list of <img> HTML strings for photos attached to a tweet article."""
    photo_divs = article.xpath('.//*[@data-testid="tweetPhoto"]')
    imgs = []
    for div in photo_divs:
        for img in div.xpath('.//img[@src]'):
            src = img.get('src', '').strip()
            if not src:
                continue
            src = src.replace('name=small', 'name=large').replace('name=medium', 'name=large')
            alt = html_module.escape(img.get('alt', '') or '')
            imgs.append(f'<img src="{html_module.escape(src)}" alt="{alt}" style="max-width:100%;">')
    return imgs


def _strip_twitter_noise(doc):
    """Remove Twitter UI chrome from a parsed lxml document in-place.

    Strips action buttons, view count/analytics link, 'View quotes' link,
    timestamp anchor, article card wrappers, and sidebar buttons.
    Ensures the readability fallback path produces clean HTML.
    """
    noise_xpaths = [
        '//*[@role="group"]',               # action button group (likes/reposts/replies)
        '//a[contains(@href, "/analytics")]',  # view count / analytics link
        '//a[contains(@href, "/quotes")]',     # "View quotes" link
        '//a[.//time]',                        # timestamp anchor
        '//*[@data-testid="card.wrapper"]',    # article card (extracted as annotation)
        '//button[@aria-label="Grok actions"]',
        '//button[@data-testid="caret"]',      # "More" menu button
    ]
    for xpath in noise_xpaths:
        for el in doc.xpath(xpath):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


def _extract_twitter_title(doc):
    """Extract a useful title from a parsed Twitter/X lxml document."""
    og_title = doc.xpath('//meta[@property="og:title"]/@content')
    if og_title and og_title[0].strip():
        return og_title[0].strip()
    title_tags = doc.xpath('//title/text()')
    if title_tags:
        t = title_tags[0].strip()
        if t and t not in ('X', 'Twitter', 'X / Twitter'):
            return t
    return None


def clean_twitter_html(html_input):
    """Extract tweet content from Twitter/X HTML, preserving paragraph structure.

    Twitter stores paragraph breaks as literal \\n\\n in text content rather than
    HTML block elements, causing Wallabag to collapse everything into one paragraph.
    Finds data-testid="tweetText" elements and converts newlines into <p> tags.
    Falls back to readability if no tweet elements are found.
    """
    from lxml import html as lxml_html

    doc = lxml_html.fromstring(html_input)
    tweet_divs = doc.xpath('//*[@data-testid="tweetText"]')

    if not tweet_divs:
        log_warning("No tweetText elements found in HTML; falling back to readability")
        _strip_twitter_noise(doc)
        title, cleaned = clean_html_with_readability(lxml_html.tostring(doc, encoding='unicode'))
        return title, cleaned, None, None

    def _build_para_parts(tweet_div):
        tweet_text = tweet_div.text_content()
        paragraphs = re.split(r'\n\n+', tweet_text)
        parts = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            # Convert remaining single newlines to <br>
            lines = [html_module.escape(line.strip()) for line in para.split('\n') if line.strip()]
            if lines:
                parts.append(f'<p>{"<br>".join(lines)}</p>')
        return parts

    output_parts = []

    # Try article-based processing to detect threads.
    # A thread is the initial run of consecutive articles by the same author.
    articles = doc.xpath('//article[@data-testid="tweet"]')
    if articles:
        first_handle = _extract_handle_from_article(articles[0])
        thread_articles = []
        for article in articles:
            if first_handle and _extract_handle_from_article(article) == first_handle:
                thread_articles.append(article)
            else:
                break

        # Detect if page was saved mid-scroll (early posts may be missing).
        # Twitter virtualizes off-screen content; if the first thread article
        # starts far down the page, posts above the saved viewport are gone.
        if thread_articles:
            parent_cells = thread_articles[0].xpath(
                'ancestor::*[@data-testid="cellInnerDiv"]'
            )
            if parent_cells:
                cell_style = parent_cells[0].get('style', '')
                m = re.search(r'translateY\(([0-9.]+)px\)', cell_style)
                if m and float(m.group(1)) > 500:
                    log_warning(
                        "Twitter thread appears to start mid-page — earlier posts may be "
                        "missing. To capture the full thread, scroll to the top before "
                        "saving the HTML."
                    )

        for article_idx, article in enumerate(thread_articles):
            tweet_texts = article.xpath('.//*[@data-testid="tweetText"]')
            user_names = article.xpath('.//*[@data-testid="User-Name"]')
            if not tweet_texts:
                continue
            if article_idx > 0:
                output_parts.append('<hr>')
            for j, tweet_div in enumerate(tweet_texts):
                para_parts = _build_para_parts(tweet_div)
                if j == 0:
                    output_parts.extend(para_parts)
                    output_parts.extend(_extract_tweet_photos(article))
                else:
                    output_parts.append('<hr>')
                    quoted_author = _extract_author_from_user_name_div(user_names[j]) if j < len(user_names) else None
                    header = f'<p><strong>Quoting {quoted_author}</strong></p>' if quoted_author else '<p><strong>Quoted tweet</strong></p>'
                    output_parts.append(header)
                    output_parts.append('<blockquote>')
                    output_parts.extend(para_parts)
                    output_parts.append('</blockquote>')
            card_url, card_headline = _extract_article_card(article)
            if card_url:
                link_text = html_module.escape(card_headline) if card_headline else html_module.escape(card_url)
                output_parts.append(f'<p><em>Linking to: <a href="{html_module.escape(card_url)}">{link_text}</a></em></p>')

    # Fallback: flat processing (original behavior, for HTML without article elements)
    if not output_parts:
        user_name_divs = doc.xpath('//*[@data-testid="User-Name"]')
        for i, tweet_div in enumerate(tweet_divs):
            para_parts = _build_para_parts(tweet_div)
            if i == 0:
                output_parts.extend(para_parts)
                for div in doc.xpath('//*[@data-testid="tweetPhoto"]'):
                    for img in div.xpath('.//img[@src]'):
                        src = img.get('src', '').strip()
                        if src:
                            src = src.replace('name=small', 'name=large').replace('name=medium', 'name=large')
                            alt = html_module.escape(img.get('alt', '') or '')
                            output_parts.append(f'<img src="{html_module.escape(src)}" alt="{alt}" style="max-width:100%;">')
            else:
                output_parts.append('<hr>')
                quoted_author = _extract_author_from_user_name_div(user_name_divs[i]) if i < len(user_name_divs) else None
                header = f'<p><strong>Quoting {quoted_author}</strong></p>' if quoted_author else '<p><strong>Quoted tweet</strong></p>'
                output_parts.append(header)
                output_parts.append('<blockquote>')
                output_parts.extend(para_parts)
                output_parts.append('</blockquote>')
        # Card annotation for flat-processing path (no article elements)
        card_divs = doc.xpath('//*[@data-testid="card.wrapper"]')
        for card in card_divs:
            anchors = card.xpath('.//a[@role="link"]')
            if not anchors:
                continue
            anchor = anchors[0]
            card_url = anchor.get('href', '').strip() or None
            if not card_url:
                continue
            card_headline = None
            aria = anchor.get('aria-label', '')
            parts = aria.split(' ', 1)
            if len(parts) == 2 and parts[1].strip():
                card_headline = parts[1].strip()
            if not card_headline:
                for span in anchor.xpath('.//span[not(ancestor::svg)][not(.//span)]'):
                    text = span.text_content().strip()
                    if text:
                        card_headline = text
                        break
            link_text = html_module.escape(card_headline) if card_headline else html_module.escape(card_url)
            output_parts.append(f'<p><em>Linking to: <a href="{html_module.escape(card_url)}">{link_text}</a></em></p>')

    if not output_parts:
        log_warning("No tweet content extracted; falling back to readability")
        _strip_twitter_noise(doc)
        title, cleaned = clean_html_with_readability(lxml_html.tostring(doc, encoding='unicode'))
        return title, cleaned, None, None

    title = _extract_twitter_title(doc)

    # Extract published_at from the first <time datetime="..."> tag
    time_tags = doc.xpath('//time/@datetime')
    tweet_time = time_tags[0].strip() if time_tags else None
    if tweet_time and tweet_time.endswith('Z'):
        tweet_time = tweet_time[:-1] + '+00:00'

    tweet_author = _extract_twitter_author(doc)

    return title, '\n'.join(output_parts), tweet_time, tweet_author


def clean_facebook_html(html_input):
    """Extract post content from a Facebook single-post page saved as HTML.

    Facebook pages are large app bundles with no semantic data-testid markers.
    This function finds the main post dialog, extracts the author and post text,
    and optionally wraps any shared article/post in a blockquote.
    Falls back to readability if the expected structure isn't found.
    """
    from lxml import html as lxml_html

    doc = lxml_html.fromstring(html_input)

    title_tags = doc.xpath('//title/text()')
    page_title = title_tags[0] if title_tags else ''
    author = _extract_fb_author_from_title(page_title)

    dialogs = doc.xpath('//div[@role="dialog"]')
    if not dialogs:
        log_warning("No dialog elements found in Facebook HTML; falling back to readability")
        title, cleaned = clean_html_with_readability(html_input)
        return title, cleaned, None, author

    main_dialog = _find_fb_main_dialog(dialogs)

    author_link = _find_fb_author_link(main_dialog, author)
    if author_link is None:
        log_warning("Could not find author link in Facebook HTML; falling back to readability")
        title, cleaned = clean_html_with_readability(html_input)
        return title, cleaned, None, author

    if author is None:
        author = author_link.text_content().strip() or None

    post_content_node = _find_fb_post_content(author_link)
    if post_content_node is None:
        log_warning("Could not find post content in Facebook HTML; falling back to readability")
        title, cleaned = clean_html_with_readability(html_input)
        return title, cleaned, None, author

    content_children = [e for e in post_content_node
                        if hasattr(e, 'tag') and not callable(e.tag)]

    if not content_children:
        main_text_node = post_content_node
        shared_card_node = None
    elif len(content_children) == 1:
        main_text_node = content_children[0]
        shared_card_node = None
    else:
        main_text_node = content_children[0]
        candidate = content_children[1]
        shared_card_node = candidate if len(candidate.text_content().strip()) > 50 else None

    output_parts = []

    # Facebook renders each paragraph as a leaf <div dir="auto" style="..."> element.
    # text_content() flattens them all together; instead, collect each leaf div's text
    # as its own paragraph to preserve breaks.
    para_divs = main_text_node.xpath('.//div[@dir][@style]')
    leaf_para_divs = [d for d in para_divs if not d.xpath('.//div[@dir][@style]')]

    if leaf_para_divs:
        for d in leaf_para_divs:
            text = d.text_content().strip()
            if text:
                output_parts.append(f'<p>{html_module.escape(text)}</p>')
    else:
        # Fallback: use text_content with newline splitting
        main_text = main_text_node.text_content().strip()
        if main_text:
            paragraphs = re.split(r'\n\n+', main_text)
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                lines = [html_module.escape(l.strip()) for l in para.split('\n') if l.strip()]
                if lines:
                    output_parts.append(f'<p>{"<br>".join(lines)}</p>')

    if shared_card_node is not None:
        source_name, article_text = _extract_fb_shared_card(shared_card_node)
        if article_text:
            output_parts.append('<hr>')
            label = f'Shared: {source_name}' if source_name else 'Shared content'
            output_parts.append(f'<p><strong>{html_module.escape(label)}</strong></p>')
            output_parts.append('<blockquote>')
            for para in re.split(r'\n\n+', article_text):
                para = para.strip()
                if para:
                    output_parts.append(f'<p>{html_module.escape(para)}</p>')
            output_parts.append('</blockquote>')

    if not output_parts:
        log_warning("No Facebook post content extracted; falling back to readability")
        title, cleaned = clean_html_with_readability(html_input)
        return title, cleaned, None, author

    post_time = _extract_fb_creation_time(html_input, author_link)

    title = _clean_fb_page_title(page_title)
    return title, '\n'.join(output_parts), post_time, author


def clean_linkedin_html(html_input):
    """Extract post content from a LinkedIn single-post page saved as HTML.

    LinkedIn pages are large Ember app bundles. This function finds the main
    post content using update-components-* class markers, extracts the author
    from the actor meta link's aria-label (ignoring any repost header), and
    returns the post text as HTML paragraphs. No absolute timestamp is available
    in saved LinkedIn HTML, so published_at is always returned as None.
    """
    from lxml import html as lxml_html
    import html as html_module

    doc = lxml_html.fromstring(html_input)

    author = _extract_linkedin_author(doc)

    content_el = _find_linkedin_post_content(doc)
    if content_el is None:
        log_warning("Could not find LinkedIn post content; falling back to readability")
        title, cleaned = clean_html_with_readability(html_input)
        return title, cleaned, None, author

    output_parts = _extract_linkedin_paragraphs(content_el, html_module)

    if not output_parts:
        log_warning("No LinkedIn post content extracted; falling back to readability")
        title, cleaned = clean_html_with_readability(html_input)
        return title, cleaned, None, author

    cleaned = '\n'.join(output_parts)
    title = author or "LinkedIn Post"
    return title, cleaned, None, author


def _extract_linkedin_author(doc):
    """Extract post author from a LinkedIn page DOM.

    Reads the aria-label of the actor meta link, which has the form:
      "View: Keith Mularski Premium • 1st Chief Global Ambassador ..."
    Strips the "View: " prefix and stops before the LinkedIn badge or first bullet.
    Falls back to the actor title span text if the aria-label is absent.
    """
    # Primary: actor meta link aria-label
    links = doc.xpath('//*[contains(@class,"update-components-actor__meta-link")]')
    for link in links:
        aria = (link.get('aria-label') or '').strip()
        if aria.startswith('View:'):
            name = aria[len('View:'):].strip()
            # Strip LinkedIn badge suffixes like " Premium •" or " Creator •" or just " •"
            m = re.match(r'^(.+?)(?:\s+(?:Premium|Creator|Open to Work|Hiring)\s*•|\s*•)', name)
            if m:
                return m.group(1).strip()
            # No badge — return everything before the first " • " separator
            if ' • ' in name:
                return name.split(' • ')[0].strip()
            return name

    # Fallback: actor title span
    titles = doc.xpath('//*[contains(@class,"update-components-actor__title")]')
    for title_el in titles:
        parts = [t.strip() for t in title_el.itertext() if t.strip()]
        if parts:
            return parts[0]

    return None


def _find_linkedin_post_content(doc):
    """Find the main post commentary element in a LinkedIn page DOM."""
    els = doc.xpath('//*[contains(@class,"update-components-update-v2__commentary")]')
    return els[0] if els else None


def _extract_linkedin_paragraphs(content_el, html_module):
    """Convert a LinkedIn post content element to a list of <p> HTML strings.

    LinkedIn renders post text with paragraph breaks represented as consecutive
    <span><br/></span> elements. We walk the DOM, emitting a newline for each
    <br> encountered, then split the result on double newlines to produce
    paragraph-level <p> tags.
    """
    # Walk the element tree, collecting text and inserting \n for each <br>
    def collect_text(el):
        parts = []
        if el.text:
            parts.append(el.text)
        for child in el:
            tag = getattr(child, 'tag', None)
            if tag == 'br':
                parts.append('\n')
            else:
                parts.extend(collect_text(child))
            if child.tail:
                parts.append(child.tail)
        return parts

    raw = ''.join(collect_text(content_el)).strip()
    if not raw:
        return []

    output_parts = []
    paragraphs = re.split(r'\n\n+', raw)
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        lines = [html_module.escape(l.strip()) for l in para.split('\n') if l.strip()]
        if lines:
            output_parts.append(f'<p>{"<br>".join(lines)}</p>')
    return output_parts


def _extract_fb_creation_time(html_input, author_link):
    """Extract post creation time from embedded JSON in Facebook HTML.

    Finds a "story":{"creation_time":...,"url":".../{slug}/..."} block whose URL
    matches the author's profile slug, then converts the unix epoch to ISO 8601.
    """
    from urllib.parse import urlparse
    from datetime import datetime, timezone

    href = author_link.get('href', '')
    slug = urlparse(href).path.strip('/')
    if not slug:
        return None

    # JSON encodes slashes as \/, match one literal backslash then slash via \\/
    pattern = (
        r'"story":\{"creation_time":(\d+),"url":"https:\\/\\/www\.facebook\.com\\/'
        + re.escape(slug) + r'\\/'
    )
    m = re.search(pattern, html_input)
    if not m:
        return None

    ts = int(m.group(1))
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _find_fb_main_dialog(dialogs):
    """Find the main post dialog, skipping the notifications panel."""
    for d in dialogs:
        if not d.text_content().strip().startswith('Notifications'):
            return d
    return dialogs[0]


def _extract_fb_author_from_title(title):
    """Extract author name from Facebook page title.

    Handles formats:
      "(2) Tyler Huckabee - snippet... | Facebook"
      "Tyler Huckabee - snippet... | Facebook"
    """
    m = re.match(r'^\(\d+\)\s+(.+?)\s+-\s+', title)
    if m:
        return m.group(1)
    m = re.match(r'^(.+?)\s+-\s+', title)
    if m:
        candidate = m.group(1)
        if candidate and len(candidate) < 100 and 'facebook.com' not in candidate.lower():
            return candidate
    return None


def _clean_fb_page_title(title):
    """Remove Facebook chrome from page title to get a usable post title."""
    title = re.sub(r'^\(\d+\)\s+', '', title)
    title = re.sub(r'\s*\|\s*Facebook\s*$', '', title)
    title = re.sub(r'[.\s\u2014\u2013-]+$', '', title)  # strip trailing dots, spaces, dashes
    return title.strip() or None


def _find_fb_author_link(dialog, author_name):
    """Find the post author's profile link in the dialog (not a comment link)."""
    for a in dialog.xpath('.//a[@href]'):
        href = a.get('href', '')
        text = a.text_content().strip()
        if 'comment_id' not in href and author_name and text == author_name:
            return a
    # Fallback: first non-trivial profile link without comment_id
    for a in dialog.xpath('.//a[@href]'):
        href = a.get('href', '')
        text = a.text_content().strip()
        if ('comment_id' not in href
                and 'facebook.com/' in href
                and len(text) > 1
                and text not in ('Facebook', 'Like', 'Comment', 'Share', '')
                and not text.startswith('http')):
            return a
    return None


def _find_fb_post_content(author_link):
    """Walk up from the author link to find the post content container.

    The author link lives inside the post header div (author + timestamp).
    The post content div is the next sibling of that header at some ancestor level.
    We identify it by having substantial text and no action-bar markers.
    """
    parent = author_link
    for _ in range(30):
        parent = parent.getparent()
        if parent is None:
            break
        sib = parent.getnext()
        if sib is not None and not callable(sib.tag):
            text = sib.text_content()
            if (len(text) > 200
                    and 'LikeCommentShare' not in text
                    and 'All reactions' not in text):
                return sib
    return None


def _extract_fb_shared_card(card_node):
    """Extract source name and article text from a Facebook shared content card.

    The card typically has an h4 element containing the source page link,
    followed by a sibling div with the clean article description text.
    """
    source_name = None
    article_text = None

    h4s = card_node.xpath('.//h4')
    for h4 in h4s:
        links = h4.xpath('.//a')
        if links:
            source_name = links[0].text_content().strip()
            break

    # Walk up from h4 until we find a next sibling that looks like article text.
    # Skip scrambled timestamp divs (they end with "Shared with Public/Friends").
    if h4s:
        node = h4s[0]
        for _ in range(15):
            sib = node.getnext()
            if sib is not None and not callable(sib.tag):
                text = sib.text_content().strip()
                if len(text) > 50 and 'Shared with' not in text:
                    article_text = text
                    break
            node = node.getparent()
            if node is None:
                break

    return source_name, article_text


def _is_nyt_birdkit(html_input):
    """Return True if the HTML appears to be an NYT birdkit interactive article."""
    return 'birdkit-body' in html_input or 'data-birdkit-hydrate' in html_input


def clean_nyt_birdkit_html(html_input):
    """Extract content from an NYT birdkit/Svelte interactive article.

    These articles use custom CSS class names (g-heading, g-byline, g-body-text,
    item-name, item-subhed, item-blurb, status) rather than semantic HTML.
    Returns (title, cleaned_html, published_at, author).
    Falls back to readability if the expected structure isn't found.
    """
    import html as html_module
    from lxml import html as lxml_html

    doc = lxml_html.fromstring(html_input)

    # Title
    h1s = doc.xpath("//h1[contains(@class,'g-heading')]")
    title = h1s[0].text_content().strip() if h1s else None

    # Author — strip leading "By "
    bylines = doc.xpath("//p[contains(@class,'g-byline')]")
    author = None
    if bylines:
        raw = bylines[0].text_content().strip()
        author = re.sub(r'^By\s+', '', raw) or None

    # Date — from the birdkit timestamp element
    times = doc.xpath("//time[contains(@class,'g-interactive-timestamp')]/@datetime")
    published_at = times[0].strip() if times else None

    output_parts = []

    # Intro paragraphs
    for p in doc.xpath("//p[contains(@class,'g-body-text')]"):
        text = p.text_content().strip()
        if text:
            output_parts.append(f'<p>{html_module.escape(text)}</p>')

    # Sections — only direct-child sections that own a status heading
    for section in doc.xpath("//section[./div[contains(@class,'status')]]"):
        status_divs = section.xpath("./div[contains(@class,'status')]")
        if status_divs:
            heading = status_divs[0].text_content().strip()
            if heading:
                output_parts.append(f'<h2>{html_module.escape(heading)}</h2>')

        for item in section.xpath("./div[contains(@class,'item')]"):
            name_els = item.xpath(".//div[contains(@class,'item-name')]")
            subhed_els = item.xpath(".//div[contains(@class,'item-subhed')]")
            blurb_els = item.xpath(".//div[contains(@class,'item-blurb')]")

            name = name_els[0].text_content().strip() if name_els else ''
            subhed = subhed_els[0].text_content().strip() if subhed_els else ''
            blurb = blurb_els[0].text_content().strip() if blurb_els else ''

            if name:
                output_parts.append(f'<h3>{html_module.escape(name)}</h3>')
            if subhed:
                output_parts.append(f'<p><em>{html_module.escape(subhed)}</em></p>')
            if blurb:
                output_parts.append(f'<p>{html_module.escape(blurb)}</p>')

    if not output_parts:
        log_warning("No birdkit content extracted; falling back to readability")
        fb_title, cleaned = clean_html_with_readability(html_input)
        return fb_title or title, cleaned, published_at, author

    return title, '\n'.join(output_parts), published_at, author


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
- STRICT EVIDENCE RULE: Before applying any tag, you must be able to point to specific text in the article that directly supports it. If a tag's subject is not explicitly named or clearly described in the article, do NOT apply that tag — even if you think it might be tangentially related.
- SUBSTRING RULE: A tag name must appear as a meaningful, standalone reference in the article — not merely as a substring within an unrelated word. For example, do NOT apply "vance" because the word "advanced" appears, "ice" because "service" appears, or "apt" because "chapter" appears.
- EVIDENCE REQUIRED IN RESPONSE: For each tag you select, you must include a direct verbatim quote from the article that supports it.

{tag_notes_block}Select tags from the allowed list ("existing").
Only put non-duplicates into "proposed_new" if a new tag would be clearly valuable and nothing in the allowed list fits."""


def _build_headline_system_prompt() -> str:
    """Build the system prompt for Twitter headline generation."""
    return (
        "You are a headline writer for a personal reading archive. "
        "Given the text of a tweet or Twitter/X post, write a single short, factual, descriptive headline.\n\n"
        "Rules:\n"
        "- Output ONLY the headline — no options, no alternatives, no explanation\n"
        "- Be descriptive and informative, not creative or witty\n"
        "- Do not use humor, puns, or rhetorical flair\n"
        "- Keep it concise (typically 6–12 words)\n"
        "- If the tweet is a quote-tweet responding to another post, describe the top (outer) post; "
        "you may note who is responding to whom if it is clear, but this is not required\n"
        "- Do not start with 'Tweet:', 'Post:', or similar prefixes"
    )


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
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string", "enum": allowed_tags},
                        "evidence": {"type": "string", "description": "Direct verbatim quote from the article supporting this tag"}
                    },
                    "required": ["tag", "evidence"],
                    "additionalProperties": False
                },
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
        "temperature": 0,
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
    log_debug(f"Tagging system prompt:\n{system_prompt}")

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

    existing_items = parsed_json.get("existing", [])
    llm_existing = [item["tag"] for item in existing_items if isinstance(item, dict) and "tag" in item]
    for item in existing_items:
        if isinstance(item, dict):
            log_info(f"  tag={item.get('tag')!r} evidence={item.get('evidence', '')!r}")

    return llm_existing, parsed_json.get("proposed_new", [])


def choose_tags_with_ollama(ollama_url: str, model: str, article_text: str, allowed_tags: list[str], max_tags: int = 6, tag_notes: dict | None = None, api_key: str | None = None):
    """Select tags using Ollama /api/generate endpoint."""
    system_prompt = _build_tagging_system_prompt(tag_notes)

    tags_list = ", ".join(f'"{t}"' for t in allowed_tags)

    prompt = f"""{system_prompt}

Allowed tags: [{tags_list}]

Respond with ONLY valid JSON in this exact format (no other text):
{{"existing": [{{"tag": "tag1", "evidence": "direct quote from article"}}, {{"tag": "tag2", "evidence": "direct quote from article"}}], "proposed_new": ["new_tag"]}}

"existing" must only contain tags from the allowed list above (max {max_tags}), each with a verbatim evidence quote.
"proposed_new" may contain up to 3 new tags only if nothing in the allowed list fits.

Tag the following article:

{article_text[:12000]}"""

    log_debug(f"Tagging system prompt:\n{system_prompt}")
    response_text = _ollama_request(ollama_url, model, prompt, api_key=api_key)
    parsed = _parse_json_response(response_text)

    # Validate existing tags against allowed list
    existing_items = parsed.get("existing", [])
    existing = [item["tag"] for item in existing_items if isinstance(item, dict) and "tag" in item and item["tag"] in allowed_tags][:max_tags]
    for item in existing_items:
        if isinstance(item, dict):
            log_info(f"  tag={item.get('tag')!r} evidence={item.get('evidence', '')!r}")
    proposed = parsed.get("proposed_new", [])[:3]

    return existing, proposed



def _generate_headline_openai(api_key: str, model: str, tweet_text: str) -> str | None:
    """Generate a headline for a tweet using the OpenAI chat completions API."""
    system_prompt = _build_headline_system_prompt()

    schema = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"}
        },
        "required": ["headline"],
        "additionalProperties": False
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Write a headline for this tweet:\n\n{tweet_text[:4000]}"}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "headline",
                "schema": schema,
                "strict": True
            }
        }
    }

    log_debug(f"Calling OpenAI API for headline with model: {model}")

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
    return parsed_json.get("headline") or None


def _generate_headline_ollama(ollama_url: str, model: str, tweet_text: str, api_key: str | None = None) -> str | None:
    """Generate a headline for a tweet using the Ollama /api/generate endpoint."""
    system_prompt = _build_headline_system_prompt()

    prompt = f"""{system_prompt}

Respond with ONLY valid JSON in this exact format (no other text):
{{"headline": "your headline here"}}

Write a headline for this tweet:

{tweet_text[:4000]}"""

    response_text = _ollama_request(ollama_url, model, prompt, api_key=api_key, num_predict=200)
    parsed = _parse_json_response(response_text)
    return parsed.get("headline") or None


def generate_twitter_headline_with_llm(config, tweet_text: str) -> str | None:
    """Generate a descriptive headline for a tweet using the configured LLM provider."""
    provider = config.get("WALLABAG", "LLM_PROVIDER", fallback="").lower()
    if provider == "openai":
        api_key = config["OPENAI"].get("API_KEY", "")
        if not api_key:
            return None
        model = config["OPENAI"].get("TAG_MODEL", "gpt-4o-mini")
        return _generate_headline_openai(api_key, model, tweet_text)
    elif provider == "ollama":
        url = config["OLLAMA"].get("URL", "http://localhost:11434")
        model = config["OLLAMA"].get("MODEL", "")
        api_key = config["OLLAMA"].get("API_KEY") or None
        return _generate_headline_ollama(url, model, tweet_text, api_key=api_key)
    return None


######################################
# Article HTML Export
######################################
def _slugify(text, max_len=60):
    """Convert text to a filesystem-safe slug."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text[:max_len]


def render_article_html(entry):
    """Render a Wallabag entry as a self-contained, mobile-friendly HTML document."""
    title = entry.get("title") or "Untitled"
    content = entry.get("content") or ""
    authors = entry.get("authors") or ""
    url = entry.get("url") or ""
    tags = [t["label"] for t in (entry.get("tags") or []) if t.get("label")]

    # Format publication date
    published_raw = entry.get("published_at") or ""
    date_display = ""
    if published_raw:
        try:
            dt = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            date_display = dt.strftime("%B %-d, %Y")
        except (ValueError, AttributeError):
            date_display = published_raw

    # Build meta line (author · date)
    meta_parts = []
    if authors:
        meta_parts.append(f'<span class="author">{html_module.escape(authors)}</span>')
    if date_display:
        meta_parts.append(f'<span class="date">{html_module.escape(date_display)}</span>')
    meta_html = '<div class="meta">' + "".join(meta_parts) + "</div>" if meta_parts else ""

    # Build tags
    tags_html = ""
    if tags:
        tag_spans = "".join(f'<span class="tag">{html_module.escape(t)}</span>' for t in tags)
        tags_html = f'<div class="tags">{tag_spans}</div>'

    # Build source link
    source_html = ""
    if url:
        escaped_url = html_module.escape(url)
        source_html = f'<div class="source"><a href="{escaped_url}">{escaped_url}</a></div>'

    escaped_title = html_module.escape(title)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escaped_title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 18px;
      line-height: 1.7;
      color: #222;
      background: #fafaf8;
      margin: 0;
      padding: 1rem;
    }}
    article {{
      max-width: 720px;
      margin: 2rem auto;
      padding: 0 1rem;
    }}
    header {{
      margin-bottom: 2rem;
      border-bottom: 1px solid #ddd;
      padding-bottom: 1.25rem;
    }}
    h1 {{
      font-size: 1.8rem;
      line-height: 1.25;
      margin: 0 0 0.75rem;
      color: #111;
    }}
    .meta {{
      font-family: system-ui, sans-serif;
      font-size: 0.875rem;
      color: #666;
    }}
    .meta span + span::before {{ content: " \00b7 "; }}
    .tags {{ margin-top: 0.5rem; }}
    .tag {{
      display: inline-block;
      background: #eee;
      border-radius: 3px;
      padding: 0.1em 0.5em;
      font-size: 0.8rem;
      font-family: system-ui, sans-serif;
      color: #555;
      margin: 0.2em 0.2em 0.2em 0;
    }}
    .source {{
      font-family: system-ui, sans-serif;
      font-size: 0.8rem;
      margin-top: 0.5rem;
      word-break: break-all;
    }}
    .source a {{ color: #0066cc; }}
    .content img {{ max-width: 100%; height: auto; }}
    .content a {{ color: #0066cc; }}
    .content pre, .content code {{
      font-size: 0.875rem;
      background: #f4f4f0;
      border-radius: 3px;
      padding: 0.1em 0.3em;
    }}
    .content pre {{ padding: 1rem; overflow-x: auto; }}
    .content pre code {{ background: none; padding: 0; }}
    .content blockquote {{
      border-left: 4px solid #ccc;
      margin-left: 0;
      padding-left: 1rem;
      color: #555;
      font-style: italic;
    }}
    @media (max-width: 480px) {{
      body {{ font-size: 16px; padding: 0.5rem; }}
      h1 {{ font-size: 1.4rem; }}
      article {{ margin: 0.5rem auto; }}
    }}
  </style>
</head>
<body>
  <article>
    <header>
      <h1>{escaped_title}</h1>
      {meta_html}
      {tags_html}
      {source_html}
    </header>
    <div class="content">
      {content}
    </div>
  </article>
</body>
</html>"""


#
# Initial Setup and call to main()
#
if __name__ == '__main__':
    #sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 1)  # reopen STDOUT unbuffered
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
