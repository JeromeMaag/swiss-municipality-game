"""Import official BFS STATPOP municipality population counts."""

import csv
from decimal import Decimal, InvalidOperation
import io
import json
import re
import urllib.request
from typing import Any
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from geo.management.commands._http import open_url_with_validated_redirects
from geo.management.commands.import_population import (
    format_bfs_numbers,
    import_population_rows,
    resolve_dataset_version,
)
from geo.models import GeoDatasetVersion, Municipality


STATPOP_PXWEB_URL = (
    "https://www.pxweb.bfs.admin.ch/api/v1/en/"
    "px-x-0103030000_102/px-x-0103030000_102.px"
)
DEFAULT_DATASET_NAME = "swissBOUNDARIES3D"
ALLOWED_URL_SCHEMES = {"https"}
ALLOWED_URL_HOSTS = {"www.pxweb.bfs.admin.ch"}

YEAR_VARIABLE = "Jahr"
SPATIAL_VARIABLE = "Kanton (-) / Bezirk (>>) / Gemeinde (......)"
SPATIAL_COLUMN = "Canton (-) / District (>>) / Commune (......)"
POPULATION_TYPE_VARIABLE = "Bev\u00f6lkerungstyp"
DOMICILE_VARIABLE = "Wohnort vor 1 Jahr"
SEX_VARIABLE = "Geschlecht"
AGE_VARIABLE = "Alter"
PERMANENT_POPULATION_VALUE = "1"
TOTAL_VALUE = "-99999"
POPULATION_COLUMN = "Age - total"
MUNICIPALITY_LABEL_PATTERN = re.compile(r"^\.\.\.\.\.\.(\d{4})\s")

# Known BFS municipality mutations needed to align STATPOP 2024 with
# swissBOUNDARIES3D 2026 municipality geometries.
POPULATION_AGGREGATION_BY_TARGET_BFS = {
    1065: (1065, 1057),
    3901: (3901, 3932),
    2097: (2097, 2061, 2066, 2072),
    2102: (2102, 2089),
    2239: (2200, 2217),
    5073: (5073, 5064),
    5079: (5079, 5078),
    5395: (5146, 5149, 5181, 5200, 5207),
    6513: (6453, 6454, 6459, 6461),
    2056: (2016, 2027),
    2262: (2262, 2278),
    2525: (2525, 2520, 2529),
    4095: (4095, 4122),
    6831: (700,),
}


class Command(BaseCommand):
    """Import population counts directly from the BFS STATPOP PX-Web API."""

    help = "Download BFS STATPOP population counts and update municipalities."

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument(
            "--source-url",
            default=STATPOP_PXWEB_URL,
            help="BFS PX-Web STATPOP API table URL.",
        )
        parser.add_argument(
            "--year",
            default="latest",
            help="STATPOP year to import, or 'latest' for the newest available year.",
        )
        parser.add_argument(
            "--dataset-name",
            default=DEFAULT_DATASET_NAME,
            help="Dataset source name used with --dataset-version.",
        )
        parser.add_argument(
            "--dataset-version",
            help="Dataset version label. Defaults to the newest imported dataset.",
        )
        parser.add_argument(
            "--allow-incomplete",
            action="store_true",
            help="Update available values even if some current municipalities are missing.",
        )

    def handle(self, *args, **options) -> None:
        """Run the STATPOP population import.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        source_url = options["source_url"]
        dataset_version = resolve_dataset_version(
            options["dataset_name"],
            options["dataset_version"],
        )
        metadata = fetch_statpop_metadata(source_url)
        year = select_statpop_year(metadata, options["year"])
        statpop_csv = fetch_statpop_csv(source_url, year)
        population_by_bfs = apply_population_aggregation(
            parse_statpop_population_csv(statpop_csv)
        )
        rows, missing_bfs_numbers = population_rows_for_dataset(
            dataset_version,
            population_by_bfs,
        )
        if missing_bfs_numbers and not options["allow_incomplete"]:
            raise CommandError(
                "Missing STATPOP population values for current municipality BFS "
                f"numbers: {format_bfs_numbers(missing_bfs_numbers)}"
            )

        with transaction.atomic():
            updated_count, _ = import_population_rows(
                dataset_version=dataset_version,
                fieldnames=["bfs_number", "population"],
                rows=rows,
                bfs_column="bfs_number",
                population_column="population",
            )

        if missing_bfs_numbers:
            self.stdout.write(
                self.style.WARNING(
                    "Missing STATPOP population values for current municipality "
                    f"BFS numbers: {format_bfs_numbers(missing_bfs_numbers)}"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Updated {updated_count} municipalities for {dataset_version} "
                f"from BFS STATPOP {year}."
            )
        )


def fetch_statpop_metadata(url: str) -> dict[str, Any]:
    """Fetch STATPOP table metadata.

    Args:
        url: BFS PX-Web table URL.

    Returns:
        Parsed table metadata.

    Raises:
        CommandError: If the request fails or returns invalid JSON.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with open_url_with_validated_redirects(
            request,
            timeout=60,
            validate_url=validate_url_scheme,
        ) as response:
            return json.load(response)
    except (OSError, json.JSONDecodeError) as error:
        raise CommandError(f"Could not load BFS STATPOP metadata: {error}") from error


