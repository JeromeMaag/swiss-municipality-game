# Data Import

GemeindeGuess CH needs municipality boundaries and population values before a
real game can be played.

## Recommended Setup

Use the combined setup command for local development:

```cmd
python manage.py setup_geodata
```

It downloads and imports:

- swissBOUNDARIES3D canton and municipality boundaries from swisstopo
- BFS STATPOP population values

This is the command for a new setup normally run after migrations.

## Quick Dummy Data

For a fast smoke test without downloading official data:

```cmd
python manage.py seed_dev_geodata
```

This creates five small dummy municipalities. It is only meant for checking that
the game flow works.

## Separate Commands

Import only official boundaries:

```cmd
python manage.py import_swissboundaries3d
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

Downloaded source files belong in `data/raw/`. That directory is intentionally
not committed to Git.

## Notes

- The current dataset is the newest `GeoDatasetVersion` by import time.
- Boundary GeoJSON uses simplified geometries when available.
- Municipality boundary GeoJSON intentionally does not expose municipality names
  during guessing.
- Municipality labels are only available after a turn has been revealed.
