"""Pure classification helpers for yt-dlp downloader failures."""
import re


def classify_error(error_text):
    text = str(error_text or '').strip().casefold()
    if not text:
        return 'other'
    if "isn't available to everyone" in text or 'certain audiences' in text or 'age-restricted' in text:
        return 'audience-restricted'
    if any(s in text for s in ('login required', 'log in to', 'sign in', 'authentication required', 'private video')):
        return 'login-required'
    if 'unsupported url' in text:
        return 'unsupported-photo'
    if re.search(r'http(?: error)?\s*429|too many requests|rate.?limit', text):
        return 'transient-rate-limit'
    if 'there is no video in this post' in text or 'no video in this post' in text:
        return 'unsupported-photo'
    return 'other'
