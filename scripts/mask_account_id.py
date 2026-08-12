#!/usr/bin/env python3
"""Redact the AWS console identity chip from screenshots.

The light pill in the top-right corner of every console screenshot reads
`<operator> (<account id>)`. It is covered with a soft rounded bar drawn in the tone
of the pill itself — darker than the pill, never black — so the result reads as a
deliberate redaction rather than a rendering fault or a censor block.

The chevron to the right of the pill is left alone: it belongs to the console, not to
the identity, and keeping it makes the pill still look like a pill. So does the
operator name printed *below* the pill, which carries no account id.

Nothing here is hard-coded to one screenshot. The console renders that pill at a
different width, height and offset depending on the page — 2701..3001 on an Athena
page, 2620..2997 on a Bedrock one — so a fixed rectangle would silently half-mask most
of them. The pill is found by its fill colour, the text inside it by its darkness, and
the bar is sized from what was found. A file where no pill is found is skipped and
reported, never guessed at.

    python scripts/mask_account_id.py --check    # every image; non-zero if any is legible
    python scripts/mask_account_id.py            # rewrite them in place

`--check` is preflight's, and it is why this file is a check and not a note in a README:
gitleaks gates the account id in text, and thirty-five committed screenshots carried it
past that gate because a scanner reads bytes and this was pixels.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 4

#: The pill's fill in the console's dark theme.
PILL_FILL_COLOUR = (127, 137, 151)
PILL_TOLERANCE = 10

#: Text inside the pill is near-black; this separates it from the fill.
TEXT_MAX_MEAN = 100

#: The bar: darker than the pill, and never black.
BAR_FILL = (101, 110, 124)

#: How far down to look, and how far in from the right edge.
SEARCH_HEIGHT = 140
SEARCH_WIDTH = 500

#: A run must reach this close to the right edge, and be at least this wide, to be the
#: pill. The window chrome above the console is the same grey and spans the full width,
#: so a run touching the left edge of the search window is chrome and is rejected.
RIGHT_MARGIN = 40
MIN_PILL_WIDTH = 150

#: A trailing run this narrow, after a gap this wide, is the chevron — leave it.
CHEVRON_MAX_WIDTH = 30
CHEVRON_MIN_GAP = 6

#: How far inside the pill to look for text, and the narrowest run that is a glyph
#: rather than antialiasing on the pill's rounded corner.
INSET = 8
MIN_GLYPH_WIDTH = 3

#: Anything smaller than a console screenshot has no console chrome to redact.
MIN_SCREENSHOT_WIDTH = 2000
MIN_SCREENSHOT_HEIGHT = 400


def _near(colour, target=PILL_FILL_COLOUR, tol=PILL_TOLERANCE) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(colour, target, strict=False))


def _runs(values: list[int], gap: int) -> list[tuple[int, int]]:
    """Contiguous runs in a sorted list, split wherever the step exceeds `gap`."""
    if not values:
        return []
    found, start, previous = [], values[0], values[0]
    for value in values[1:]:
        if value - previous > gap:
            found.append((start, previous))
            start = value
        previous = value
    found.append((start, previous))
    return found


def _pill(px, width: int, height: int) -> tuple[int, int, int, int] | None:
    """The pill's bounding box, found by its fill colour."""
    left = width - SEARCH_WIDTH
    top: tuple[int, int, int] | None = None
    for y in range(min(SEARCH_HEIGHT, height)):
        for x0, x1 in _runs([x for x in range(left, width) if _near(px[x, y])], gap=3):
            # `x0 == left` means the run is clipped by the search window: chrome, not pill.
            if x0 > left and x1 >= width - RIGHT_MARGIN and x1 - x0 >= MIN_PILL_WIDTH:
                top = (y, x0, x1)
                break
        if top:
            break
    if top is None:
        return None

    y0, x0, x1 = top
    # The first rows are the pill's rounded top; take the widest of them as its extent.
    for y in range(y0, min(y0 + 8, height)):
        for a, b in _runs([x for x in range(left, width) if _near(px[x, y])], gap=3):
            if a > left and b >= width - RIGHT_MARGIN:
                x0, x1 = min(x0, a), max(x1, b)

    # Walk down while the row is still mostly pill. Text breaks it up but not away.
    y1 = y0
    for y in range(y0, min(SEARCH_HEIGHT, height)):
        if sum(1 for x in range(x0, x1 + 1) if _near(px[x, y])) < 0.4 * (x1 - x0):
            break
        y1 = y
    return x0, y0, x1, y1


def find_bar(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Where the bar must go, or None if this image has no identity pill."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width < MIN_SCREENSHOT_WIDTH or height < MIN_SCREENSHOT_HEIGHT:
        return None
    px = rgb.load()

    box = _pill(px, width, height)
    if box is None:
        return None
    px0, py0, px1, py1 = box

    # Inset past the pill's own rounded corners: the dark header shows through them, and
    # those few pixels would otherwise read as the last glyph and displace the chevron.
    dark = [
        (x, y)
        for y in range(py0 + 3, py1 - 1)
        for x in range(px0 + INSET, px1 - INSET)
        if sum(px[x, y]) / 3 < TEXT_MAX_MEAN
    ]
    if not dark:
        return None
    text_rows = sorted({y for _, y in dark})
    columns = [
        run
        for run in _runs(sorted({x for x, _ in dark}), CHEVRON_MIN_GAP)
        if run[1] - run[0] >= MIN_GLYPH_WIDTH
    ]
    if not columns:
        return None

    # Drop a narrow trailing run: that is the chevron, and it stays.
    if len(columns) > 1 and columns[-1][1] - columns[-1][0] <= CHEVRON_MAX_WIDTH:
        chevron_x0 = columns[-1][0]
        columns = columns[:-1]
    else:
        chevron_x0 = px1

    return (
        px0 + 5,
        max(py0 + 2, text_rows[0] - 10),
        min(columns[-1][1] + 9, chevron_x0 - 7),
        min(py1 - 1, text_rows[-1] + 3),
    )


def draw_bar(image: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Draw the anti-aliased rounded bar in place, preserving the image's mode."""
    x0, y0, x1, y1 = box
    size = (x1 - x0, y1 - y0)
    radius = max(4, min(16, size[1] // 2 - 2))
    big = (size[0] * SCALE, size[1] * SCALE)
    patch = Image.new(image.mode, big, BAR_FILL + (255,) * (len(image.getbands()) - 3))
    mask = Image.new("L", big, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big[0] - 1, big[1] - 1), radius * SCALE, fill=255)
    image.paste(patch.resize(size, Image.LANCZOS), (x0, y0), mask.resize(size, Image.LANCZOS))


def main(argv: list[str]) -> int:
    check = "--check" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    if not paths:
        paths = sorted((Path(__file__).resolve().parents[1] / "images").glob("*.png"))
    done, skipped = [], []

    for path in sorted(paths):
        image = Image.open(path)
        box = find_bar(image)
        if box is None:
            skipped.append(path.name)
            continue
        if not check:
            draw_bar(image, box)
            image.save(path)
        done.append((path.name, box))

    if check:
        print(f"  {len(done)} unredacted, {len(skipped)} with no identity pill")
        for name, _ in done:
            print(f"  {name}: the account id is legible in this screenshot", file=sys.stderr)
        # A screenshot that still shows the pill has not been published yet. Refusing here is
        # the whole point: gitleaks reads text, and this is the same rule for pixels.
        return 1 if done else 0

    print(f"masked {len(done)}, no identity pill in {len(skipped)}")
    for name, box in done:
        print(f"  {name:32} {box}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
