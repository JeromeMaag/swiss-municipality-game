"""Import municipality population counts from CSV files."""

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from geo.models import GeoDatasetVersion, Municipality
from geo.selectors import get_current_dataset_version


class Command(BaseCommand):
    """Import population values for municipalities in one dataset version."""

    help = "Import municipality population counts from a CSV file."

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument("source", help="Path to the population CSV file.")
        parser.add_argument(
            "--dataset-name",
            default="swissBOUNDARIES3D",
            help="Dataset source name used with --dataset-version.",
        )
        parser.add_argument(
            "--dataset-version",
            help="Dataset version label. Defaults to the newest imported dataset.",
        )
        parser.add_argument(
            "--bfs-column",
            default="bfs_number",
            help="CSV column containing the municipality BFS number.",
        )
        parser.add_argument(
            "--population-column",
            default="population",
            help="CSV column containing the population count.",
        )
        parser.add_argument(
            "--delimiter",
            default=",",
            help="CSV delimiter character.",
        )

    def handle(self, *args, **options) -> None:
        """Run the population import.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        source = resolve_csv_path(options["source"])
        dataset_version = resolve_dataset_version(
            options["dataset_name"],
            options["dataset_version"],
        )
        fieldnames, rows = read_csv_rows(source, options["delimiter"])

        with transaction.atomic():
            updated_count, missing_bfs_numbers = import_population_rows(
                dataset_version=dataset_version,
                fieldnames=fieldnames,
                rows=rows,
                bfs_column=options["bfs_column"],
                population_column=options["population_column"],
            )

        if missing_bfs_numbers:
            self.stdout.write(
                self.style.WARNING(
                    "Missing municipalities for BFS numbers: "
                    f"{format_bfs_numbers(missing_bfs_numbers)}"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Updated {updated_count} municipalities for {dataset_version}."
            )
        )


def resolve_csv_path(source: str) -> Path:
    """Resolve and validate a CSV source path.

    Args:
        source: User-provided CSV path.

    Returns:
        The resolved CSV path.

    Raises:
        CommandError: If the path does not point to a file.
    """
    path = Path(source)
    if not path.exists():
        raise CommandError(f"Population CSV does not exist: {source}")
    if not path.is_file():
        raise CommandError(f"Population CSV is not a file: {source}")
    return path


def resolve_dataset_version(
    dataset_name: str,
    version_label: str | None,
) -> GeoDatasetVersion:
    """Resolve the target dataset version for population updates.

    Args:
        dataset_name: Dataset source name.
        version_label: Optional dataset version label.

    Returns:
        The target dataset version.

    Raises:
        CommandError: If no matching dataset version exists.
    """
    if version_label:
        try:
            return GeoDatasetVersion.objects.get(
                name=dataset_name,
                version_label=version_label,
            )
        except GeoDatasetVersion.DoesNotExist as error:
            raise CommandError(
                f"Dataset version not found: {dataset_name} {version_label}"
            ) from error

    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        raise CommandError(
            "No dataset version found. Import boundaries first or pass "
            "--dataset-version."
        )
    return dataset_version


def read_csv_rows(path: Path, delimiter: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Read CSV rows with a header.

    Args:
        path: CSV file path.
        delimiter: CSV delimiter character.

    Returns:
        A tuple of field names and row dictionaries.

    Raises:
        CommandError: If the delimiter is invalid or the file has no header.
    """
    if len(delimiter) != 1:
        raise CommandError("--delimiter must be exactly one character.")

    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file, delimiter=delimiter)
        if not reader.fieldnames:
            raise CommandError("Population CSV requires a header row.")
        validate_unique_columns(reader.fieldnames)
        return reader.fieldnames, list(reader)


