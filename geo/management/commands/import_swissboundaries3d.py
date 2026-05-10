"""Download and import official swissBOUNDARIES3D boundaries."""

import json
import shutil
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from geo.management.commands._http import open_url_with_validated_redirects
from geo.management.commands.import_boundaries import (
    import_cantons,
    import_municipalities,
    read_layer,
)
from geo.models import Canton, GeoDatasetVersion, Municipality


STAC_ITEMS_URL = (
    "https://data.geo.admin.ch/api/stac/v1/collections/"
    "ch.swisstopo.swissboundaries3d/items"
)
DATASET_NAME = "swissBOUNDARIES3D"
CANTON_LAYER = "tlm_kantonsgebiet"
MUNICIPALITY_LAYER = "tlm_hoheitsgebiet"
CANTON_ABBREVIATION_FIELD = "_canton_abbreviation"
MUNICIPALITY_OBJECT_TYPE_FIELD = "objektart"
MUNICIPALITY_OBJECT_TYPE = "Gemeindegebiet"
ALLOWED_URL_SCHEMES = {"https"}
ALLOWED_URL_HOSTS = {"data.geo.admin.ch"}
UNIX_FILE_TYPE_MASK = 0o170000
UNIX_SYMLINK_TYPE = 0o120000

CANTON_ABBREVIATIONS = {
    1: "ZH",
    2: "BE",
    3: "LU",
    4: "UR",
    5: "SZ",
    6: "OW",
    7: "NW",
    8: "GL",
    9: "ZG",
    10: "FR",
    11: "SO",
    12: "BS",
    13: "BL",
    14: "SH",
    15: "AR",
    16: "AI",
    17: "SG",
    18: "GR",
    19: "AG",
    20: "TG",
    21: "TI",
    22: "VD",
    23: "VS",
    24: "NE",
    25: "GE",
    26: "JU",
}


class Command(BaseCommand):
    """Import the latest official swissBOUNDARIES3D dataset."""

    help = "Download and import official swissBOUNDARIES3D canton and municipality boundaries."

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument(
            "--stac-items-url",
            default=STAC_ITEMS_URL,
            help="STAC items endpoint for swissBOUNDARIES3D.",
        )
        parser.add_argument(
            "--dataset-version",
            default="",
            help="Optional dataset date to import, for example 2026-01-01.",
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
            help="Do not clear existing records for the selected dataset version first.",
        )

    def handle(self, *args, **options) -> None:
        """Run the official swissBOUNDARIES3D import.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        items = load_stac_items(options["stac_items_url"])
        item = select_stac_item(items, options["dataset_version"])
        asset = select_geopackage_asset(item)
        version_label = dataset_version_label(item)

        tmp_parent = Path(settings.BASE_DIR) / "data" / "raw"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_parent / f"swissboundaries3d_{uuid.uuid4().hex}"
        tmp_path.mkdir()
        try:
            archive_path = tmp_path / "swissboundaries3d.gpkg.zip"
            self.stdout.write(f"Downloading {asset['href']}")
            download_asset(asset["href"], archive_path)
            geopackage_path = extract_single_geopackage(archive_path, tmp_path)

            cantons, municipalities = import_dataset(
                geopackage_path=geopackage_path,
                version_label=version_label,
                source_url=asset["href"],
                simplify_tolerance=options["simplify_tolerance"],
                keep_existing=options["keep_existing"],
            )
        finally:
            shutil.rmtree(tmp_path, ignore_errors=True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(cantons)} cantons and {len(municipalities)} "
                f"municipalities from swissBOUNDARIES3D {version_label}."
            )
        )


def load_stac_items(url: str) -> dict[str, Any]:
    """Load swissBOUNDARIES3D STAC items.

    Args:
        url: STAC items endpoint URL.

    Returns:
        Parsed STAC FeatureCollection.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with open_url_with_validated_redirects(
            request,
            timeout=60,
            validate_url=validate_url_scheme,
        ) as response:
            return json.load(response)
    except OSError as error:
        raise CommandError(f"Could not load STAC items: {error}") from error


def select_stac_item(items: dict[str, Any], requested_version: str = "") -> dict[str, Any]:
    """Select a swissBOUNDARIES3D STAC item.

    Args:
        items: Parsed STAC FeatureCollection.
        requested_version: Optional version date requested by the user.

    Returns:
        The matching item, or the newest item when no version was requested.

    Raises:
        CommandError: If no matching item exists.
    """
    features = items.get("features") or []
    if not features:
        raise CommandError("No swissBOUNDARIES3D STAC items found.")

    if requested_version:
        for feature in features:
            if dataset_version_label(feature) == requested_version:
                return feature
        raise CommandError(f"No swissBOUNDARIES3D item found for {requested_version}.")

    return max(features, key=dataset_version_label)


def dataset_version_label(item: dict[str, Any]) -> str:
    """Return a stable dataset version label for a STAC item.

    Args:
        item: STAC item.

    Returns:
        Dataset date in YYYY-MM-DD format.
    """
    datetime_value = (item.get("properties") or {}).get("datetime", "")
    if datetime_value:
        return datetime_value[:10]
    item_id = item.get("id", "")
    if "_" in item_id:
        return item_id.rsplit("_", maxsplit=1)[-1]
    return item_id


def select_geopackage_asset(item: dict[str, Any]) -> dict[str, Any]:
    """Select the GeoPackage ZIP asset from a STAC item.

    Args:
        item: STAC item.

    Returns:
        GeoPackage asset dictionary.

    Raises:
        CommandError: If the item has no GeoPackage ZIP asset.
    """
    for asset in (item.get("assets") or {}).values():
        href = asset.get("href", "")
        media_type = asset.get("type", "")
        if href.endswith(".gpkg.zip") or "geopackage" in media_type:
            return asset
    raise CommandError(f"No GeoPackage asset found for {item.get('id', 'item')}.")


