#!/usr/bin/env python3

import sys
import os
import argparse
import logging
import time
from html.parser import HTMLParser
from html import unescape

DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOGGING_FORMAT = '%(asctime)s:%(levelname)s:%(message)s'

# Tags we want to preserve
ALLOWED_TAGS = {'p', 'br', 'b', 'strong', 'i', 'em', 'u'}

# Block-level elements that indicate structural breaks (disjointed text)
BLOCK_LEVEL_TAGS = {
    'div', 'section', 'article', 'nav', 'header', 'footer', 'aside',
    'ul', 'ol', 'dl', 'li', 'table', 'tr', 'td', 'th', 'thead', 'tbody',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'hr',
    'form', 'fieldset', 'figure', 'figcaption', 'main', 'address'
}

class HTMLSimplifier(HTMLParser):
    """Parser that extracts text and preserves only basic formatting tags"""
    
    def __init__(self):
        super().__init__()
        self.output = []
        self.current_text = []
        self.in_paragraph = False
        self.tag_stack = []
        self.block_stack = []  # Track block-level elements
        self.last_was_block_end = False  # Track if we just closed a block element
        
    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        
        # Track block-level elements
        if tag_lower in BLOCK_LEVEL_TAGS:
            self.block_stack.append(tag_lower)
            # If we're in a paragraph and hit a block element, it's disjointed
            if self.in_paragraph and self.current_text:
                self.flush_text()
                self.output.append('</p>')
                self.in_paragraph = False
                # Add extra break for disjoint
                self.output.append('<br>')
        
        # Handle paragraph tags
        if tag_lower == 'p':
            if self.current_text:
                self.flush_text()
            # If last thing was a block end, this is disjointed content
            if self.last_was_block_end and self.output and not self.output[-1].endswith('<br>'):
                self.output.append('<br>')
            self.in_paragraph = True
            self.output.append('<p>')
            self.last_was_block_end = False
            
        # Handle line breaks
        elif tag_lower == 'br':
            self.output.append('<br>')
            self.last_was_block_end = False
            
        # Handle allowed formatting tags
        elif tag_lower in ALLOWED_TAGS:
            self.tag_stack.append(tag_lower)
            self.output.append(f'<{tag_lower}>')
            self.last_was_block_end = False
    
    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        
        # Track block-level element closures
        if tag_lower in BLOCK_LEVEL_TAGS and tag_lower in self.block_stack:
            self.block_stack.remove(tag_lower)
            self.last_was_block_end = True
            # Close paragraph if we're in one
            if self.in_paragraph:
                self.flush_text()
                self.output.append('</p>')
                self.in_paragraph = False
        
        if tag_lower == 'p':
            self.flush_text()
            self.output.append('</p>')
            self.in_paragraph = False
            self.last_was_block_end = False
            
        elif tag_lower in ALLOWED_TAGS and tag_lower in self.tag_stack:
            # Close any formatting tags in reverse order up to this one
            if tag_lower in self.tag_stack:
                idx = len(self.tag_stack) - 1 - self.tag_stack[::-1].index(tag_lower)
                self.tag_stack.pop(idx)
                self.output.append(f'</{tag_lower}>')
                self.last_was_block_end = False
    
    def handle_data(self, data):
        # Collect text data
        text = data.strip()
        if text:
            if not self.in_paragraph:
                # Start with a paragraph if we have text but no paragraph yet
                if self.last_was_block_end and self.output and not self.output[-1].endswith('<br>'):
                    self.output.append('<br>')
                self.output.append('<p>')
                self.in_paragraph = True
            self.current_text.append(data)
            self.last_was_block_end = False
    
    def flush_text(self):
        """Add accumulated text to output"""
        if self.current_text:
            text = ''.join(self.current_text)
            # Normalize whitespace but preserve single spaces
            text = ' '.join(text.split())
            if text:
                self.output.append(text)
            self.current_text = []
    
    def get_output(self):
        """Get the simplified HTML output with line breaks after paragraphs"""
        self.flush_text()
        
        # Close any unclosed paragraph
        if self.in_paragraph:
            self.output.append('</p>')
        
        # Join output and add line breaks after each </p> tag
        result = ''.join(self.output)
        
        # If we never opened a paragraph but have content, wrap it
        if result and not '<p>' in result:
            result = f'<p>{result}</p>'
        
        # Add line breaks after each paragraph for readability
        result = result.replace('</p>', '</p>\n')
        
        return result


def simplify_html(html_content):
    """Simplify HTML by removing complex formatting"""
    parser = HTMLSimplifier()
    parser.feed(html_content)
    return parser.get_output()


def process_file(filepath):
    """Process a single HTML file"""
    log_info(f"Processing file: {filepath}")
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        log_debug(f"Read {len(html_content)} characters from {filepath}")
        
        simplified = simplify_html(html_content)
        
        print(simplified)
        log_info(f"Successfully processed {filepath}")
        
    except FileNotFoundError:
        log_error(f"File not found: {filepath}")
        return False
    except Exception as e:
        log_error(f"Error processing {filepath}: {str(e)}")
        return False
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description='HTML Simplifier - Remove complex formatting from HTML while preserving text and basic tags (p, br, b, i, u, em, strong)'
    )
    parser.add_argument('files', help="HTML file(s) to process", nargs='*')
    parser.add_argument("-v", action="store_true", default=False, help="Print extra info")
    parser.add_argument("-vv", action="store_true", default=False, help="Print (more) extra info")
    parser.add_argument("-i", "--input", dest="input_file", help="Read HTML from file instead of arguments")
    parser.add_argument("-s", "--stdin", action="store_true", help="Read HTML from stdin")
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
    # Process input
    ######################################
    
    # Read from stdin
    if args.stdin:
        log_info("Reading from stdin")
        html_content = sys.stdin.read()
        simplified = simplify_html(html_content)
        print(simplified)
        return 0
    
    # Read from specified input file
    if args.input_file:
        if not process_file(args.input_file):
            return 1
        return 0
    
    # Read from files specified as arguments
    if args.files:
        success = True
        for filepath in args.files:
            if not process_file(filepath):
                success = False
        return 0 if success else 1
    
    # No input specified
    log_fatal("No input specified. Use files as arguments, -i/--input, or -s/--stdin", exit_code=1)


##############################################################################
#
# Output and Logging Message Functions
#
##############################################################################
def write_out(message):
    info = {
      'levelname' : 'OUTPUT',
      'asctime'   : time.strftime(DEFAULT_TIME_FORMAT, time.localtime()),
      'message'   : message
    }
    print(LOGGING_FORMAT % info)

def write_status(message):
    """Write status message to stderr (for progress updates independent of logging)"""
    info = {
        'levelname': 'STATUS',
        'asctime': time.strftime(DEFAULT_TIME_FORMAT, time.localtime()),
        'message': message
    }
    print(LOGGING_FORMAT % info, file=sys.stderr, flush=True)

def log_fatal(msg, exit_code=-1):
    logging.critical("Fatal Err: %s\n" % msg)
    sys.exit(exit_code)

def log_warning(msg):
    logging.warning(msg)

def log_error(msg):
    logging.error(msg)

def log_info(msg):
    logging.info(msg)

def log_debug(msg):
    logging.debug(msg)

#
# Initial Setup and call to main()
#
if __name__ == '__main__':
    #sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 1)  # reopen STDOUT unbuffered
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
