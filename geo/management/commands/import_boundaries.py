"""Import Swiss canton and municipality boundaries."""

import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from shapely import force_2d

from geo.models import Canton, GeoDatasetVersion, Municipality


VECTOR_SUFFIXES = {".gpkg", ".gdb", ".shp", ".geojson", ".json"}


class Command(BaseCommand):
    """Import canton and municipality boundaries from a vector datasource."""

    help = "Import swissBOUNDARIES3D canton and municipality boundaries."

    def add_arguments(self, parser) -> None:
        """Configure command-line arguments.

        Args:
            parser: Django argument parser.
        """
        parser.add_argument("source", help="Path to a vector file or datasource folder.")
        parser.add_argument(
            "--dataset-name",
            default="swissBOUNDARIES3D",
            help="Dataset source name.",
        )
        parser.add_argument(
            "--dataset-version",
            required=True,
            help="Dataset version label, for example 2026-01-01.",
        )
        parser.add_argument("--source-url", default="", help="Optional source URL.")
        parser.add_argument("--notes", default="", help="Optional dataset notes.")
        parser.add_argument("--municipality-layer", help="Municipality layer name.")
        parser.add_argument("--canton-layer", help="Canton layer name.")
        parser.add_argument(
            "--list-layers",
            action="store_true",
            help="List available layers and exit.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing records for this dataset before importing.",
        )
        parser.add_argument(
            "--simplify-tolerance",
            type=float,
            default=0.0,
            help="Optional simplification tolerance in WGS84 degrees.",
        )
        parser.add_argument(
            "--municipality-bfs-field",
            default="BFS_NUMMER",
            help="Municipality BFS number column.",
        )
        parser.add_argument(
            "--municipality-name-field",
            default="NAME",
            help="Municipality name column.",
        )
        parser.add_argument(
            "--municipality-canton-bfs-field",
            default="KANTONSNUM",
            help="Municipality canton BFS number column.",
        )
        parser.add_argument(
            "--municipality-canton-abbreviation-field",
            default="KANTON",
            help="Municipality canton abbreviation column.",
        )
        parser.add_argument(
            "--municipality-area-field",
            default="",
            help="Optional municipality area column in square kilometers.",
        )
        parser.add_argument(
            "--canton-bfs-field",
            default="BFS_NUMMER",
            help="Canton BFS number column.",
        )
        parser.add_argument(
            "--canton-abbreviation-field",
            default="KANTON",
            help="Canton abbreviation column.",
        )
        parser.add_argument(
            "--canton-name-field",
            default="NAME",
            help="Canton name column.",
        )

    def handle(self, *args, **options) -> None:
        """Run the boundary import.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options.
        """
        source = resolve_data_source(options["source"])

        if options["list_layers"] or not (
            options["municipality_layer"] and options["canton_layer"]
        ):
            list_available_layers(source, self.stdout)
            if options["list_layers"] or not (
                options["municipality_layer"] or options["canton_layer"]
            ):
                return
            raise CommandError(
                "Both --municipality-layer and --canton-layer are required for import."
            )

        municipality_gdf = read_layer(source, options["municipality_layer"])
        canton_gdf = read_layer(source, options["canton_layer"])

        with transaction.atomic():
            import_time = timezone.now()
            dataset_version, _ = GeoDatasetVersion.objects.update_or_create(
                name=options["dataset_name"],
                version_label=options["dataset_version"],
                defaults={
                    "source_url": options["source_url"],
                    "notes": options["notes"],
                    "imported_at": import_time,
                    "boundaries_updated_at": import_time,
                },
            )

            if options["clear"]:
                Municipality.objects.filter(dataset_version=dataset_version).delete()
                Canton.objects.filter(dataset_version=dataset_version).delete()

            cantons = import_cantons(canton_gdf, dataset_version, options)
            municipalities = import_municipalities(
                municipality_gdf,
                dataset_version,
                cantons,
                options,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(cantons)} cantons and {len(municipalities)} "
                f"municipalities for {dataset_version}."
            )
        )


