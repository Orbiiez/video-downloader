"""Draw the app icon: a play triangle turned to point down onto a baseline (play + save), lime on ink.

Usage: python make_icon.py      -> assets/icon.ico (16-256 px) and assets/icon.png (256 px)
"""
import os

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
INK = (17, 17, 20, 255)
LIME = (212, 255, 79, 255)
S = 1024  # drawn large, then scaled down


def rounded(mask, radius):
    """Round the corners of a hard-edged mask: blur, then re-threshold."""
    return mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: 255 if v > 128 else 0)


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile).rounded_rectangle((40, 40, S - 40, S - 40), radius=230, fill=255)
    img.paste(INK, (0, 0), tile)

    mark = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mark)
    d.polygon([(262, 250), (762, 250), (512, 612)], fill=255)  # play triangle pointing down
    mark = rounded(mark, 38)
    img.paste(LIME, (0, 0), mark)

    # it lands on a video scrubber: played part bright, the rest dim, with a playhead knob
    track = Image.new("L", (S, S), 0)
    ImageDraw.Draw(track).rounded_rectangle((232, 712, 792, 764), radius=26, fill=255)
    img.paste(tuple(int(i * 0.3 + l * 0.7) for i, l in zip(LIME, INK)), (0, 0), track)  # dim lime, blended onto ink
    played = Image.new("L", (S, S), 0)
    pd = ImageDraw.Draw(played)
    pd.rounded_rectangle((232, 712, 600, 764), radius=26, fill=255)
    pd.ellipse((560, 680, 676, 796), fill=255)
    img.paste(LIME, (0, 0), played)
    return img


def main():
    img = draw()
    out = os.path.join(HERE, "assets")
    os.makedirs(out, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(out, "icon.png"))
    img.save(os.path.join(out, "icon.ico"), sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print("wrote assets/icon.ico and assets/icon.png")


if __name__ == "__main__":
    main()
