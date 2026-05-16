# Data Import

Find the Municipality! needs municipality boundaries before a real municipality
game can be played. Village games additionally need village/locality boundaries.
Population values are optional for gameplay but enrich municipality reveals and
profile/history data.

## Recommended Setup

Use the combined setup command after migrations when you want real Swiss data:

```cmd
python manage.py setup_geodata
```

It downloads and imports:

- swissBOUNDARIES3D canton and municipality boundaries from swisstopo
- BFS STATPOP population values

This is also available from the Django admin geodata setup screen.

Village games require one extra import after the municipality dataset exists:

```cmd
python manage.py import_villages
```

This downloads the official swisstopo locality shapefile and attaches villages
to the current geodata version. Existing village games protect their referenced
village records, so replacing village data should normally happen by importing a
newer dataset version rather than clearing records in use.

## Quick Dummy Data

For a fast smoke test without downloading official data:

```cmd
python manage.py seed_dev_geodata
```

This creates five small dummy municipalities in one development canton. It is
only meant for checking that the municipality game flow works. It does not seed
village targets.

## Separate Commands

Import only official boundaries:

```cmd
python manage.py import_swissboundaries3d
```

Import only official village/locality boundaries for the current dataset:

```cmd
python manage.py import_villages
```

Import only population values for already imported municipalities:

```cmd
python manage.py import_statpop_population
```

Import population from a local CSV:

```cmd
python manage.py import_population data/raw/population.csv
```

The CSV must contain a municipality BFS number and a population value. Column
names can be changed with `--bfs-column` and `--population-column`.

## Local Files

Downloaded and manually supplied source files belong in `data/raw/`. That
directory is intentionally not committed to Git.

## Notes

- The current dataset is the newest `GeoDatasetVersion` by import time.
- Boundary GeoJSON uses simplified geometries when available.
- Municipality and village boundary GeoJSON intentionally does not expose target
  names during guessing.
- Municipality labels are only available after a turn has been revealed.
- Village games can optionally render municipality boundaries as a visual
  overlay, but the village polygons remain the scoring and validation target.