def resolve_data_source(source: str) -> Path:
    """Resolve a vector datasource path.

    Args:
        source: User-provided file or folder path.

    Returns:
        The resolved datasource path.

    Raises:
        CommandError: If no supported datasource can be found.
    """
    path = Path(source)
    if path.exists() and (path.is_file() or path.suffix.lower() == ".gdb"):
        return path
    if not path.exists():
        raise CommandError(f"Datasource does not exist: {source}")
    if not path.is_dir():
        raise CommandError(f"Datasource is not a file or folder: {source}")

    candidates = sorted(
        child
        for child in path.rglob("*")
        if child.is_file() and child.suffix.lower() in VECTOR_SUFFIXES
    )
    candidates.extend(
        child
        for child in sorted(path.rglob("*.gdb"))
        if child.is_dir()
    )
    if not candidates:
        raise CommandError(f"No supported vector datasource found in: {source}")
    if len(candidates) > 1:
        candidate_list = "\n".join(str(candidate) for candidate in candidates[:10])
        raise CommandError(
            "Multiple vector datasources found. Pass one explicitly:\n"
            f"{candidate_list}"
        )
    return candidates[0]


def list_available_layers(source: Path, stdout) -> None:
    """List vector layers in a datasource.

    Args:
        source: Datasource path.
        stdout: Django command output stream.
    """
    import pyogrio

    layers = pyogrio.list_layers(source)
    stdout.write("Available layers:")
    for layer in layers:
        try:
            name = layer[0]
            geometry_type = layer[1] if len(layer) > 1 else ""
        except (TypeError, IndexError):
            name = layer
            geometry_type = ""
        suffix = f" ({geometry_type})" if geometry_type else ""
        stdout.write(f"- {name}{suffix}")


def read_layer(source: Path, layer: str):
    """Read one datasource layer as WGS84 GeoDataFrame.

    Args:
        source: Datasource path.
        layer: Layer name to read.

    Returns:
        A GeoDataFrame in EPSG:4326.

    Raises:
        CommandError: If the layer has no CRS.
    """
    import geopandas as gpd

    gdf = gpd.read_file(source, layer=layer)
    if gdf.crs is None:
        raise CommandError(f"Layer {layer} has no CRS.")
    return gdf.to_crs(epsg=4326)


def import_cantons(
    gdf,
    dataset_version: GeoDatasetVersion,
    options: dict[str, Any],
) -> list[Canton]:
    """Import canton rows.

    Args:
        gdf: Canton GeoDataFrame.
        dataset_version: Dataset version for imported rows.
        options: Parsed command options.

    Returns:
        Imported or updated canton objects.
    """
    cantons = []
    for _, row in gdf.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        geom = shapely_to_multipolygon(geometry)
        simplified_geom = simplify_geometry(geom, options["simplify_tolerance"])
        abbreviation = to_str(
            row_value(row, options["canton_abbreviation_field"])
        )
        name = to_str(row_value(row, options["canton_name_field"]))
        bfs_number = to_int(row_value(row, options["canton_bfs_field"]))
        if not abbreviation or not name:
            raise CommandError("Canton rows require abbreviation and name values.")

        canton, _ = Canton.objects.update_or_create(
            dataset_version=dataset_version,
            abbreviation=abbreviation,
            defaults={
                "bfs_number": bfs_number,
                "name": name,
                "geom": geom,
                "geom_simplified": simplified_geom,
                "label_point": geom.point_on_surface,
            },
        )
        cantons.append(canton)
    return cantons


def import_municipalities(
    gdf,
    dataset_version: GeoDatasetVersion,
    cantons: list[Canton],
    options: dict[str, Any],
) -> list[Municipality]:
    """Import municipality rows.

    Args:
        gdf: Municipality GeoDataFrame.
        dataset_version: Dataset version for imported rows.
        cantons: Imported canton objects for matching.
        options: Parsed command options.

    Returns:
        Imported or updated municipality objects.
    """
    by_abbreviation = {canton.abbreviation: canton for canton in cantons}
    by_bfs = {
        canton.bfs_number: canton
        for canton in cantons
        if canton.bfs_number is not None
    }
    municipalities = []

    for _, row in gdf.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        geom = shapely_to_multipolygon(geometry)
        simplified_geom = simplify_geometry(geom, options["simplify_tolerance"])
        bfs_number = to_int(row_value(row, options["municipality_bfs_field"]))
        name = to_str(row_value(row, options["municipality_name_field"]))
        if bfs_number is None or not name:
            raise CommandError("Municipality rows require BFS number and name values.")
        canton = find_canton(row, geom, by_abbreviation, by_bfs, cantons, options)
        area_km2 = to_float(row_value(row, options["municipality_area_field"]))

        municipality, _ = Municipality.objects.update_or_create(
            dataset_version=dataset_version,
            bfs_number=bfs_number,
            defaults={
                "name": name,
                "canton": canton,
                "area_km2": area_km2,
                "geom": geom,
                "geom_simplified": simplified_geom,
                "label_point": geom.point_on_surface,
                "is_active": True,
            },
        )
        municipalities.append(municipality)
    return municipalities


