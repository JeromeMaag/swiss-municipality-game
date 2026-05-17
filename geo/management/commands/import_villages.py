"""Import official village/locality boundaries."""

from dataclasses import dataclass
from datetime import date, datetime
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import urllib.request
from urllib.parse import urlparse
import zipfile

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from geo.management.commands._http import open_url_with_validated_redirects
from geo.management.commands.import_boundaries import (
    VECTOR_SUFFIXES,
    row_value,
    shapely_to_multipolygon,
    simplify_geometry,
    to_int,
    to_str,
)
from geo.models import Canton, GeoDatasetVersion, Municipality, Village
from geo.selectors import get_current_dataset_version


OFFICIAL_VILLAGE_SHAPE_URL = (
    "https://data.geo.admin.ch/ch.swisstopo-vd.ortschaftenverzeichnis_plz/"
    "ortschaftenverzeichnis_plz/ortschaftenverzeichnis_plz_2056.shp.zip"
)
OFFICIAL_VILLAGE_LAYER = "AMTOVZ_LOCALITY"
ALLOWED_URL_SCHEMES = {"https"}
ALLOWED_URL_HOSTS = {"data.geo.admin.ch"}
DIRECTORY_VECTOR_SUFFIXES = {".gdb"}
UNIX_FILE_TYPE_MASK = 0o170000
UNIX_SYMLINK_TYPE = 0o120000


@dataclass(frozen=True)
class VillageImportResult:
    """Village import result counts."""

    villages: list[Village]
    skipped_without_canton: int