def download_asset(url: str, destination: Path) -> None:
    """Download an asset to disk.

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
        raise CommandError(f"Could not download swissBOUNDARIES3D asset: {error}") from error


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


def extract_single_geopackage(archive_path: Path, destination: Path) -> Path:
    """Extract one GeoPackage file from a ZIP archive.

    Args:
        archive_path: ZIP archive path.
        destination: Extraction directory.

    Returns:
        Extracted GeoPackage path.

    Raises:
        CommandError: If the archive does not contain exactly one GeoPackage.
    """
    try:
        with zipfile.ZipFile(archive_path) as archive:
            safe_extract_zip(archive, destination)
    except zipfile.BadZipFile as error:
        raise CommandError("Downloaded swissBOUNDARIES3D asset is not a valid ZIP.") from error

    geopackages = list(destination.rglob("*.gpkg"))
    if len(geopackages) != 1:
        raise CommandError(
            f"Expected one GeoPackage in swissBOUNDARIES3D ZIP, found {len(geopackages)}."
        )
    return geopackages[0]


def safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract a ZIP archive without allowing path traversal.

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
            raise CommandError("Unsafe symlink found in swissBOUNDARIES3D ZIP.")
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
        raise CommandError("Unsafe path found in swissBOUNDARIES3D ZIP.") from error
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


def add_canton_abbreviations(canton_gdf):
    """Add Swiss canton abbreviations to the official canton GeoDataFrame.

    Args:
        canton_gdf: Official swissBOUNDARIES3D canton GeoDataFrame.

    Returns:
        A copy with an import helper abbreviation column.

    Raises:
        CommandError: If a canton number has no known abbreviation.
    """
    canton_gdf = canton_gdf.copy()
    canton_gdf[CANTON_ABBREVIATION_FIELD] = canton_gdf["kantonsnummer"].map(
        canton_abbreviation
    )
    if canton_gdf[CANTON_ABBREVIATION_FIELD].isna().any():
        raise CommandError("Could not map every canton number to an abbreviation.")
    return canton_gdf


def canton_abbreviation(value) -> str | None:
    """Map an official swissBOUNDARIES3D canton number to its abbreviation.

    Args:
        value: Canton number from swissBOUNDARIES3D.

    Returns:
        Canton abbreviation, or None when the value cannot be mapped.
    """
    if value is None:
        return None
    try:
        return CANTON_ABBREVIATIONS.get(int(value))
    except (TypeError, ValueError):
        return None


def filter_municipality_gdf(municipality_gdf):
    """Keep only political municipality surfaces from swissBOUNDARIES3D.

    Args:
        municipality_gdf: Official swissBOUNDARIES3D municipality GeoDataFrame.

    Returns:
        Filtered GeoDataFrame containing only municipality surfaces.
    """
    filtered_gdf = municipality_gdf
    if MUNICIPALITY_OBJECT_TYPE_FIELD in municipality_gdf.columns:
        filtered_gdf = filtered_gdf[
            filtered_gdf[MUNICIPALITY_OBJECT_TYPE_FIELD] == MUNICIPALITY_OBJECT_TYPE
        ]
    if "kantonsnummer" in filtered_gdf.columns:
        filtered_gdf = filtered_gdf[
            filtered_gdf["kantonsnummer"].map(canton_abbreviation).notna()
        ]
    return filtered_gdf.copy()


def import_dataset(
    geopackage_path: Path,
    version_label: str,
    source_url: str,
    simplify_tolerance: float,
    keep_existing: bool,
) -> tuple[list[Canton], list[Municipality]]:
    """Import official swissBOUNDARIES3D layers from a GeoPackage.

    Args:
        geopackage_path: Official GeoPackage path.
        version_label: Dataset version label.
        source_url: Official source URL.
        simplify_tolerance: Display-geometry simplification tolerance.
        keep_existing: Whether existing records for the dataset should remain.

    Returns:
        Imported canton and municipality objects.
    """
    canton_gdf = add_canton_abbreviations(read_layer(geopackage_path, CANTON_LAYER))
    municipality_gdf = filter_municipality_gdf(
        read_layer(geopackage_path, MUNICIPALITY_LAYER)
    )
    import_options = {
        "canton_abbreviation_field": CANTON_ABBREVIATION_FIELD,
        "canton_bfs_field": "kantonsnummer",
        "canton_name_field": "name",
        "municipality_area_field": "",
        "municipality_bfs_field": "bfs_nummer",
        "municipality_canton_abbreviation_field": "",
        "municipality_canton_bfs_field": "kantonsnummer",
        "municipality_name_field": "name",
        "simplify_tolerance": simplify_tolerance,
    }

    with transaction.atomic():
        dataset_version, _ = GeoDatasetVersion.objects.update_or_create(
            name=DATASET_NAME,
            version_label=version_label,
            defaults={
                "source_url": source_url,
                "notes": "Official swissBOUNDARIES3D dataset from data.geo.admin.ch.",
                "imported_at": timezone.now(),
            },
        )
        if not keep_existing:
            Municipality.objects.filter(dataset_version=dataset_version).delete()
            Canton.objects.filter(dataset_version=dataset_version).delete()

        cantons = import_cantons(canton_gdf, dataset_version, import_options)
        municipalities = import_municipalities(
            municipality_gdf,
            dataset_version,
            cantons,
            import_options,
        )

    return cantons, municipalities