def find_canton(
    row,
    geom: MultiPolygon,
    by_abbreviation: dict[str, Canton],
    by_bfs: dict[int, Canton],
    cantons: list[Canton],
    options: dict[str, Any],
) -> Canton:
    """Find the canton for a municipality row.

    Args:
        row: Municipality row.
        geom: Municipality geometry.
        by_abbreviation: Canton lookup by abbreviation.
        by_bfs: Canton lookup by BFS number.
        cantons: Imported canton objects.
        options: Parsed command options.

    Returns:
        The matching canton.

    Raises:
        CommandError: If no canton can be matched.
    """
    abbreviation = to_str(
        row_value(row, options["municipality_canton_abbreviation_field"])
    )
    if abbreviation and abbreviation in by_abbreviation:
        return by_abbreviation[abbreviation]

    bfs_number = to_int(row_value(row, options["municipality_canton_bfs_field"]))
    if bfs_number is not None and bfs_number in by_bfs:
        return by_bfs[bfs_number]

    label_point = geom.point_on_surface
    for canton in cantons:
        if canton.geom.contains(label_point) or canton.geom.intersects(label_point):
            return canton

    municipality_name = to_str(row_value(row, options["municipality_name_field"]))
    municipality_bfs = to_int(row_value(row, options["municipality_bfs_field"]))
    raise CommandError(
        "Could not match municipality row to a canton: "
        f"{municipality_name or 'unknown'} ({municipality_bfs or 'unknown'})."
    )


def shapely_to_multipolygon(shapely_geometry) -> MultiPolygon:
    """Convert a Shapely polygon geometry to a GEOS MultiPolygon.

    Args:
        shapely_geometry: Shapely Polygon or MultiPolygon geometry.

    Returns:
        A GEOS MultiPolygon with SRID 4326.

    Raises:
        CommandError: If the geometry is not polygonal.
    """
    geometry = GEOSGeometry(memoryview(force_2d(shapely_geometry).wkb), srid=4326)
    if geometry.geom_type == "Polygon":
        return MultiPolygon(geometry, srid=4326)
    if geometry.geom_type == "MultiPolygon":
        geometry.srid = 4326
        return geometry
    raise CommandError(f"Unsupported geometry type: {geometry.geom_type}")


def simplify_geometry(geometry: MultiPolygon, tolerance: float) -> MultiPolygon | None:
    """Simplify a geometry while preserving topology.

    Args:
        geometry: Geometry to simplify.
        tolerance: Simplification tolerance.

    Returns:
        Simplified geometry, or None when simplification is disabled.
    """
    if tolerance <= 0:
        return None
    simplified = geometry.simplify(tolerance, preserve_topology=True)
    if simplified.geom_type == "Polygon":
        return MultiPolygon(simplified, srid=4326)
    simplified.srid = 4326
    return simplified


def row_value(row, field: str) -> Any:
    """Read a row value if the field exists.

    Args:
        row: GeoDataFrame row.
        field: Column name to read.

    Returns:
        The row value or None.
    """
    if not field or field not in row.index:
        return None
    value = row[field]
    if pd.isna(value):
        return None
    return value


def to_str(value: Any) -> str:
    """Convert a value to stripped text.

    Args:
        value: Value to convert.

    Returns:
        Stripped string or an empty string.
    """
    return "" if value is None else str(value).strip()


def to_int(value: Any) -> int | None:
    """Convert a value to integer.

    Args:
        value: Value to convert.

    Returns:
        Integer value or None.

    Raises:
        CommandError: If the value cannot be converted to an integer.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise CommandError(
            f"Expected integer-compatible value, got {value!r}."
        ) from error
    if not number.is_finite() or number != number.to_integral_value():
        raise CommandError(f"Expected whole-number value, got {value!r}.")
    return int(number)


def to_float(value: Any) -> float | None:
    """Convert a value to float.

    Args:
        value: Value to convert.

    Returns:
        Float value or None.

    Raises:
        CommandError: If the value cannot be converted to a float.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CommandError(f"Expected float-compatible value, got {value!r}.") from error
    if not math.isfinite(number):
        raise CommandError(f"Expected finite float-compatible value, got {value!r}.")
    return number
