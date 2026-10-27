"""Generate the PWA icons in public/icons/ (needs Pillow: pip install pillow).

Design: the desk's navy background with a rising price line in the accent
gradient (#5b8cff -> #2ee6a8). The maskable variant keeps the mark inside the
central 80% safe zone so Android's circle/squircle crops never clip it.
"""
import os

from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public", "icons")
BG_TOP, BG_BOTTOM = (20, 28, 54), (7, 10, 18)
A1, A3 = (91, 140, 255), (46, 230, 168)
POINTS = [(0.00, 0.78), (0.18, 0.62), (0.34, 0.70), (0.52, 0.42), (0.68, 0.50), (0.86, 0.24), (1.00, 0.30)]


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def render(size: int, *, maskable: bool, rounded: bool) -> Image.Image:
    s = size * 4  # supersample, then downscale for smooth edges
    bg = Image.new("RGB", (s, s))
    px = bg.load()
    for y in range(s):
        row = lerp(BG_TOP, BG_BOTTOM, y / (s - 1))
        for x in range(s):
            px[x, y] = row
    img = bg.convert("RGBA")

    inset = 0.24 if maskable else 0.17  # maskable: stay inside the 80% safe zone
    x0, x1 = s * inset, s * (1 - inset)
    y0, y1 = s * (inset + 0.04), s * (1 - inset - 0.02)
    pts = [(x0 + (x1 - x0) * u, y0 + (y1 - y0) * v) for u, v in POINTS]
    width = max(2, int(s * 0.055))

    # soft glow under the line
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=A3 + (150,), width=width * 3, joint="curve")
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(s * 0.03)))

    # gradient line: draw each segment in its own colour
    d = ImageDraw.Draw(img)
    colour = lambda i: lerp(A1, A3, i / (len(pts) - 1)) + (255,)  # noqa: E731
    r = width / 2
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        d.line([a, b], fill=colour(i + 0.5), width=width)
    for i, p in enumerate(pts):  # round joints and caps hide the seams between segments
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=colour(i))
    tip, r = pts[-1], width * 1.15
    d.ellipse([tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r], fill=A3 + (255,))

    if rounded:
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
        img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    targets = {
        "icon-192.png": (192, False, True),
        "icon-512.png": (512, False, True),
        "maskable-192.png": (192, True, False),
        "maskable-512.png": (512, True, False),
        "apple-touch-icon.png": (180, False, False),  # iOS rounds the corners itself
        "favicon-32.png": (32, False, True),
    }
    for name, (size, maskable, rounded) in targets.items():
        render(size, maskable=maskable, rounded=rounded).save(os.path.join(OUT, name), optimize=True)
        print("wrote", name)


if __name__ == "__main__":
    main()
