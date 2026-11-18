#!/usr/bin/env python3
"""Replace every Odoo logo picture in the Odoo source by the Resta one.

Run it again after each update of the Odoo source (the files come back with it):

    python3 tools/resta_logos.py odoo

Each picture keeps its name, size and format, so every screen, email, report, POS screen
and app icon that points at it shows Resta without touching Odoo's code. Pictures are made
from the Resta assets in bs_addons/resta_sidebar_theme/static/img. Needs Pillow (in Odoo's
requirements). Copyright and licence notices in Odoo's source files are not touched.
"""
import shutil
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "bs_addons" / "resta_sidebar_theme" / "static" / "img"
WARM = (249, 247, 243)


def _open(name):
    return Image.open(ASSETS / name).convert("RGBA")


def _fit(src, size, pad=0.06, bg=None, alpha=1.0):
    """`src` centred inside `size`, keeping its proportions."""
    w, h = size
    box_w, box_h = int(w * (1 - 2 * pad)), int(h * (1 - 2 * pad))
    img = src.copy()
    img.thumbnail((box_w, box_h), Image.LANCZOS)
    if alpha < 1:
        img.putalpha(img.getchannel("A").point(lambda a: int(a * alpha)))
    canvas = Image.new("RGBA", size, bg + (255,) if bg else (0, 0, 0, 0))
    canvas.alpha_composite(img, ((w - img.width) // 2, (h - img.height) // 2))
    return canvas


def _square(src, size):
    return src.resize((size, size), Image.LANCZOS)


def logo(alpha=1.0, bg=None, pad=0.06):
    return lambda size: _fit(_open("resta-logo.png"), size, pad=pad, bg=bg, alpha=alpha)


def logo_white(size):
    return _fit(_open("resta-logo-white.png"), size)


def mark(size):  # rounded orange square with the R
    return _square(_open("resta-avatar.png"), size[0])


def icon(size):  # full-bleed square (app icons: the phone rounds the corners itself)
    return _square(_open("resta-icon-512.png"), size[0])


def poster(size):
    return _fit(_open("resta-logo.png"), size, pad=0.3, bg=WARM)


PICTURES = {
    # web client, login, database manager, PWA, emails
    "addons/web/static/img/logo.png": logo(),
    "addons/web/static/img/logo2.png": logo(),
    "addons/web/static/img/nologo.png": logo(alpha=0.3),
    "addons/web/static/img/logo_inverse_white_206px.png": logo_white,
    "addons/web/static/img/odoo_logo_tiny.png": logo(pad=0.02),
    "addons/web/static/img/odoo-icon-192x192.png": icon,
    "addons/web/static/img/odoo-icon-512x512.png": icon,
    "addons/web/static/img/odoo-icon-ios.png": icon,
    "addons/web/static/img/favicon.ico": mark,
    "addons/web/static/img/enterprise_upgrade.jpg": poster,
    # point of sale (screens, customer display, browser tab)
    "addons/point_of_sale/static/src/img/logo.png": logo_white,
    "addons/point_of_sale/static/src/img/favicon.ico": mark,
    # discussion and the system bot
    "addons/mail/static/src/img/odoo_o.png": mark,
    "addons/mail/static/src/img/odoobot.png": mark,
    "addons/mail/static/src/img/odoobot_transparent.png": mark,
    # company and report placeholders
    "odoo/addons/base/static/img/logo_white.png": logo(bg=(255, 255, 255)),
    "odoo/addons/base/static/img/main_partner-image.png": logo(),
    "odoo/addons/base/static/img/demo_logo_report.png": logo(alpha=0.3),
}
SVGS = {
    "addons/web/static/img/odoo_logo.svg": "resta-wordmark-default.svg",
    "addons/web/static/img/odoo_logo_dark.svg": "resta-wordmark-white.svg",
    "addons/web/static/img/odoo-icon.svg": "resta-mark.svg",
    "addons/account/static/src/img/Odoo_logo_O.svg": "resta-mark.svg",  # centre of the payment QR code
}


def save(img, path, old):
    if path.suffix == ".ico":
        big = max(old.size[0], 64)
        sizes = sorted({16, 32, 48, 64, old.size[0]})
        img.resize((big, big), Image.LANCZOS).save(path, format="ICO", sizes=[(s, s) for s in sizes])
    elif path.suffix in (".jpg", ".jpeg"):
        img.convert("RGB").save(path, format="JPEG", quality=88, optimize=True)
    else:
        img = img if old.mode in ("RGBA", "LA", "P") else img.convert("RGB")
        img.save(path, format="PNG", optimize=True)


def main(odoo_dir):
    root = Path(odoo_dir)
    if not (root / "odoo" / "addons" / "base").is_dir():
        sys.exit("%s is not an Odoo source folder (odoo/addons/base is missing)" % root)
    done = missing = 0
    for rel, make in PICTURES.items():
        path = root / rel
        if not path.exists():
            print("missing (skipped):", rel)
            missing += 1
            continue
        with Image.open(path) as old:
            old.load()
        save(make(old.size), path, old)
        done += 1
        print("replaced:", rel, "%dx%d" % old.size)
    for rel, name in SVGS.items():
        path = root / rel
        if not path.exists():
            print("missing (skipped):", rel)
            missing += 1
            continue
        shutil.copyfile(ASSETS / name, path)
        done += 1
        print("replaced:", rel)
    print("%d pictures replaced, %d missing" % (done, missing))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else HERE.parent / "odoo")
