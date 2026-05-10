"""Set up official boundary and population geodata."""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from geo.management.commands.import_swissboundaries3d import (
    DATASET_NAME,
    STAC_ITEMS_URL,
)
from geo.management.commands.import_statpop_population import STATPOP_PXWEB_URL
from geo.models import GeoDatasetVersion, Municipality


class Command(BaseCommand):
    """Import official boundaries and population values in one command."""

    help = "Download official boundaries and population data for local setup."

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument(
            "--dataset-version",
            default="",
            help="Optional swissBOUNDARIES3D dataset date, for example 2026-01-01.",
        )
        parser.add_argument(
            "--stac-items-url",
            default=STAC_ITEMS_URL,
            help="STAC items endpoint for swissBOUNDARIES3D.",
        )
        parser.add_argument(
            "--statpop-url",
            default=STATPOP_PXWEB_URL,
            help="BFS PX-Web STATPOP API table URL.",
        )
        parser.add_argument(
            "--statpop-year",
            default="latest",
            help="STATPOP year to import, or 'latest' for the newest available year.",
        )
        parser.add_argument(
            "--simplify-tolerance",
            type=float,
            default=0.0002,
            help="Display-geometry simplification tolerance in WGS84 degrees.",
        )
        parser.add_argument(
            "--keep-existing",
            action="store_true",
            help="Do not clear existing boundary records before importing.",
        )
        parser.add_argument(
            "--allow-incomplete-population",
            action="store_true",
            help="Finish setup even if some municipalities have no population value.",
        )

    def handle(self, *args, **options) -> None:
        """Run the complete geodata setup.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        self.stdout.write("Importing official swissBOUNDARIES3D data...")
        call_command(
            "import_swissboundaries3d",
            stac_items_url=options["stac_items_url"],
            dataset_version=options["dataset_version"],
            simplify_tolerance=options["simplify_tolerance"],
            keep_existing=options["keep_existing"],
            stdout=self.stdout,
            stderr=self.stderr,
        )

        dataset_version = resolve_setup_dataset_version(options["dataset_version"])

        self.stdout.write("Importing official BFS STATPOP population data...")
        call_command(
            "import_statpop_population",
            source_url=options["statpop_url"],
            year=options["statpop_year"],
            dataset_name=DATASET_NAME,
            dataset_version=dataset_version.version_label,
            allow_incomplete=options["allow_incomplete_population"],
            stdout=self.stdout,
            stderr=self.stderr,
        )

        municipality_count, missing_population_count = validate_setup_result(
            dataset_version,
            allow_incomplete_population=options["allow_incomplete_population"],
        )
        message = (
            f"Geodata setup complete for {dataset_version}: "
            f"{municipality_count} municipalities"
        )
        if missing_population_count:
            message = f"{message}, {missing_population_count} without population"
        self.stdout.write(self.style.SUCCESS(f"{message}."))


def resolve_setup_dataset_version(requested_version: str) -> GeoDatasetVersion:
    """Resolve the dataset version imported by the setup command.

    Args:
        requested_version: Optional requested swissBOUNDARIES3D version label.

    Returns:
        Imported dataset version.

    Raises:
        CommandError: If the expected dataset version is missing.
    """
    queryset = GeoDatasetVersion.objects.filter(name=DATASET_NAME)
    if requested_version:
        queryset = queryset.filter(version_label=requested_version)
    dataset_version = queryset.order_by("-imported_at", "-id").first()
    if dataset_version is None:
        if requested_version:
            raise CommandError(
                f"Dataset version not found after boundary import: "
                f"{DATASET_NAME} {requested_version}"
            )
        raise CommandError(f"No {DATASET_NAME} dataset version found after import.")
    return dataset_version


def validate_setup_result(
    dataset_version: GeoDatasetVersion,
    allow_incomplete_population: bool,
) -> tuple[int, int]:
    """Validate that setup produced usable geodata.

    Args:
        dataset_version: Imported dataset version.
        allow_incomplete_population: Whether missing population values are allowed.

    Returns:
        Number of municipalities and number without population values.

    Raises:
        CommandError: If boundary data is empty or population values are missing.
    """
    if not dataset_version.cantons.exists():
        raise CommandError(f"{dataset_version} has no cantons.")

    municipalities = Municipality.objects.filter(dataset_version=dataset_version)
    municipality_count = municipalities.count()
    if municipality_count == 0:
        raise CommandError(f"{dataset_version} has no municipalities.")

    missing_population_count = municipalities.filter(population__isnull=True).count()
    if missing_population_count and not allow_incomplete_population:
        raise CommandError(
            f"{dataset_version} has {missing_population_count} municipalities "
            "without population values."
        )
    return municipality_count, missing_population_count
