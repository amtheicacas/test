from datetime import timedelta

from django import template

register = template.Library()


@register.filter
def add_days(d, n):
    """{{ deadline|add_days:10 }} -> the deadline plus 10 days."""
    try:
        return d + timedelta(days=int(n))
    except (TypeError, ValueError):
        return d


@register.filter
def ordinal_suffix(n):
    """1 -> 'st', 2 -> 'nd', 3 -> 'rd', 4 -> 'th', 11 -> 'th', 21 -> 'st'"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
