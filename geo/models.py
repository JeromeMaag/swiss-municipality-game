"""Database models for Swiss geodata."""

from django.core.exceptions import ValidationError
from django.contrib.gis.db import models
from django.db.models import Q


class GeoDatasetVersion(models.Model):
    """Imported geodata dataset version.

    A dataset version represents one municipal boundary state from a source such
    as swissBOUNDARIES3D.
    """

    name = models.CharField(max_length=120)
    version_label = models.CharField(max_length=80)
    source_url = models.URLField(blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        """Model metadata for dataset versions."""

        ordering = ["-imported_at", "name", "version_label"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "version_label"],
                name="unique_geo_dataset_name_version",
            ),
        ]

    def __str__(self) -> str:
        """Return the dataset display label.

        Returns:
            A human-readable dataset name and version.
        """
        return f"{self.name} {self.version_label}"


class Canton(models.Model):
    """Swiss canton boundary for a specific dataset version."""

    dataset_version = models.ForeignKey(
        GeoDatasetVersion,
        on_delete=models.CASCADE,
        related_name="cantons",
    )
    bfs_number = models.IntegerField(blank=True, null=True, db_index=True)
    abbreviation = models.CharField(max_length=2)
    name = models.CharField(max_length=120, db_index=True)
    geom = models.MultiPolygonField(srid=4326)
    geom_simplified = models.MultiPolygonField(srid=4326, blank=True, null=True)
    label_point = models.PointField(srid=4326, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata for cantons."""

        ordering = ["abbreviation"]
        indexes = [
            models.Index(fields=["dataset_version", "name"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dataset_version", "abbreviation"],
                name="unique_canton_dataset_abbreviation",
            ),
        ]

    def __str__(self) -> str:
        """Return the canton display label.

        Returns:
            A human-readable canton abbreviation and name.
        """
        return f"{self.abbreviation} - {self.name}"


class Municipality(models.Model):
    """Swiss municipality boundary for a specific dataset version."""

    dataset_version = models.ForeignKey(
        GeoDatasetVersion,
        on_delete=models.CASCADE,
        related_name="municipalities",
    )
    bfs_number = models.IntegerField(db_index=True)
    name = models.CharField(max_length=120, db_index=True)
    canton = models.ForeignKey(
        Canton,
        on_delete=models.PROTECT,
        related_name="municipalities",
    )
    population = models.IntegerField(blank=True, null=True)
    area_km2 = models.FloatField(blank=True, null=True)
    geom = models.MultiPolygonField(srid=4326)
    geom_simplified = models.MultiPolygonField(srid=4326, blank=True, null=True)
    label_point = models.PointField(srid=4326, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateField(blank=True, null=True)
    valid_to = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata for municipalities."""

        ordering = ["name"]
        indexes = [
            models.Index(fields=["dataset_version", "name"]),
            models.Index(fields=["canton", "name"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dataset_version", "bfs_number"],
                name="unique_municipality_dataset_bfs_number",
            ),
        ]
        verbose_name_plural = "municipalities"

    def __str__(self) -> str:
        """Return the municipality display label.

        Returns:
            A human-readable municipality name with its canton abbreviation.
        """
        return f"{self.name} ({self.canton.abbreviation})"

    def clean(self) -> None:
        """Validate municipality consistency.

        Raises:
            ValidationError: If the municipality and canton belong to different
                dataset versions.
        """
        super().clean()
        if self.canton_id and self.dataset_version_id != self.canton.dataset_version_id:
            raise ValidationError(
                {
                    "canton": (
                        "Municipality and canton must belong to the same dataset "
                        "version."
                    )
                }
            )


class Village(models.Model):
    """Swiss village/locality boundary for a specific dataset version."""

    dataset_version = models.ForeignKey(
        GeoDatasetVersion,
        on_delete=models.CASCADE,
        related_name="villages",
    )
    source_identifier = models.CharField(max_length=120, blank=True, db_index=True)
    name = models.CharField(max_length=160, db_index=True)
    postal_code = models.CharField(max_length=10, blank=True, db_index=True)
    canton = models.ForeignKey(
        Canton,
        on_delete=models.PROTECT,
        related_name="villages",
    )
    municipality = models.ForeignKey(
        Municipality,
        on_delete=models.PROTECT,
        related_name="villages",
        blank=True,
        null=True,
    )
    geom = models.MultiPolygonField(srid=4326)
    geom_simplified = models.MultiPolygonField(srid=4326, blank=True, null=True)
    label_point = models.PointField(srid=4326, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateField(blank=True, null=True)
    valid_to = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model metadata for villages."""

        ordering = ["name", "postal_code"]
        indexes = [
            models.Index(fields=["dataset_version", "name"]),
            models.Index(fields=["dataset_version", "postal_code"]),
            models.Index(fields=["canton", "name"]),
            models.Index(fields=["municipality", "name"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dataset_version", "source_identifier"],
                condition=~Q(source_identifier=""),
                name="unique_village_dataset_source_identifier",
            ),
        ]

    def __str__(self) -> str:
        """Return the village display label.

        Returns:
            A human-readable village name, optional postal code, and canton.
        """
        if self.postal_code:
            return f"{self.name} {self.postal_code} ({self.canton.abbreviation})"
        return f"{self.name} ({self.canton.abbreviation})"

    def clean(self) -> None:
        """Validate village consistency.

        Raises:
            ValidationError: If the village, canton, or municipality belong to
                incompatible dataset versions or cantons.
        """
        super().clean()
        errors: dict[str, list[str]] = {}

        if self.canton_id and self.dataset_version_id != self.canton.dataset_version_id:
            errors.setdefault("canton", []).append(
                "Village and canton must belong to the same dataset version."
            )

        if self.municipality_id:
            if self.dataset_version_id != self.municipality.dataset_version_id:
                errors.setdefault("municipality", []).append(
                    "Village and municipality must belong to the same dataset "
                    "version."
                )
            if self.canton_id and self.canton_id != self.municipality.canton_id:
                errors.setdefault("municipality", []).append(
                    "Village and municipality must belong to the same canton."
                )

        if errors:
            raise ValidationError(errors)
