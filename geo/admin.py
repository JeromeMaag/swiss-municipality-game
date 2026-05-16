"""Admin configuration for the geo app."""

from django.contrib import admin
from django.utils import timezone

from .models import Canton, GeoDatasetVersion, Municipality, Village


def bump_village_dataset_versions(dataset_version_ids) -> None:
    """Refresh village boundary cache versions for affected datasets."""
    ids = {dataset_version_id for dataset_version_id in dataset_version_ids if dataset_version_id}
    if ids:
        GeoDatasetVersion.objects.filter(pk__in=ids).update(
            villages_updated_at=timezone.now(),
        )


@admin.register(GeoDatasetVersion)
class GeoDatasetVersionAdmin(admin.ModelAdmin):
    """Admin configuration for geodata dataset versions."""

    change_list_template = "admin/geo/geodatasetversion/change_list.html"
    list_display = ("name", "version_label", "imported_at", "villages_updated_at")
    search_fields = ("name", "version_label", "source_url")
    readonly_fields = ("imported_at", "villages_updated_at")


@admin.register(Canton)
class CantonAdmin(admin.ModelAdmin):
    """Admin configuration for cantons."""

    list_display = ("abbreviation", "name", "bfs_number", "dataset_version")
    list_filter = ("dataset_version",)
    search_fields = ("abbreviation", "name")
    autocomplete_fields = ("dataset_version",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Municipality)
class MunicipalityAdmin(admin.ModelAdmin):
    """Admin configuration for municipalities."""

    list_display = (
        "name",
        "bfs_number",
        "canton",
        "dataset_version",
        "population",
        "is_active",
    )
    list_filter = ("dataset_version", "canton", "is_active")
    search_fields = ("name", "canton__name", "canton__abbreviation")
    autocomplete_fields = ("dataset_version", "canton")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Village)
class VillageAdmin(admin.ModelAdmin):
    """Admin configuration for villages."""

    list_display = (
        "name",
        "postal_code",
        "canton",
        "municipality",
        "dataset_version",
        "is_active",
    )
    list_filter = ("dataset_version", "canton", "is_active")
    search_fields = (
        "name",
        "postal_code",
        "source_identifier",
        "canton__name",
        "canton__abbreviation",
        "municipality__name",
    )
    autocomplete_fields = ("dataset_version", "canton", "municipality")
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        """Return villages with related owner geography for the changelist."""
        return (
            super()
            .get_queryset(request)
            .select_related("dataset_version", "canton", "municipality")
        )

    def save_model(self, request, obj, form, change) -> None:
        """Save a village and refresh the affected boundary cache version."""
        previous_dataset_version_id = None
        if change and obj.pk:
            previous_dataset_version_id = (
                Village.objects.filter(pk=obj.pk)
                .values_list("dataset_version_id", flat=True)
                .first()
            )
        super().save_model(request, obj, form, change)
        bump_village_dataset_versions(
            {previous_dataset_version_id, obj.dataset_version_id}
        )

    def delete_model(self, request, obj) -> None:
        """Delete a village and refresh the affected boundary cache version."""
        dataset_version_id = obj.dataset_version_id
        super().delete_model(request, obj)
        bump_village_dataset_versions({dataset_version_id})

    def delete_queryset(self, request, queryset) -> None:
        """Delete villages and refresh affected boundary cache versions."""
        dataset_version_ids = set(
            queryset.values_list("dataset_version_id", flat=True).distinct()
        )
        super().delete_queryset(request, queryset)
        bump_village_dataset_versions(dataset_version_ids)