class Command(BaseCommand):
    """Import village/locality boundaries into the current geodata version."""

    help = (
        "Import official village/locality boundaries. Without a source path, "
        "the command downloads the official swisstopo locality shapefile."
    )

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument(
            "source",
            nargs="?",
            default="",
            help="Optional vector file, datasource folder, or ZIP archive.",
        )
        parser.add_argument(
            "--dataset-version",
            default="",
            help=(
                "Existing GeoDatasetVersion.version_label to attach villages to. "
                "Defaults to the current geodata version."
            ),
        )
        parser.add_argument(
            "--source-url",
            default="",
            help=(
                "Optional source URL. When no source path is provided, this URL "
                "is downloaded instead of the default official swisstopo archive."
            ),
        )
        parser.add_argument(
            "--layer",
            default=OFFICIAL_VILLAGE_LAYER,
            help="Vector layer name to import.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing villages for the selected dataset version first.",
        )
        parser.add_argument(
            "--simplify-tolerance",
            type=float,
            default=0.0002,
            help="Display-geometry simplification tolerance in WGS84 degrees.",
        )
        parser.add_argument(
            "--source-identifier-field",
            default="LOCALITYID",
            help="Stable village source identifier column.",
        )
        parser.add_argument(
            "--name-field",
            default="NAME",
            help="Village name column.",
        )
        parser.add_argument(
            "--postal-code-field",
            default="",
            help="Optional village postal code column.",
        )
        parser.add_argument(
            "--status-field",
            default="STATUS",
            help="Optional status column used to set is_active.",
        )
        parser.add_argument(
            "--active-status",
            default="REAL",
            help="Status value that marks a village as active.",
        )
        parser.add_argument(
            "--valid-from-field",
            default="VALIDITY",
            help="Optional validity start date column.",
        )
        parser.add_argument(
            "--valid-to-field",
            default="",
            help="Optional validity end date column.",
        )
        parser.add_argument(
            "--canton-abbreviation-field",
            default="",
            help="Optional canton abbreviation column.",
        )
        parser.add_argument(
            "--canton-bfs-field",
            default="",
            help="Optional canton BFS number column.",
        )
        parser.add_argument(
            "--skip-municipality-assignment",
            action="store_true",
            help="Do not assign villages to municipalities by label point.",
        )

    def handle(self, *args, **options) -> None:
        """Run the village import.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        dataset_version = resolve_dataset_version(options["dataset_version"])
        source_url = options["source_url"] or OFFICIAL_VILLAGE_SHAPE_URL

        with TemporaryDirectory(prefix="villages_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = resolve_or_download_source(options["source"], source_url, tmp_path)
            layer_source = resolve_layer_source(source, options["layer"], tmp_path)
            village_gdf = read_village_layer(layer_source, options["layer"])

            with transaction.atomic():
                if options["clear"]:
                    try:
                        Village.objects.filter(dataset_version=dataset_version).delete()
                    except ProtectedError as error:
                        raise CommandError(
                            "Existing games reference villages in this dataset. "
                            "Import a newer dataset version instead."
                        ) from error

                result = import_villages(
                    village_gdf,
                    dataset_version,
                    options,
                )

        skipped = ""
        if result.skipped_without_canton:
            skipped = f" Skipped {result.skipped_without_canton} rows without Swiss canton."
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(result.villages)} villages for {dataset_version}."
                f"{skipped}"
            )
        )


def resolve_dataset_version(requested_version: str) -> GeoDatasetVersion:
    """Resolve the geodata version villages should be attached to.

    Args:
        requested_version: Optional explicit dataset version label.

    Returns:
        Existing dataset version.

    Raises:
        CommandError: If the requested or current dataset version is unavailable.
    """
    current_dataset_version = get_current_dataset_version()
    if current_dataset_version is None:
        raise CommandError("Import canton and municipality boundaries first.")

    boundary_versions = GeoDatasetVersion.objects.filter(
        name=current_dataset_version.name,
        cantons__isnull=False,
        municipalities__isnull=False,
    ).distinct()
    if requested_version:
        dataset_version = (
            boundary_versions.filter(version_label=requested_version)
            .order_by("-imported_at", "name")
            .first()
        )
        if dataset_version is None:
            raise CommandError(
                f"No geodata dataset version found for {requested_version}."
            )
        return dataset_version

    dataset_version = current_dataset_version
    if not boundary_versions.filter(pk=dataset_version.pk).exists():
        dataset_version = boundary_versions.order_by("-imported_at", "-id").first()
    if dataset_version is None:
        raise CommandError("Import canton and municipality boundaries first.")
    return dataset_version


def resolve_or_download_source(source: str, source_url: str, tmp_path: Path) -> Path:
    """Resolve a source path or download the official village archive.

    Args:
        source: Optional user-provided source path.
        source_url: Official or user-described source URL.
        tmp_path: Temporary working directory.

    Returns:
        Path to a local vector datasource or ZIP archive.
    """
    if source:
        path = Path(source)
        if not path.exists():
            raise CommandError(f"Datasource does not exist: {source}")
        return path

    archive_path = tmp_path / "official_villages.shp.zip"
    download_asset(source_url, archive_path)
    return archive_path


def download_asset(url: str, destination: Path) -> None:
    """Download a village datasource archive.

    Args:
        url: Asset URL.
        destination: Destination file path.
    """
    request = urllib.request.Request(url)
    try:
        with open_url_with_validated_redirects(
            request,
            timeout=300,
            validate_url=validate_url_scheme,
        ) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    except OSError as error:
        raise CommandError(f"Could not download village datasource: {error}") from error


def validate_url_scheme(url: str) -> None:
    """Validate that a village download URL uses an allowed origin.

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


