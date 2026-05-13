# Architecture

Find the Municipality! is a small Django app with server-side game logic and a
thin Leaflet frontend.

## Apps

`accounts`
: Registration, login, logout, profile settings, and personal statistics.

`geo`
: Imported canton and municipality data, GeoJSON endpoints, and data import
commands.

`game`
: Game sessions, turns, guesses, player identity, scoring, page views,
  summaries, history, and statistics aggregation.

`tracking`
: Persisted gameplay events such as game start, map clicks, reveals, and game
finish.

## Data Flow

1. A player chooses Switzerland mode or a single-canton mode.
2. The backend resolves the player identity as either an authenticated user or a
   guest browser key.
3. The backend creates five turns with unique active municipalities inside the
   chosen map scope.
4. The game page loads neutral municipality boundaries and canton boundaries for
   that scope.
5. The user places a pin and submits a guess.
6. Django validates ownership and the turn, calculates distance and score in
   PostGIS, stores the guess, and reveals the turn.
7. The reveal view highlights the target municipality and can load municipality
   labels.
8. After five turns, the game is marked as finished and the summary page shows
   the result.
9. Signed-in users can replay finished games from history and see aggregate
   statistics on their profile.

## Geodata

The database is the source of truth for gameplay:

- target municipalities
- boundaries
- guesses
- distances
- scores
- tracking events

swisstopo tiles are only used as visual map background. Guess validation and
distance calculation never depend on a live map service.

## GeoJSON Endpoints

The game uses three GeoJSON endpoint types:

- canton boundaries with canton names
- municipality boundaries without names
- municipality labels after reveal

Municipality names are deliberately withheld during the guessing phase. Label
access is tied to a revealed turn owned by the current player identity. Boundary
endpoints can be scoped with a canton query parameter for single-canton games.

## Frontend

The frontend is plain Django templates, CSS, and a small `game_map.js` file.
Leaflet handles the map interaction. The browser only places pins and renders
layers; the backend owns validation, scoring, and persistence.

Map background, boundary line color, and outline visibility are browser
preferences stored in `localStorage`. They affect presentation only and do not
change game validation or scoring.

## Internationalization

User-facing UI strings use Django's translation system. English is the source
language, and supported languages are configured in `LANGUAGES` in
`config/settings.py`.

Templates use `{% trans %}` or `{% blocktrans %}`. Python code uses
`gettext()` at the point where user-facing messages are created. Profile
language choices are built from `settings.LANGUAGES` so language names stay in
their native form.

When adding or changing UI strings, update the relevant
`locale/<language>/LC_MESSAGES/django.po` files and commit the compiled
`django.mo` files as well, so deployments do not depend on gettext tooling
being available at runtime.

## Tracking

Tracking events are stored from both backend actions and lightweight frontend
events. Tracking failures must not block gameplay.
