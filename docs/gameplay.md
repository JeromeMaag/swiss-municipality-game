# Gameplay

Find the Municipality! is about finding Swiss municipalities on a map.

## How To Play

1. Choose a game mode: all of Switzerland or a single canton.
2. Start a game as a guest, or log in if you want account-based history and
   statistics later.
3. The game shows the name of one target municipality.
4. Move around the map and place your pin where you think the municipality is.
5. Confirm the guess.
6. The game reveals the target municipality, your distance, your score, the
   canton, and the population if it is available.
7. Continue with the next turn.

A game has five turns. After the fifth guess, the game ends and the summary page
shows all turns and the total score. The summary can start another game in the
same mode, or return to the mode picker.

Signed-in players can review finished games from the history page and see
aggregate statistics on their profile. Guest games stay available only in the
same browser session and are not included in account history or statistics.

## Scoring

The score is based on the shortest distance from your pin to the target
municipality polygon. The scoring curve scales with the played map area, so
single-canton games use a stricter distance scale than the full Switzerland map.

If the pin is inside the correct municipality, the distance is `0 m` and the
turn receives the maximum score of `1000`. Guesses outside the target
municipality can still score highly, but are capped at `999`.

## Reveal

Before the guess, the map shows municipality and canton boundaries without
municipality names. After the guess, the target municipality is highlighted and
municipality labels can appear when zoomed in far enough.

Reveal lines use the nearest target boundary point calculated by PostGIS, so the
line endpoint matches the distance used for scoring.

## Map Settings

The settings button on the map lets players switch between available background
maps, choose a boundary line theme (`auto`, `white`, or `black`), and set which
outlines are shown (`all`, `cantons`, `municipalities`, or `off`). These choices
are stored in the browser, so guest and signed-in players keep the same map
preference on the same device.

## Language

The interface language follows the browser preference by default. Signed-in
players can override it from their profile settings; the choice is stored in
Django's language cookie and applies to the same browser.