def resolve_layer_source(source: Path, layer: str, tmp_path: Path) -> Path:
    """Resolve the concrete vector file for a layer.

    Args:
        source: Source file, ZIP archive, or datasource folder.
        layer: Layer name to prefer.
        tmp_path: Temporary working directory.

    Returns:
        Concrete vector datasource path.

    Raises:
        CommandError: If no matching datasource can be found.
    """
    if source.is_file() and source.suffix.lower() == ".zip":
        extract_dir = tmp_path / "extract"
        extract_zip(source, extract_dir)
        return resolve_layer_source(extract_dir, layer, tmp_path)

    if source.is_file():
        if source.suffix.lower() not in VECTOR_SUFFIXES:
            raise CommandError(f"Unsupported village datasource: {source}")
        return source

    if is_supported_vector_directory(source):
        return source

    if not source.is_dir():
        raise CommandError(f"Datasource is not a file or folder: {source}")

    layer_lower = layer.lower()
    matching = [
        candidate
        for candidate in sorted(source.rglob("*"))
        if is_supported_vector_source(candidate)
        and vector_source_name(candidate) == layer_lower
    ]
    if matching:
        return matching[0]

    candidates = [
        candidate
        for candidate in sorted(source.rglob("*"))
        if is_supported_vector_source(candidate)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise CommandError(f"No supported village datasource found in: {source}")

    candidate_list = "\n".join(str(candidate) for candidate in candidates[:10])
    raise CommandError(
        f"Multiple vector datasources found. Could not find layer {layer}:\n"
        f"{candidate_list}"
    )


def is_supported_vector_source(path: Path) -> bool:
    """Return whether a file or folder is a supported vector datasource."""
    return (
        path.is_file()
        and path.suffix.lower() in VECTOR_SUFFIXES
    ) or is_supported_vector_directory(path)


def is_supported_vector_directory(path: Path) -> bool:
    """Return whether a folder is a supported directory-backed datasource."""
    return path.is_dir() and path.suffix.lower() in DIRECTORY_VECTOR_SUFFIXES


def vector_source_name(path: Path) -> str:
    """Return a layer-comparison name for a vector datasource path."""
    return path.stem.lower()


def extract_zip(archive_path: Path, destination: Path) -> None:
    """Extract a ZIP archive without allowing path traversal.

    Args:
        archive_path: ZIP archive path.
        destination: Extraction directory.
    """
    try:
        with zipfile.ZipFile(archive_path) as archive:
            safe_extract_zip(archive, destination)
    except zipfile.BadZipFile as error:
        raise CommandError("Village datasource ZIP is not valid.") from error


def safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Safely extract a ZIP archive.

    Args:
        archive: Open ZIP archive.
        destination: Extraction directory.

    Raises:
        CommandError: If a ZIP member would be unsafe to extract.
    """
    resolved_destination = destination.resolve()
    for member in archive.infolist():
        target = safe_zip_member_path(resolved_destination, member.filename)
        if is_zip_member_symlink(member):
            raise CommandError("Unsafe symlink found in village datasource ZIP.")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def safe_zip_member_path(destination: Path, filename: str) -> Path:
    """Resolve a ZIP member path inside an extraction destination.

    Args:
        destination: Resolved extraction destination.
        filename: ZIP member filename.

    Returns:
        Resolved target path.

    Raises:
        CommandError: If the member path would leave the destination.
    """
    target = (destination / filename).resolve()
    try:
        target.relative_to(destination)
    except ValueError as error:
        raise CommandError("Unsafe path found in village datasource ZIP.") from error
    return target


def is_zip_member_symlink(member: zipfile.ZipInfo) -> bool:
    """Return whether a ZIP member is a Unix symlink entry.

    Args:
        member: ZIP member metadata.

    Returns:
        True when the member declares a symlink file type.
    """
    file_type = (member.external_attr >> 16) & UNIX_FILE_TYPE_MASK
    return file_type == UNIX_SYMLINK_TYPE


def read_village_layer(source: Path, layer: str):
    """Read a village layer as WGS84 GeoDataFrame.

    Args:
        source: Vector datasource path.
        layer: Layer name for multi-layer datasources.

    Returns:
        A GeoDataFrame in EPSG:4326.

    Raises:
        CommandError: If the layer has no CRS.
    """
    import geopandas as gpd

    if source.suffix.lower() == ".shp":
        gdf = gpd.read_file(source)
    else:
        gdf = gpd.read_file(source, layer=layer)
    if gdf.crs is None:
        raise CommandError(f"Layer {layer} has no CRS.")
    return gdf.to_crs(epsg=4326)


def import_villages(
    gdf,
    dataset_version: GeoDatasetVersion,
    options: dict[str, Any],
) -> VillageImportResult:
    """Import village rows.

    Args:
        gdf: Village GeoDataFrame.
        dataset_version: Dataset version for imported rows.
        options: Parsed command options.

    Returns:
        Imported villages and skipped row counts.
    """
    cantons = list(Canton.objects.filter(dataset_version=dataset_version))
    canton_by_abbreviation = {canton.abbreviation: canton for canton in cantons}
    canton_by_bfs = {
        canton.bfs_number: canton
        for canton in cantons
        if canton.bfs_number is not None
    }

    villages = []
    skipped_without_canton = 0
    for _, row in gdf.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        geom = shapely_to_multipolygon(geometry)
        simplified_geom = simplify_geometry(geom, options["simplify_tolerance"])
        source_identifier = to_str(
            row_value(row, options["source_identifier_field"])
        )
        name = to_str(row_value(row, options["name_field"]))
        if not source_identifier or not name:
            raise CommandError("Village rows require source identifier and name values.")

        canton = find_canton(
            row,
            geom,
            cantons,
            options,
            canton_by_abbreviation,
            canton_by_bfs,
        )
        if canton is None:
            skipped_without_canton += 1
            continue

        status = to_str(row_value(row, options["status_field"]))
        is_active = not status or status == options["active_status"]

        village, _ = Village.objects.update_or_create(
            dataset_version=dataset_version,
            source_identifier=source_identifier,
            defaults={
                "name": name,
                "postal_code": to_str(row_value(row, options["postal_code_field"])),
                "canton": canton,
                "municipality": None,
                "geom": geom,
                "geom_simplified": simplified_geom,
                "label_point": geom.point_on_surface,
                "is_active": is_active,
                "valid_from": to_date(row_value(row, options["valid_from_field"])),
                "valid_to": to_date(row_value(row, options["valid_to_field"])),
            },
        )
        villages.append(village)

    if not options["skip_municipality_assignment"]:
        assign_village_municipalities(villages, dataset_version)

    village_update_time = timezone.now()
    GeoDatasetVersion.objects.filter(pk=dataset_version.pk).update(
        villages_updated_at=village_update_time,
    )
    dataset_version.villages_updated_at = village_update_time

    return VillageImportResult(
        villages=villages,
        skipped_without_canton=skipped_without_canton,
    )


def find_canton(
    row,
    geom,
    cantons: list[Canton],
    options: dict[str, Any],
    by_abbreviation: dict[str, Canton],
    by_bfs: dict[int, Canton],
) -> Canton | None:
    """Find the canton for a village row.

    Args:
        row: Village row.
        geom: Village geometry.
        cantons: Dataset cantons.
        options: Parsed command options.
        by_abbreviation: Canton lookup by abbreviation.
        by_bfs: Canton lookup by BFS number.

    Returns:
        The matching canton, or None for rows outside Swiss cantons.
    """
    abbreviation = to_str(row_value(row, options["canton_abbreviation_field"]))
    if abbreviation and abbreviation in by_abbreviation:
        return by_abbreviation[abbreviation]

    bfs_number = to_int(row_value(row, options["canton_bfs_field"]))
    if bfs_number is not None and bfs_number in by_bfs:
        return by_bfs[bfs_number]

    label_point = geom.point_on_surface
    for canton in cantons:
        if canton.geom.contains(label_point) or canton.geom.intersects(label_point):
            return canton
    return None


def assign_village_municipalities(
    villages: list[Village],
    dataset_version: GeoDatasetVersion,
) -> None:
    """Assign unambiguous parent municipalities to imported villages in bulk.

    Args:
        villages: Imported villages to assign.
        dataset_version: Dataset version of the imported village rows.
    """
    village_ids = [village.id for village in villages]
    if not village_ids:
        return

    village_table = connection.ops.quote_name(Village._meta.db_table)
    municipality_table = connection.ops.quote_name(Municipality._meta.db_table)
    query = f"""
        WITH matches AS (
            SELECT
                village.id AS village_id,
                MIN(municipality.id) AS municipality_id,
                COUNT(municipality.id) AS match_count
            FROM {village_table} village
            JOIN {municipality_table} municipality
              ON municipality.dataset_version_id = village.dataset_version_id
             AND municipality.canton_id = village.canton_id
             AND municipality.is_active = TRUE
             AND ST_Intersects(municipality.geom, village.label_point)
            WHERE village.dataset_version_id = %s
              AND village.id = ANY(%s)
              AND village.label_point IS NOT NULL
            GROUP BY village.id
        )
        UPDATE {village_table} village
        SET municipality_id = matches.municipality_id
        FROM matches
        WHERE village.id = matches.village_id
          AND matches.match_count = 1
        RETURNING village.id, village.municipality_id
    """
    with connection.cursor() as cursor:
        cursor.execute(query, [dataset_version.id, village_ids])
        assigned_municipalities = dict(cursor.fetchall())

    for village in villages:
        village.municipality_id = assigned_municipalities.get(village.id)


def to_date(value: Any) -> date | None:
    """Convert a source date value to a Python date.

    Args:
        value: Source value.

    Returns:
        Date value, or None when unavailable.

    Raises:
        CommandError: If the value cannot be parsed as a date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.to_datetime(value)
    except (TypeError, ValueError) as error:
        raise CommandError(f"Expected date-compatible value, got {value!r}.") from error
    if pd.isna(parsed):
        return None
    return parsed.date()
