"""Template tags for geodata admin status."""

from django import template

from geo.admin_views import get_geodata_status


register = template.Library()


@register.simple_tag
def geodata_status() -> dict:
    """Return current geodata status for admin templates.

    Returns:
        Status dictionary for the current geodata dataset.
    """
    return get_geodata_status()
