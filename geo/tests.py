"""Tests for the geo app."""

from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from tests.utils import make_test_geometry

from .models import Canton, GeoDatasetVersion, Municipality


class GeoModelTests(TestCase):
    """Tests for geodata model behavior."""

    def setUp(self) -> None:
        """Create shared geodata fixtures for model tests."""
        self.dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
            source_url="https://example.test/source",
        )
        self.canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
            geom_simplified=make_test_geometry(),
            label_point=Point(8.05, 47.05, srid=4326),
        )

    def test_dataset_version_string(self) -> None:
        """Dataset versions expose a concise display label."""
        self.assertEqual(str(self.dataset_version), "swissBOUNDARIES3D 2026-01-01")

    def test_dataset_version_name_and_label_are_unique(self) -> None:
        """Dataset names and version labels are unique together."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeoDatasetVersion.objects.create(
                name="swissBOUNDARIES3D",
                version_label="2026-01-01",
            )

    def test_canton_string(self) -> None:
        """Cantons expose abbreviation and name in their display label."""
        self.assertEqual(str(self.canton), "ZH - Zurich")

    def test_municipality_string(self) -> None:
        """Municipalities expose name and canton abbreviation."""
        municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            population=443000,
            area_km2=87.88,
            geom=make_test_geometry(),
            geom_simplified=make_test_geometry(),
            label_point=Point(8.05, 47.05, srid=4326),
        )

        self.assertEqual(str(municipality), "Zurich (ZH)")

    def test_municipality_bfs_number_is_unique_per_dataset(self) -> None:
        """Municipality BFS numbers are unique within a dataset version."""
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Municipality.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=261,
                name="Duplicate Zurich",
                canton=self.canton,
                geom=make_test_geometry(),
            )

    def test_canton_abbreviation_is_unique_per_dataset(self) -> None:
        """Canton abbreviations are unique within a dataset version."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            Canton.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=2,
                abbreviation="ZH",
                name="Duplicate Zurich",
                geom=make_test_geometry(),
            )

    def test_canton_abbreviation_can_repeat_across_datasets(self) -> None:
        """Canton abbreviations may repeat across different dataset versions."""
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )

        canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )

        self.assertEqual(canton.abbreviation, "ZH")

    def test_municipality_bfs_number_can_repeat_across_datasets(self) -> None:
        """Municipality BFS numbers may repeat across different dataset versions."""
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )
        other_canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )

        municipality = Municipality.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        self.assertEqual(municipality.bfs_number, 261)

    def test_municipality_requires_canton_from_same_dataset(self) -> None:
        """Municipality validation rejects cantons from another dataset version."""
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )
        other_canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        municipality = Municipality(
            dataset_version=self.dataset_version,
            bfs_number=9999,
            name="Invalid Municipality",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        with self.assertRaises(ValidationError):
            municipality.full_clean()