def import_population_rows(
    dataset_version: GeoDatasetVersion,
    fieldnames: list[str],
    rows: list[dict[str, Any]],
    bfs_column: str,
    population_column: str,
) -> tuple[int, list[int]]:
    """Import population values from parsed CSV rows.

    Args:
        dataset_version: Dataset version whose municipalities are updated.
        fieldnames: CSV field names.
        rows: CSV rows.
        bfs_column: Column containing municipality BFS numbers.
        population_column: Column containing population counts.

    Returns:
        The number of updated municipalities and missing BFS numbers.

    Raises:
        CommandError: If required columns or values are invalid.
    """
    validate_distinct_columns([bfs_column, population_column])
    validate_required_columns(fieldnames, [bfs_column, population_column])
    municipalities_by_bfs = {
        municipality.bfs_number: municipality
        for municipality in Municipality.objects.filter(
            dataset_version=dataset_version
        )
    }
    seen_bfs_numbers = set()
    updated_municipalities = []
    missing_bfs_numbers = []
    updated_at = timezone.now()

    for index, row in enumerate(rows, start=2):
        bfs_number = parse_whole_number(
            row.get(bfs_column),
            field_name=bfs_column,
            row_number=index,
            allow_empty=False,
        )
        if bfs_number <= 0:
            raise CommandError(f"Row {index}: {bfs_column} must be positive.")
        population = parse_whole_number(
            row.get(population_column),
            field_name=population_column,
            row_number=index,
            allow_empty=True,
        )
        if population is not None and population < 0:
            raise CommandError(f"Row {index}: population must not be negative.")
        if bfs_number in seen_bfs_numbers:
            raise CommandError(f"Row {index}: duplicate BFS number {bfs_number}.")
        seen_bfs_numbers.add(bfs_number)

        municipality = municipalities_by_bfs.get(bfs_number)
        if municipality is None:
            missing_bfs_numbers.append(bfs_number)
            continue

        municipality.population = population
        municipality.updated_at = updated_at
        updated_municipalities.append(municipality)

    if updated_municipalities:
        Municipality.objects.bulk_update(
            updated_municipalities,
            ["population", "updated_at"],
            batch_size=500,
        )
    return len(updated_municipalities), missing_bfs_numbers


def validate_unique_columns(fieldnames: list[str]) -> None:
    """Validate that CSV field names are unique.

    Args:
        fieldnames: CSV field names.

    Raises:
        CommandError: If one or more field names are duplicated.
    """
    seen_columns = set()
    duplicate_columns = []
    for fieldname in fieldnames:
        if fieldname in seen_columns and fieldname not in duplicate_columns:
            duplicate_columns.append(fieldname)
        seen_columns.add(fieldname)
    if duplicate_columns:
        raise CommandError(
            "Population CSV contains duplicate column(s): "
            f"{', '.join(duplicate_columns)}"
        )


def validate_distinct_columns(columns: list[str]) -> None:
    """Validate that configured semantic columns are distinct.

    Args:
        columns: Configured column names.

    Raises:
        CommandError: If a column name is reused for multiple meanings.
    """
    if len(set(columns)) != len(columns):
        raise CommandError("BFS and population columns must be distinct.")


def validate_required_columns(fieldnames: list[str], required_columns: list[str]) -> None:
    """Validate that all required CSV columns exist.

    Args:
        fieldnames: CSV field names.
        required_columns: Required column names.

    Raises:
        CommandError: If one or more required columns are missing.
    """
    missing_columns = [
        column for column in required_columns if column not in fieldnames
    ]
    if missing_columns:
        raise CommandError(
            "Population CSV is missing required column(s): "
            f"{', '.join(missing_columns)}"
        )


def parse_whole_number(
    value: Any,
    field_name: str,
    row_number: int,
    allow_empty: bool,
) -> int | None:
    """Parse a CSV value as a whole number.

    Args:
        value: Raw CSV value.
        field_name: CSV field name for error messages.
        row_number: One-based CSV row number for error messages.
        allow_empty: Whether empty values should be returned as None.

    Returns:
        Parsed integer, or None for allowed empty values.

    Raises:
        CommandError: If the value is empty when required or not a whole number.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        if allow_empty:
            return None
        raise CommandError(f"Row {row_number}: {field_name} is required.")

    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise CommandError(
            f"Row {row_number}: {field_name} must be a whole number."
        ) from error

    if not number.is_finite() or number != number.to_integral_value():
        raise CommandError(f"Row {row_number}: {field_name} must be a whole number.")
    return int(number)


def format_bfs_numbers(bfs_numbers: list[int]) -> str:
    """Format missing BFS numbers for command output.

    Args:
        bfs_numbers: Missing BFS numbers.

    Returns:
        A compact display string.
    """
    sorted_numbers = sorted(bfs_numbers)
    if len(sorted_numbers) <= 20:
        return ", ".join(str(number) for number in sorted_numbers)
    displayed_numbers = ", ".join(str(number) for number in sorted_numbers[:20])
    remaining_count = len(sorted_numbers) - 20
    return f"{displayed_numbers}, ... ({remaining_count} more)"
