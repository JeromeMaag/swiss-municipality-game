"""Custom admin views for geodata maintenance."""

from io import StringIO

from django.contrib import admin, messages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from geo.models import Municipality
from geo.selectors import get_current_dataset_version


GEODATA_ADMIN_ACTIONS = {
    "seed_dev_geodata": {
        "command": "seed_dev_geodata",
        "label": "Development geodata",
    },
    "setup_geodata": {
        "command": "setup_geodata",
        "label": "Official geodata",
        "options": {"keep_existing": True},
    },
}


@require_http_methods(["GET", "POST"])
def geodata_setup(request):
    """Render and process the geodata maintenance admin page.

    Args:
        request: The incoming admin request.

    Returns:
        A rendered admin page or redirect after a submitted action.
    """
    if request.method == "POST":
        run_geodata_admin_action(request)
        return redirect("admin_geodata_setup")

    context = {
        **admin.site.each_context(request),
        "title": "Geodata setup",
        "status": get_geodata_status(),
    }
    return render(request, "admin/geo/geodata_setup.html", context)


def run_geodata_admin_action(request) -> None:
    """Run one allowlisted geodata admin action and report the result.

    Args:
        request: Admin POST request containing an action name.
    """
    action = request.POST.get("action", "")
    action_config = GEODATA_ADMIN_ACTIONS.get(action)
    if action_config is None:
        messages.error(request, "Unknown geodata action.")
        return

    command_output = StringIO()
    try:
        call_command(
            action_config["command"],
            **action_config.get("options", {}),
            stdout=command_output,
            stderr=command_output,
        )
    except CommandError as error:
        messages.error(request, f"{action_config['label']} update failed: {error}")
        return

    messages.success(request, f"{action_config['label']} update completed.")
    output = command_output.getvalue().strip()
    if output:
        messages.info(request, truncate_command_output(output))


def truncate_command_output(output: str, limit: int = 2000) -> str:
    """Return command output short enough for a Django admin message.

    Args:
        output: Raw management command output.
        limit: Maximum number of characters to return.

    Returns:
        Full or shortened command output.
    """
    if len(output) <= limit:
        return output
    if limit <= 3:
        return output[-limit:]
    return f"...{output[-(limit - 3):]}"


def get_geodata_status() -> dict:
    """Return summary information for the current geodata dataset.

    Returns:
        Status dictionary used by the admin template.
    """
    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        return {
            "dataset_version": None,
            "canton_count": 0,
            "municipality_count": 0,
            "active_municipality_count": 0,
            "inactive_municipality_count": 0,
            "missing_population_count": 0,
        }

    municipality_counts = Municipality.objects.filter(
        dataset_version=dataset_version
    ).aggregate(
        municipality_count=Count("id"),
        active_municipality_count=Count("id", filter=Q(is_active=True)),
        inactive_municipality_count=Count("id", filter=Q(is_active=False)),
        missing_population_count=Count(
            "id",
            filter=Q(is_active=True, population__isnull=True),
        ),
    )
    return {
        "dataset_version": dataset_version,
        "canton_count": dataset_version.cantons.count(),
        **municipality_counts,
    }
