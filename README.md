# Mountain Lake Finder

Finds **lakes with mountains around them**, and the **towns and villages**
near such lakes, using OpenStreetMap. It works anywhere in the world. Both are
ranked on a scoreboard, and lakes that look **blue** (fed by a glacier) earn
bonus points.

Why glacier-fed lakes are blue: an active glacier grinds the rock under it
into fine "rock flour". Meltwater carries that flour down rocky valleys into
the lake, and the suspended particles scatter light, so the water looks
turquoise or blue.

No API keys are needed, and the only requirement is Python 3.9+ (no extra packages).

## Usage

```bash
python3 -m lakefinder --place "Interlaken, Switzerland" --radius 20 --map interlaken.html
```

```bash
python3 -m lakefinder --lat 51.42 --lon -116.18 --radius 15 --json banff.json
```

```bash
python3 -m lakefinder --bbox 46.6,8.27,46.83,8.59 --save-raw susten-raw.json
```

```bash
python3 -m lakefinder --from-file susten-raw.json --glacier-km 3 --map susten.html
```

The terminal output has two scoreboards:

```
LAKE SCOREBOARD — lakes with mountains around them (blue lakes get bonus points)
  #  Lake                        Score    Mtn  Lake Blue+  Colour         Elev  Nearest place
  1  Laguna Sucia                 38.0   23.6   6.0   8.4  blue          890 m  El Chalten 9.13 km
  2  Laguna Torre                 37.8   23.3   6.0   8.5  blue          636 m  El Chalten 7.44 km
  ...
PLACE SCOREBOARD — towns and villages near mountains and lakes
  1  El Chalten                   29.0    7.2   8.7  13.1  Laguna Torre (blue) 7.44 km
```

- `--map` writes an interactive map (topo basemap, Lakes / Places scoreboard tabs, popups explaining each score).
- `--json` writes the full results.
- `--save-raw` / `--from-file` let you download an area once and re-score it offline with different settings.
- Every value in `Settings` (`lakefinder/scoring.py`) is a CLI flag, e.g. `--mountain-km 15`, `--min-lake-ha 5`, `--blue-threshold 0.6`. Run `--help` for the list.

Example maps for Sustenpass (Swiss Alps), Lake Louise (Canadian Rockies) and El Chaltén (Patagonia) are in `examples/`.

## How a lake is judged glacier-fed

Each signal found on the map adds to a 0–1 confidence score:

| Signal on the map | OSM data used | Weight |
|---|---|---|
| Glacier word in the lake's name (Gletschersee, Glacier Lake, Lago del Ghiacciaio, Buzul Gölü…) | `name*`, `alt_name` | +0.35 |
| Colour word in the name (Blausee, Turquoise Lake, Lago Azul, Mavi Göl…) | `name*` | +0.15 |
| Colour / origin tag (`colour=turquoise`, `water=glacial_lake`…) | tags | +0.30 |
| Active glacier within 5 km | `natural=glacier` | up to +0.30 |
| That glacier sits higher than the lake (upstream) | `ele` tag, or Open-Meteo terrain elevation | +0.10 |
| A meltwater stream traced from the glacier down into the lake | `waterway=stream/river`, followed in flow direction | +0.30 |
| Rocky glacial terrain within 2 km (scree, bare rock, moraine, arête, cliffs) | `natural=*`, `geological=moraine` | +0.15 (+0.20 with a moraine) |
| High-altitude lake (≥ 1500 m) | `ele` / terrain elevation | +0.10 |

A lake needs **glacier evidence and rocky-valley evidence** and a confidence of at least 0.5. Then:

- **blue**: we can confirm the glacier drains into it (traced stream, glacier name or tag). Earns the full bonus.
- **likely blue**: a glacier sits above it in rocky terrain, but no feeding stream is mapped. Earns half the bonus.

## How lakes are scored (lake scoreboard)

For each lake of at least 1 ha:

- **Mountains**: each peak within 10 km of the shore gives 1 point, plus 1 per 1000 m of height above 1000 m. Closer peaks count more, and the total levels off softly at 25.
- **Lake**: 4 points, plus 2 if larger than 50 ha.
- **Blue lake bonus (+)**: 10 × confidence for a blue lake, or half that for a likely-blue lake.

Lakes with no peaks within 10 km are left off the scoreboard unless you pass `--all`.

## How places are scored (place scoreboard)

For each city, town, village or hamlet:

- **Mountains**: each peak within 10 km gives 1 point, plus 1 per 1000 m of height above 1000 m. Closer peaks count more, and the total levels off softly at 25.
- **Lakes**: the best 5 lakes within 10 km give 4 points each, plus 2 if larger than 50 ha. Closer lakes count more.
- **Blue lake bonus**: 10 × confidence for each blue lake (half that for likely blue), with the same distance weighting.

Places with no mountains or no lakes nearby are skipped unless you pass `--all`.

## Limitations

- The result is only as good as OSM mapping. Glaciers are retreating faster than maps get updated, and some meltwater streams aren't mapped.
- Not every glacier lake is blue (very muddy proglacial lakes can be grey), and some clear blue lakes aren't glacial.
- The public Overpass servers are rate-limited. Large areas (radius > ~30 km in the Alps) can time out, so split them up or use `--save-raw`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
