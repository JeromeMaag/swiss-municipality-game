"""Seed local development geodata."""

from dataclasses import dataclass

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from geo.models import Canton, GeoDatasetVersion, Municipality


DATASET_NAME = "dev-seed"
DATASET_VERSION = "local"


@dataclass(frozen=True)
class DevMunicipality:
    """Static municipality seed data.

    Attributes:
        bfs_number: Stable development BFS number.
        name: Municipality display name.
        population: Development population value.
        area_km2: Development area value.
        coordinates: Closed WGS84 polygon ring.
    """

    bfs_number: int
    name: str
    population: int
    area_km2: float
    coordinates: tuple[tuple[float, float], ...]


DEV_MUNICIPALITIES = (
    DevMunicipality(
        bfs_number=9001,
        name="Dev Municipality 1",
        population=12000,
        area_km2=12.4,
        coordinates=(
            (7.40, 46.90),
            (7.55, 46.90),
            (7.55, 47.02),
            (7.40, 47.02),
            (7.40, 46.90),
        ),
    ),
    DevMunicipality(
        bfs_number=9002,
        name="Dev Municipality 2",
        population=8700,
        area_km2=9.8,
        coordinates=(
            (7.58, 46.91),
            (7.73, 46.91),
            (7.73, 47.03),
            (7.58, 47.03),
            (7.58, 46.91),
        ),
    ),
    DevMunicipality(
        bfs_number=9003,
        name="Dev Municipality 3",
        population=15300,
        area_km2=15.1,
        coordinates=(
            (7.42, 47.05),
            (7.57, 47.05),
            (7.57, 47.17),
            (7.42, 47.17),
            (7.42, 47.05),
        ),
    ),
    DevMunicipality(
        bfs_number=9004,
        name="Dev Municipality 4",
        population=6400,
        area_km2=7.2,
        coordinates=(
            (7.60, 47.06),
            (7.75, 47.06),
            (7.75, 47.18),
            (7.60, 47.18),
            (7.60, 47.06),
        ),
    ),
    DevMunicipality(
        bfs_number=9005,
        name="Dev Municipality 5",
        population=21100,
        area_km2=18.6,
        coordinates=(
            (7.78, 46.98),
            (7.93, 46.98),
            (7.93, 47.10),
            (7.78, 47.10),
            (7.78, 46.98),
        ),
    ),
)


class Command(BaseCommand):
    """Create a small local geodata dataset for development."""

    help = "Seed five simple active municipalities for local development."

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete the existing dev seed dataset before recreating it.",
        )

    def handle(self, *args, **options) -> None:
        """Create or refresh the development geodata records.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        with transaction.atomic():
            if options["clear"]:
                GeoDatasetVersion.objects.filter(
                    name=DATASET_NAME,
                    version_label=DATASET_VERSION,
                ).delete()

            dataset_version, _ = GeoDatasetVersion.objects.update_or_create(
                name=DATASET_NAME,
                version_label=DATASET_VERSION,
                defaults={
                    "source_url": "",
                    "imported_at": timezone.now(),
                    "notes": "Local dummy geodata for development only.",
                },
            )
            canton_geom = make_multipolygon(
                (
                    (7.35, 46.85),
                    (8.00, 46.85),
                    (8.00, 47.22),
                    (7.35, 47.22),
                    (7.35, 46.85),
                )
            )
            canton, _ = Canton.objects.update_or_create(
                dataset_version=dataset_version,
                abbreviation="DV",
                defaults={
                    "bfs_number": 99,
                    "name": "Dev Canton",
                    "geom": canton_geom,
                    "label_point": canton_geom.point_on_surface,
                },
            )

            for seed in DEV_MUNICIPALITIES:
                geom = make_multipolygon(seed.coordinates)
                Municipality.objects.update_or_create(
                    dataset_version=dataset_version,
                    bfs_number=seed.bfs_number,
                    defaults={
                        "name": seed.name,
                        "canton": canton,
                        "population": seed.population,
                        "area_km2": seed.area_km2,
                        "geom": geom,
                        "label_point": geom.point_on_surface,
                        "is_active": True,
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(DEV_MUNICIPALITIES)} development municipalities."
            )
        )


def make_multipolygon(coordinates: tuple[tuple[float, float], ...]) -> MultiPolygon:
    """Create a WGS84 multipolygon from a closed coordinate ring.

    Args:
        coordinates: Closed polygon coordinates as longitude/latitude pairs.

    Returns:
        A WGS84 multipolygon.
    """
    return MultiPolygon(Polygon(coordinates), srid=4326)
