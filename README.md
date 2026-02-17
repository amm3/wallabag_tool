# Wallabag Tool

A command-line tool for managing articles in Wallabag with AI-powered tagging.

## Features

- Multiple Input Methods: Add articles from URLs, HTML files, or stdin
- AI Integration: Automatic tagging using OpenAI or Ollama
- Smart Tagging: LLM-powered tag selection from your existing tags
- Clean Content: Uses Readability to extract article content
- Update & Create: Add new articles or update existing ones by URL

## Table of Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Basic Usage](#basic-usage)
- [AI Features](#ai-features)
- [Advanced Usage](#advanced-usage)
- [Troubleshooting](#troubleshooting)
- [Examples](#examples)

## Installation

### Requirements

```bash
pip install readability-lxml requests lxml
```

### Setup

1. Download the script
2. Make the main script executable:
   ```bash
   chmod +x wallabag_tool.py
   ```

## Configuration

Create a configuration file at `~/.wallabag`:

```ini
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
API_KEY =  # Optional, for nginx proxy auth

# Optional: help the LLM disambiguate tag names
[TAGNOTES]
ai = Artificial Intelligence
ice = U.S. Immigration and Customs Enforcement (ICE), not frozen water
infosec = information security and computer hacking
```

### Configuration Options

#### [WALLABAG] Section
- `BASEURL` **(required)**: Your Wallabag instance URL
- `CLIENTID` **(required)**: OAuth client ID
- `CLIENTSECRET` **(required)**: OAuth client secret
- `USERNAME` **(required)**: Your Wallabag username
- `PASSWORD` **(required)**: Your Wallabag password
- `LLM_PROVIDER` *(optional)*: LLM provider to use for tagging (`openai` or `ollama`, default: `openai`)

#### [OPENAI] Section
- `API_KEY` *(optional)*: OpenAI API key for AI features
- `TAG_MODEL` *(optional)*: Model to use for tagging (default: gpt-4o-mini)

#### [OLLAMA] Section *(optional)*
- `URL` *(optional)*: Ollama server URL (default: `http://localhost:11434`)
- `MODEL` *(optional)*: Model name to use (default: `llama3.1:8b`)
- `API_KEY` *(optional)*: API key for nginx proxy authentication (sent as `X-Ollama-Key` header)

#### [TAGNOTES] Section *(optional)*
Helps the LLM correctly interpret ambiguous tag names. Each key is a tag name and the value is a short description of what it refers to.

## Basic Usage

### Add Article from URL

```bash
# Simple add
./wallabag_tool.py --url "https://example.com/article"

# With custom tags
./wallabag_tool.py --url "https://example.com/article" --tags "tech,interesting"

# With custom title
./wallabag_tool.py --url "https://example.com/article" --title "My Custom Title"

# Skip if already exists
./wallabag_tool.py --url "https://example.com/article" --skip-existing
```

### Add Article from HTML File

```bash
# From file
./wallabag_tool.py article.html

# From stdin
cat article.html | ./wallabag_tool.py -

# With tags
./wallabag_tool.py article.html --tags "saved,reading-list"
```

### Update Existing Article

```bash
# Update by ID
./wallabag_tool.py --id 123 article.html

# Update the most recently created article (e.g. paste HTML from clipboard)
pbpaste | ./wallabag_tool.py --last -

# Update from URL (updates if exists, creates if not)
./wallabag_tool.py --url "https://example.com/article"
```

### List Tags

```bash
./wallabag_tool.py --list-tags
```

### Dump Article HTML

```bash
./wallabag_tool.py --dump-html --id 123 > article.html
```

## AI Features

### Automatic Tagging

When an LLM provider is configured (OpenAI or Ollama), the tool can automatically select relevant tags:

```bash
# AI will choose from your existing tags
./wallabag_tool.py --url "https://tech-article.com"

# Combine with manual tags
./wallabag_tool.py --url "https://tech-article.com" --tags "manual-tag"
```

The AI:
- Analyzes article content
- Selects up to 6 tags from your existing Wallabag tags
- Can suggest new tags if none match well

## Common Workflows

**Update the most recent article with better HTML from the clipboard** (macOS). Useful when Wallabag couldn't get past a paywall, but you already have the HTML in your desktop browser:

```bash
pbpaste | wallabag_tool.py -l -
```

**Retag the most recent article** (any OS):

```bash
wallabag_tool.py -lr
```

## Advanced Usage

### Combining Features

```bash
./wallabag_tool.py \
  --url "https://example.com/article" \
  --tags "important,tech" \
  -v
```

### URL Processing

The tool automatically:
- Strips UTM tracking parameters
- Checks for existing entries before creating duplicates
- Cleans HTML using Readability

### Verbose Logging

```bash
# Basic verbose
./wallabag_tool.py --url "https://example.com" -v

# Debug level
./wallabag_tool.py --url "https://example.com" -vv
```

### Custom Config File

```bash
./wallabag_tool.py -c /path/to/config --url "https://example.com"
```

## Troubleshooting

### Entry Already Exists

The tool checks for existing URLs before creating duplicates. To force update:
```bash
# This will update if exists, create if not
./wallabag_tool.py --url "https://example.com"
```

To skip if exists:
```bash
./wallabag_tool.py --url "https://example.com" --skip-existing
```

### API Rate Limits

- OpenAI has rate limits; if you hit them, wait a few minutes
- Consider upgrading your OpenAI plan for higher limits
- The tool processes one article at a time to avoid overwhelming APIs

### Content Extraction Issues

If article content isn't extracting correctly:
- Some sites have complex HTML that Readability struggles with
- Manual HTML editing may be needed for some sites

## Examples

### Daily Workflow

```bash
#!/bin/bash
# save_article.sh - Save and process an article

URL="$1"

./wallabag_tool.py \
  --url "$URL" \
  -v

echo "Article saved to Wallabag!"
```

### Batch Processing

```bash
#!/bin/bash
# Process multiple URLs from a file

while IFS= read -r url; do
    echo "Processing: $url"
    ./wallabag_tool.py --url "$url" --skip-existing
    sleep 2  # Rate limiting
done < urls.txt
```

### Research Workflow

```bash
# Save research article with specific tags
./wallabag_tool.py \
  --url "https://arxiv.org/abs/..." \
  --tags "research,ai,papers" \
  --title "Paper: Novel AI Architecture"
```

## Command Reference

```
usage: wallabag_tool.py [-h] [-v] [-vv] [-c [CONFIG]] [--url URL] [--title TITLE]
                        [--tags TAGS] [--skip-existing] [-i ID] [-l] [--list-tags]
                        [--dump-html] [-r] [--clean]
                        [HTML_FILE]

Options:
  -v                    Print extra info
  -vv                   Print debug info
  -c CONFIG             Config file (default: ~/.wallabag)

URL Operations:
  --url URL             URL to add or update
  --skip-existing       Skip if entry already exists

Content:
  HTML_FILE             HTML file or '-' for stdin
  --title TITLE         Custom title
  --tags TAGS           Comma-separated tags
  -i ID, --id ID        Entry ID to update
  -l, --last            Update the most recently created entry
  --clean               Use readability preprocessing to extract article content

Other:
  --list-tags                   List all tags
  --dump-html --id ID   Export entry HTML
  -r, --retag           Re-run LLM tagging on an existing entry
```

## Tips & Best Practices

1. **Tag Consistently**: Let AI help but maintain a core set of manual tags for organization
2. **Verbose for Debugging**: Always use `-v` or `-vv` when troubleshooting
3. **Monitor Token Usage**: AI features use tokens; monitor your usage

## License

This tool is provided as-is for personal use with Wallabag instances.

## Support

For issues related to:
- **Wallabag API**: Check Wallabag documentation
- **OpenAI**: Verify API key and check usage limits