def fetch_statpop_csv(url: str, year: str) -> str:
    """Fetch STATPOP population data as CSV.

    Args:
        url: BFS PX-Web table URL.
        year: STATPOP year.

    Returns:
        CSV response text.

    Raises:
        CommandError: If the request fails.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(build_statpop_query(year)).encode("utf-8"),
        headers={
            "Accept": "text/csv",
            "Content-Type": "application/json",
        },
    )
    try:
        with open_url_with_validated_redirects(
            request,
            timeout=120,
            validate_url=validate_url_scheme,
        ) as response:
            return decode_statpop_response(response.read())
    except OSError as error:
        raise CommandError(
            f"Could not load BFS STATPOP population data: {error}"
        ) from error


def build_statpop_query(year: str) -> dict[str, Any]:
    """Build a PX-Web query for total permanent resident population.

    Args:
        year: STATPOP year.

    Returns:
        PX-Web JSON query.
    """
    return {
        "query": [
            {"code": YEAR_VARIABLE, "selection": {"filter": "item", "values": [year]}},
            {
                "code": SPATIAL_VARIABLE,
                "selection": {"filter": "all", "values": ["*"]},
            },
            {
                "code": POPULATION_TYPE_VARIABLE,
                "selection": {
                    "filter": "item",
                    "values": [PERMANENT_POPULATION_VALUE],
                },
            },
            {
                "code": DOMICILE_VARIABLE,
                "selection": {"filter": "item", "values": [TOTAL_VALUE]},
            },
            {
                "code": SEX_VARIABLE,
                "selection": {"filter": "item", "values": [TOTAL_VALUE]},
            },
            {
                "code": AGE_VARIABLE,
                "selection": {"filter": "item", "values": [TOTAL_VALUE]},
            },
        ],
        "response": {"format": "csv"},
    }


def decode_statpop_response(content: bytes) -> str:
    """Decode a STATPOP CSV response.

    Args:
        content: Raw response bytes.

    Returns:
        Decoded CSV text.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def select_statpop_year(metadata: dict[str, Any], requested_year: str) -> str:
    """Select a STATPOP year from table metadata.

    Args:
        metadata: Parsed PX-Web metadata.
        requested_year: Requested year or 'latest'.

    Returns:
        Selected year.

    Raises:
        CommandError: If the requested year is unavailable.
    """
    year_variable = metadata_variable(metadata, YEAR_VARIABLE)
    values = [str(value) for value in year_variable.get("values", [])]
    if not values:
        raise CommandError("BFS STATPOP metadata does not expose any years.")
    if requested_year == "latest":
        return max(values, key=int)
    if requested_year not in values:
        raise CommandError(f"BFS STATPOP year is not available: {requested_year}")
    return requested_year


def metadata_variable(metadata: dict[str, Any], code: str) -> dict[str, Any]:
    """Return one metadata variable by code.

    Args:
        metadata: Parsed PX-Web metadata.
        code: Variable code to find.

    Returns:
        Matching variable metadata.

    Raises:
        CommandError: If the variable is missing.
    """
    for variable in metadata.get("variables", []):
        if variable.get("code") == code:
            return variable
    raise CommandError(f"BFS STATPOP metadata is missing variable: {code}")


def parse_statpop_population_csv(statpop_csv: str) -> dict[int, int]:
    """Parse municipality population counts from a STATPOP CSV response.

    Args:
        statpop_csv: CSV response text.

    Returns:
        Population values keyed by BFS municipality number.

    Raises:
        CommandError: If the response structure or values are invalid.
    """
    reader = csv.DictReader(io.StringIO(statpop_csv))
    if not reader.fieldnames:
        raise CommandError("BFS STATPOP CSV response has no header row.")
    if (
        SPATIAL_COLUMN not in reader.fieldnames
        or POPULATION_COLUMN not in reader.fieldnames
    ):
        raise CommandError("BFS STATPOP CSV response is missing required columns.")

    population_by_bfs = {}
    for row_number, row in enumerate(reader, start=2):
        bfs_number = parse_municipality_bfs_number(row.get(SPATIAL_COLUMN, ""))
        if bfs_number is None:
            continue
        if bfs_number in population_by_bfs:
            raise CommandError(f"Row {row_number}: duplicate BFS number {bfs_number}.")
        population_by_bfs[bfs_number] = parse_population_value(
            row.get(POPULATION_COLUMN),
            row_number,
        )
    return population_by_bfs


