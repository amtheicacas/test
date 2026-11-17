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
