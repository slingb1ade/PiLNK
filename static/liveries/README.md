# static/liveries: the artwork slot

The 3D Approach view paints airline colours itself (`static/livery.js`, `LIVERIES`). It draws **no logos,
titles or emblems**. Those come from image files you put here, one folder per airline:

```
static/liveries/
  ANZ/
    art.json
    titles.png
    fin.png
    aft.png
```

The folder name is the ICAO callsign prefix: ANZ for Air New Zealand, JST for Jetstar. With no folder,
or no `art.json`, the aircraft just shows its colours.

## art.json

```json
{
  "titles": { "file": "titles.png", "b": [0.14, 0.42], "yc": 0.78 },
  "fin":    { "file": "fin.png",    "b": [0.82, 0.95], "hc": 0.55 },
  "aft":    { "file": "aft.png",    "b": [0.60, 0.98], "yc": 0.45 },
  "types": {
    "AT76": { "titles": { "file": "titles.png", "b": [0.16, 0.45], "yc": 0.82 } }
  }
}
```

Every item is optional.

| Item | Goes on | Position |
|---|---|---|
| `titles` | the fuselage, both sides | `b` = from/to along the length (0 = nose, 1 = tail); `yc` = centre height (0 = belly, 1 = roof) |
| `aft` | the fuselage, both sides (artwork over the rear) | same as `titles` |
| `fin` | the fin, both sides | `b` = from/to along the length; `hc` = centre height up the fin (0 = roof, 1 = fin tip) |

- **Size.** Width comes from `b`. Height comes from the image's own shape, so nothing is stretched.
- **Both sides read correctly.** Draw the image as it should look on the **left** side of the aircraft
  (nose to the left). The right side gets it flipped back, so writing reads the right way there too.
  Add `"right": "mirror"` to an item to draw it mirrored on the right side instead.
- **`types`** overrides items for one ICAO type (AT76, A20N, B789…). Positions differ between a 787 and an
  ATR, so tune them per type here.
- **Images.** PNG with a transparent background. White artwork for over a black area, dark for over white.
  Around 1000–2000 px wide is plenty.

## Checking it

Hard-refresh the 3D view (Ctrl+Shift+R) and open an aircraft of that airline, or add `&livery=ANZ` to the
address to put it on whatever is showing. The artwork arrives a moment after the aircraft and is painted on
when it does. Each airline's files load once per page.

## Which nodes see it

This folder is served by each node from its own copy of the repo. Files committed to git go to every node
with the next release. Files left uncommitted stay on the node where you put them.