def parse_municipality_bfs_number(spatial_label: str) -> int | None:
    """Parse a BFS number from a PX-Web municipality label.

    Args:
        spatial_label: PX-Web spatial unit label.

    Returns:
        Parsed BFS number, or None for non-municipality rows.
    """
    match = MUNICIPALITY_LABEL_PATTERN.match(str(spatial_label))
    if match is None:
        return None
    return int(match.group(1))


def parse_population_value(value: Any, row_number: int) -> int:
    """Parse one STATPOP population value.

    Args:
        value: Raw CSV cell value.
        row_number: One-based CSV row number for errors.

    Returns:
        Parsed population count.

    Raises:
        CommandError: If the value is missing or not a whole number.
    """
    text = "" if value is None else str(value).strip()
    text = text.replace(" ", "").replace("'", "")
    if not text:
        raise CommandError(f"Row {row_number}: population value is required.")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise CommandError(
            f"Row {row_number}: population value must be a whole number."
        ) from error
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise CommandError(
            f"Row {row_number}: population value must be a whole number."
        )
    return int(number)


def apply_population_aggregation(population_by_bfs: dict[int, int]) -> dict[int, int]:
    """Apply known municipality mutations to STATPOP population values.

    Args:
        population_by_bfs: Raw STATPOP values keyed by BFS number.

    Returns:
        Population values including aggregated successor municipalities.

    Raises:
        CommandError: If a partial mutation source set is present.
    """
    aggregated_population_by_bfs = dict(population_by_bfs)
    for target_bfs, source_bfs_numbers in POPULATION_AGGREGATION_BY_TARGET_BFS.items():
        present_sources = [
            source_bfs
            for source_bfs in source_bfs_numbers
            if source_bfs in population_by_bfs
        ]
        if not present_sources:
            continue
        missing_sources = [
            source_bfs
            for source_bfs in source_bfs_numbers
            if source_bfs not in population_by_bfs
        ]
        if missing_sources:
            if (
                target_bfs in population_by_bfs
                and present_sources == [target_bfs]
            ):
                continue
            raise CommandError(
                "Cannot aggregate STATPOP mutation for BFS "
                f"{target_bfs}; missing source BFS numbers: "
                f"{format_bfs_numbers(missing_sources)}"
            )
        aggregated_population_by_bfs[target_bfs] = sum(
            population_by_bfs[source_bfs] for source_bfs in source_bfs_numbers
        )
    return aggregated_population_by_bfs


def population_rows_for_dataset(
    dataset_version: GeoDatasetVersion,
    population_by_bfs: dict[int, int],
) -> tuple[list[dict[str, str]], list[int]]:
    """Build import rows for municipalities in one dataset version.

    Args:
        dataset_version: Target geodata dataset version.
        population_by_bfs: Population values keyed by BFS number.

    Returns:
        A tuple of import rows and current municipality BFS numbers missing
        from STATPOP data.
    """
    rows = []
    missing_bfs_numbers = []
    current_bfs_numbers = (
        Municipality.objects.filter(dataset_version=dataset_version, is_active=True)
        .values_list("bfs_number", flat=True)
        .order_by("bfs_number")
    )
    for bfs_number in current_bfs_numbers:
        population = population_by_bfs.get(bfs_number)
        if population is None:
            missing_bfs_numbers.append(bfs_number)
            continue
        rows.append(
            {
                "bfs_number": str(bfs_number),
                "population": str(population),
            }
        )
    return rows, missing_bfs_numbers


def validate_url_scheme(url: str) -> None:
    """Validate that a URL uses an allowed remote origin.

    Args:
        url: URL to validate.

    Raises:
        CommandError: If the URL scheme or host is not allowed.
    """
    parsed_url = urlparse(url)
    scheme = parsed_url.scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise CommandError(f"URL scheme '{scheme}' is not allowed. Use https.")
    host = parsed_url.hostname or ""
    if host.lower() not in ALLOWED_URL_HOSTS:
        raise CommandError(f"URL host '{host}' is not allowed.")
