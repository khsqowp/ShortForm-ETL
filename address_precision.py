"""Conservative address granularity check; identifies street/building numbers."""
import re

def has_venue_precision(address):
    text = str(address or '').strip()
    patterns = (r'(?:로|길)\s*\d+(?:-\d+)?', r'(?:동|리|가)\s*\d+-\d+', r'산\s*\d+-\d+', r'\d+\s*(?:층|호)\b', r'\bB\d+\b|\b\d+F\b')
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

def address_precision(address):
    return 'venue' if has_venue_precision(address) else 'area-only'
