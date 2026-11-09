#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CT_QR Banner (qr-bandeau) - permanent QR code strip for pack station computers.

- Starts automatically when the user session opens (GNOME autostart).
- Always visible, cannot be closed, never takes keyboard focus
  (the scanner keeps typing into the pack application).
- Reserves its screen space: maximized windows stop at the banner.
- 100% LOCAL: no network access. Codes come only from codes.txt and the
  images/ folder on the computer.
- Click a QR code to show it full size in the middle of the screen (30 s).

Ubuntu 20.04+ (GNOME, Xorg session), Python 3.8+, GTK 3.
No package installation required.
"""

import configparser
import ctypes
import ctypes.util
import fcntl
import json
import os
import platform
import re
import sys
import time
import unicodedata
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

VERSION = "1.3"
ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)
import qrcodegen  # noqa: E402  (fourni à côté du script, licence MIT)

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango  # noqa: E402

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11  # noqa: E402
except (ValueError, ImportError):
    GdkX11 = None

HOME = os.path.expanduser("~")
DOSSIER_CONF = os.environ.get("QR_BANDEAU_CONF") or os.path.join(HOME, ".config", "qr-bandeau")
DOSSIER_CACHE = os.path.join(HOME, ".cache", "qr-bandeau")
CODE_DEJA_LANCE = 3

# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------


def log(msg):
    try:
        os.makedirs(DOSSIER_CACHE, exist_ok=True)
        f = os.path.join(DOSSIER_CACHE, "bandeau.log")
        if os.path.exists(f) and os.path.getsize(f) > 500000:
            os.replace(f, f + ".1")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def norm(s):
    """minuscules, sans accents, sans espaces autour."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


OUI = {"oui", "o", "yes", "y", "true", "vrai", "1", "on"}
NON = {"non", "n", "no", "false", "faux", "0", "off"}
TOUTES = {"", "toutes", "tous", "toute", "all", "*"}

POSITIONS = {
    "bas": "bas", "bottom": "bas", "b": "bas",
    "haut": "haut", "top": "haut", "h": "haut",
    "gauche": "gauche", "left": "gauche", "g": "gauche",
    "droite": "droite", "right": "droite", "d": "droite",
}

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# English key -> (old French key, default). Old French config files keep working.
DEFAUTS = {
    "position": ("position", "bottom"),
    "thickness": ("epaisseur", "170"),
    "table": ("table", ""),
    "respect_panels": ("respecter_panneaux", "yes"),
    "show_header": ("afficher_entete", "yes"),
    "show_logo": ("afficher_logo", "yes"),
    "background_color": ("couleur_fond", "#1b2a4a"),
    "title_color": ("couleur_titre", "#ffffff"),
    "text_size": ("taille_texte", "12"),
    "reload_seconds": ("relire_secondes", "30"),
}


def chemin_config():
    for c in (os.path.join(DOSSIER_CONF, "config.ini"), os.path.join(ICI, "config.ini")):
        if os.path.isfile(c):
            return c
    return None


def lire_config():
    cp = configparser.ConfigParser(interpolation=None)
    chemin = chemin_config()
    if chemin:
        try:
            cp.read(chemin, encoding="utf-8-sig")  # -sig: accepts files saved with Windows Notepad
        except Exception as e:
            log("Unreadable config (%s): %s" % (chemin, e))
    sec = cp["banner"] if cp.has_section("banner") else (cp["bandeau"] if cp.has_section("bandeau") else {})

    def val(cle):
        fr, defaut = DEFAUTS[cle]
        v = sec.get(cle) if cle in sec else sec.get(fr, defaut)
        return (v if v is not None else defaut).strip()

    def entier(cle, mini, maxi):
        try:
            v = int(float(val(cle)))
        except Exception:
            v = int(DEFAUTS[cle][1])
        return max(mini, min(maxi, v))

    return {
        "chemin": chemin,
        "position": POSITIONS.get(norm(val("position")), "bas"),
        "epaisseur": entier("thickness", 80, 600),
        "table": val("table"),
        "respecter_panneaux": norm(val("respect_panels")) not in NON,
        "afficher_entete": norm(val("show_header")) not in NON,
        "afficher_logo": norm(val("show_logo")) not in NON,
        "couleur_fond": val("background_color") or "#1b2a4a",
        "couleur_titre": val("title_color") or "#ffffff",
        "taille_texte": entier("text_size", 8, 30),
        "relire": entier("reload_seconds", 5, 86400),
    }


# --------------------------------------------------------------------------
# Identité du poste
# --------------------------------------------------------------------------


def nom_pc():
    return platform.node()


def remplacer_variables(texte, ident):
    for k, v in ident.items():
        texte = texte.replace("{" + k + "}", v)
    return texte


def pour_cette_table(tables, ident):
    t = norm(tables)
    if t in TOUTES:
        return True
    moi = {norm(ident["table"]), norm(ident["hostname"])}
    num = re.sub(r"\D", "", ident["table"])
    for j in [x for x in re.split(r"[,;\s]+", t) if x]:
        if j in moi:
            return True
        m = re.match(r"^(\d+)-(\d+)$", j)
        if m and num and int(m.group(1)) <= int(num) <= int(m.group(2)):
            return True
    return False


# --------------------------------------------------------------------------
# Lecture des codes (fichiers locaux uniquement)
# --------------------------------------------------------------------------


def lire_codes_txt():
    chemin = os.path.join(DOSSIER_CONF, "codes.txt")
    if not os.path.isfile(chemin):
        chemin = os.path.join(ICI, "codes.txt")
    res = []
    if not os.path.isfile(chemin):
        return res
    with open(chemin, encoding="utf-8-sig") as f:
        for ligne in f:
            s = ligne.strip()
            if not s or s.startswith("#"):
                continue
            p = [x.strip() for x in s.split("|", 2)]
            if len(p) == 1:
                res.append({"libelle": "", "contenu": p[0], "tables": ""})
            else:
                res.append({"libelle": p[0], "contenu": p[1], "tables": p[2] if len(p) > 2 else ""})
    return [r for r in res if r["contenu"]]


EXT_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp")


def lire_images():
    dossier = os.path.join(DOSSIER_CONF, "images")
    if not os.path.isdir(dossier) or not any(n.lower().endswith(EXT_IMAGES) for n in os.listdir(dossier)):
        dossier = os.path.join(ICI, "images")  # images livrées avec le programme (version snap)
    res = []
    if not os.path.isdir(dossier):
        return res
    for nom in sorted(os.listdir(dossier), key=str.lower):
        if nom.lower().endswith(EXT_IMAGES):
            p = os.path.join(dossier, nom)
            lib = re.sub(r"^\d+[\s._-]+", "", os.path.splitext(nom)[0])  # "01_Login.png" -> "Login"
            res.append({"libelle": lib.replace("_", " "), "image": p, "tables": "",
                        "mtime": os.path.getmtime(p)})
    return res


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def pixbuf_qr(texte, cote):
    """QR net (pixels entiers, zone blanche de 2 modules) qui tient dans cote x cote."""
    qr = qrcodegen.QrCode.encode_text(texte, qrcodegen.QrCode.Ecc.MEDIUM)
    n, q = qr.get_size(), 2
    tot = n + 2 * q
    s = max(1, cote // tot)
    w = tot * s
    blanc, noir = b"\xff\xff\xff" * s, b"\x00\x00\x00" * s
    vide = (blanc * tot) * s
    marge = blanc * q
    lignes = []
    for y in range(-q, n + q):
        if y < 0 or y >= n:
            lignes.append(vide)
        else:
            row = marge + b"".join(noir if qr.get_module(x, y) else blanc for x in range(n)) + marge
            lignes.append(row * s)
    data = GLib.Bytes.new(b"".join(lignes))
    return GdkPixbuf.Pixbuf.new_from_bytes(data, GdkPixbuf.Colorspace.RGB, False, 8, w, w, w * 3)


def pixbuf_image(chemin, cote):
    pb = GdkPixbuf.Pixbuf.new_from_file(chemin)
    w, h = pb.get_width(), pb.get_height()
    r = min(cote / float(w), cote / float(h))
    if r >= 1:
        r = max(1, int(r))  # agrandissement en entier = QR toujours net
        interp = GdkPixbuf.InterpType.NEAREST
    else:
        interp = GdkPixbuf.InterpType.BILINEAR
    return pb.scale_simple(max(1, int(w * r)), max(1, int(h * r)), interp)


# --------------------------------------------------------------------------
# X11 : réservation de la place à l'écran (_NET_WM_STRUT_PARTIAL)
# --------------------------------------------------------------------------


class X11:
    def __init__(self, nom_display):
        self.nom = nom_display.encode() if nom_display else None
        L = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        L.XOpenDisplay.restype = ctypes.c_void_p
        L.XOpenDisplay.argtypes = [ctypes.c_char_p]
        L.XInternAtom.restype = ctypes.c_ulong
        L.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        L.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        L.XFlush.argtypes = [ctypes.c_void_p]
        L.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.L = L

    def cardinal(self, xid, propriete, valeurs):
        L = self.L
        d = L.XOpenDisplay(self.nom)
        if not d:
            raise RuntimeError("XOpenDisplay impossible")
        try:
            atome = L.XInternAtom(d, propriete.encode(), 0)
            tab = (ctypes.c_long * len(valeurs))(*[int(v) for v in valeurs])
            XA_CARDINAL, PROP_MODE_REPLACE = 6, 0
            L.XChangeProperty(d, xid, atome, XA_CARDINAL, 32, PROP_MODE_REPLACE,
                              ctypes.cast(tab, ctypes.c_void_p), len(valeurs))
            L.XFlush(d)
        finally:
            L.XCloseDisplay(d)


# --------------------------------------------------------------------------
# Le bandeau
# --------------------------------------------------------------------------


class Bandeau:
    MARGE = 6     # marge intérieure du bandeau
    PAD = 6       # marge intérieure d'une carte
    ECART = 10    # espace entre deux cartes

    def __init__(self):
        self.cfg = lire_config()
        self.mtime_cfg = self._mtime_cfg()
        table = self.cfg["table"] or nom_pc()
        self.ident = {"table": table, "hostname": nom_pc(), "poste": nom_pc()}
        self.horizontal = self.cfg["position"] in ("bas", "haut")
        self.signature = None
        self.loupe = None

        disp = Gdk.Display.get_default()
        self.x11 = None
        if GdkX11 is not None and isinstance(disp, GdkX11.X11Display):
            try:
                self.x11 = X11(disp.get_name())
            except Exception as e:
                log("libX11 unavailable: %s" % e)
        else:
            log("Not an X11 session (Wayland?): screen space cannot be reserved.")

        self._css()
        self.insets = self._insets_panneaux()
        self.win = w = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        w.set_name("bandeau")
        w.set_title("CT_QR Banner")
        w.set_type_hint(Gdk.WindowTypeHint.DOCK)
        w.set_decorated(False)
        w.set_resizable(False)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_keep_above(True)
        w.set_accept_focus(False)
        w.set_focus_on_map(False)
        w.stick()
        w.connect("delete-event", lambda *a: True)  # impossible à fermer

        ori = Gtk.Orientation.HORIZONTAL if self.horizontal else Gtk.Orientation.VERTICAL
        racine = Gtk.Box(orientation=ori, spacing=10)
        racine.set_border_width(self.MARGE)

        self.entete = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.entete.set_valign(Gtk.Align.CENTER)
        self.logo_h = 0
        logo = self._logo()
        if logo is not None:
            self.entete.pack_start(logo, False, False, 0)
        titre = self.ident["table"]
        if re.fullmatch(r"\d+", titre):
            titre = "Table " + titre
        self.lbl_titre = Gtk.Label(label=titre)
        self.lbl_titre.get_style_context().add_class("titre")
        self.lbl_titre.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_titre.set_max_width_chars(14)
        self.lbl_statut = Gtk.Label(label="loading…")
        self.lbl_statut.get_style_context().add_class("statut")
        self.lbl_statut.set_line_wrap(True)
        self.lbl_statut.set_max_width_chars(16)
        for l in (self.lbl_titre, self.lbl_statut):
            l.set_xalign(0)
            self.entete.pack_start(l, False, False, 0)
        if self.horizontal:
            self.entete.set_size_request(120, -1)
        if self.cfg["afficher_entete"]:
            racine.pack_start(self.entete, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        if self.horizontal:
            self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        else:
            self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.liste = Gtk.Box(orientation=ori, spacing=self.ECART)
        self.liste.set_valign(Gtk.Align.CENTER)
        self.liste.set_halign(Gtk.Align.START if self.horizontal else Gtk.Align.CENTER)
        self.scroll.add(self.liste)
        racine.pack_start(self.scroll, True, True, 0)
        w.add(racine)

        self.placer()
        w.show_all()
        self.placer()  # certains WM ignorent move() avant show
        self.poser_strut()

        scr = Gdk.Screen.get_default()
        scr.connect("size-changed", self._ecran_change)
        scr.connect("monitors-changed", self._ecran_change)

        self.rafraichir()
        GLib.timeout_add_seconds(self.cfg["relire"], self.rafraichir)
        GLib.timeout_add_seconds(5, self._surveiller_config)

    # ---- apparence -------------------------------------------------------

    def _logo(self):
        """Team logo on a white card at the top of the header (logo.png)."""
        if not self.cfg["afficher_logo"]:
            return None
        chemin = next((c for c in (os.path.join(DOSSIER_CONF, "logo.png"), os.path.join(ICI, "logo.png"))
                       if os.path.isfile(c)), None)
        if not chemin:
            return None
        e = self.cfg["epaisseur"]
        if self.horizontal:
            larg = 100
            haut_max = e - 2 * self.MARGE - 52   # leaves room for the table name + status
        else:
            larg = min(150, e - 2 * self.MARGE - 10)
            haut_max = 160
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(chemin)
            r = min(larg / float(pb.get_width()), max(20, haut_max) / float(pb.get_height()))
            pb = pb.scale_simple(max(1, int(pb.get_width() * r)), max(1, int(pb.get_height() * r)),
                                 GdkPixbuf.InterpType.HYPER)
        except Exception as ex:
            log("Logo not loaded: %s" % ex)
            return None
        self.logo_h = pb.get_height() + 10
        carte = Gtk.Box()
        carte.get_style_context().add_class("logo")
        carte.set_border_width(4)
        carte.set_halign(Gtk.Align.START if self.horizontal else Gtk.Align.CENTER)
        carte.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        return carte

    def _css(self):
        c = self.cfg
        css = """
        #bandeau, #bandeau scrolledwindow, #bandeau viewport { background-color: %(fond)s; }
        #bandeau .titre  { color: %(titre)s; font-weight: 800; font-size: 17px; }
        #bandeau .statut { color: alpha(%(titre)s, 0.75); font-size: 10px; }
        #bandeau .carte  { background-color: #ffffff; border-radius: 8px; }
        #bandeau .libelle { color: #111111; font-weight: 700; font-size: %(tt)spx; }
        #bandeau .vide   { color: %(titre)s; font-size: 14px; }
        #bandeau .logo   { background-color: #ffffff; border-radius: 8px; }
        #bandeau .erreur { color: #b00020; font-weight: 700; font-size: 11px; }
        #loupewin { background-color: %(fond)s; }
        .loupe { background-color: #ffffff; border-radius: 12px; }
        .loupe .grand { color: #111111; font-size: 30px; font-weight: 800; }
        .loupe .aide  { color: #666666; font-size: 13px; }
        """ % {"fond": c["couleur_fond"], "titre": c["couleur_titre"], "tt": c["taille_texte"]}
        prov = Gtk.CssProvider()
        try:
            prov.load_from_data(css.encode("utf-8"))
        except Exception as e:  # couleur mal tapée dans config.ini
            log("Invalid CSS (%s), using default colors" % e)
            prov.load_from_data(css.replace(c["couleur_fond"], "#1b2a4a")
                                .replace(c["couleur_titre"], "#ffffff").encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ---- position & place réservée ----------------------------------------

    def _moniteur(self):
        d = Gdk.Display.get_default()
        return d.get_primary_monitor() or d.get_monitor(0)

    def _insets_panneaux(self):
        """Place déjà prise par les autres panneaux (barre Ubuntu, dock, panneau DataMatrix…).
        Mesurée une seule fois au démarrage, avant que le bandeau réserve la sienne."""
        if not self.cfg["respecter_panneaux"]:
            return (0, 0, 0, 0)
        m = self._moniteur()
        g, wa = m.get_geometry(), m.get_workarea()
        ins = (max(0, wa.x - g.x), max(0, wa.y - g.y),
               max(0, (g.x + g.width) - (wa.x + wa.width)),
               max(0, (g.y + g.height) - (wa.y + wa.height)))
        log("Space already used by other panels (l,t,r,b) = %s" % (ins,))
        return ins

    def geometrie(self):
        g = self._moniteur().get_geometry()
        il, it, ir, ib = self.insets
        zx, zy, zw, zh = g.x + il, g.y + it, g.width - il - ir, g.height - it - ib
        e = self.cfg["epaisseur"]
        p = self.cfg["position"]
        if p == "bas":
            return zx, zy + zh - e, zw, e
        if p == "haut":
            return zx, zy, zw, e
        if p == "gauche":
            return zx, zy, e, zh
        return zx + zw - e, zy, e, zh

    def placer(self):
        x, y, w, h = self.geo = self.geometrie()
        self.win.set_size_request(w, h)
        self.win.resize(w, h)
        self.win.move(x, y)

    def poser_strut(self):
        if not self.x11 or not self.win.get_window():
            return
        x, y, w, h = self.geo
        s = self._moniteur().get_scale_factor()
        scr = Gdk.Screen.get_default()
        sw, sh = scr.get_width() * s, scr.get_height() * s
        X, Y, W, H = x * s, y * s, w * s, h * s
        p = [0] * 12
        pos = self.cfg["position"]
        if pos == "bas":
            p[3], p[10], p[11] = sh - Y, X, X + W - 1
        elif pos == "haut":
            p[2], p[8], p[9] = Y + H, X, X + W - 1
        elif pos == "gauche":
            p[0], p[4], p[5] = X + W, Y, Y + H - 1
        else:
            p[1], p[6], p[7] = sw - X, Y, Y + H - 1
        try:
            xid = self.win.get_window().get_xid()
            self.x11.cardinal(xid, "_NET_WM_STRUT_PARTIAL", p)
            self.x11.cardinal(xid, "_NET_WM_STRUT", p[:4])
            log("Screen space reserved: %s" % p)
        except Exception as e:
            log("Could not reserve screen space: %s" % e)

    def _ecran_change(self, *a):
        GLib.timeout_add(800, self._replacer)

    def _replacer(self):
        self.placer()
        self.poser_strut()
        self.signature = None  # force la reconstruction (taille dispo changée)
        self.rafraichir()
        return False

    # ---- données -----------------------------------------------------------

    def _mtime_cfg(self):
        c = chemin_config()
        try:
            return os.path.getmtime(c) if c else 0
        except OSError:
            return 0

    def _surveiller_config(self):
        if self._mtime_cfg() != self.mtime_cfg:
            log("config.ini changed: restarting")
            relancer()
            return False
        return True

    def rafraichir(self):
        """Relit codes.txt et images/ (aucun accès réseau)."""
        try:
            codes = lire_codes_txt() + lire_images()
            statut, alerte = "local codes · " + time.strftime("%H:%M"), False
        except Exception as e:
            log("Unreadable local codes: %s" % e)
            codes, statut, alerte = [], "codes.txt unreadable", True
        self._appliquer(codes, statut, alerte)
        return True

    def _appliquer(self, codes, statut, alerte):
        self.lbl_statut.set_text(("⚠ " if alerte else "") + statut)
        retenus = []
        for c in codes:
            if not pour_cette_table(c.get("tables", ""), self.ident):
                continue
            c = dict(c)
            c["libelle"] = remplacer_variables(c.get("libelle", ""), self.ident)
            if "contenu" in c:
                c["contenu"] = remplacer_variables(c["contenu"], self.ident)
            retenus.append(c)
        sig = json.dumps([retenus, self.geo], sort_keys=True, ensure_ascii=False)
        if sig != self.signature:
            self.signature = sig
            self.construire(retenus)
        return False

    # ---- affichage ---------------------------------------------------------

    def _cote(self, n):
        e, LH = self.cfg["epaisseur"], int(self.cfg["taille_texte"] * 1.6) + 4
        x, y, w, h = self.geo
        ent = 130 if self.cfg["afficher_entete"] else 0
        if self.horizontal:
            maxi = e - 2 * self.MARGE - 2 * self.PAD - LH
            dispo = w - 2 * self.MARGE - ent
            fit = dispo // max(1, n) - 2 * self.PAD - self.ECART
        else:
            maxi = e - 2 * self.MARGE - 2 * self.PAD
            dispo = h - 2 * self.MARGE - ((70 + self.logo_h) if self.cfg["afficher_entete"] else 0)
            fit = dispo // max(1, n) - 2 * self.PAD - self.ECART - LH
        return max(60, min(maxi, fit))

    def construire(self, codes):
        for enfant in self.liste.get_children():
            enfant.destroy()
        if not codes:
            l = Gtk.Label(label="No QR code for this station.\nAdd lines to codes.txt or images to images/")
            l.get_style_context().add_class("vide")
            self.liste.pack_start(l, False, False, 0)
            self.liste.show_all()
            return
        cote = self._cote(len(codes))
        for c in codes:
            self.liste.pack_start(self._carte(c, cote), False, False, 0)
        self.liste.show_all()
        log("%d code(s) displayed, QR size %d px" % (len(codes), cote))

    def _image(self, c, cote):
        if "image" in c:
            return Gtk.Image.new_from_pixbuf(pixbuf_image(c["image"], cote))
        return Gtk.Image.new_from_pixbuf(pixbuf_qr(c["contenu"], cote))

    def _carte(self, c, cote):
        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        boite.get_style_context().add_class("carte")
        boite.set_border_width(self.PAD)
        try:
            img = self._image(c, cote)
        except Exception as e:
            log("Cannot render code (%s): %s" % (c.get("libelle"), e))
            img = Gtk.Label(label="Code too long\nor unreadable image")
            img.get_style_context().add_class("erreur")
            img.set_size_request(cote, cote)
        boite.pack_start(img, False, False, 0)
        lib = c.get("libelle") or ""
        if lib:
            l = Gtk.Label(label=lib)
            l.get_style_context().add_class("libelle")
            l.set_ellipsize(Pango.EllipsizeMode.END)
            l.set_max_width_chars(1)
            l.set_size_request(cote, -1)
            l.set_tooltip_text(lib)
            boite.pack_start(l, False, False, 0)
        ev = Gtk.EventBox()
        ev.set_visible_window(False)
        ev.add(boite)
        ev.connect("button-press-event", lambda *a: self.ouvrir_loupe(c) or True)
        return ev

    def ouvrir_loupe(self, c):
        self.fermer_loupe()
        g = self._moniteur().get_geometry()
        cote = int(min(g.width, g.height) * 0.55)
        win = Gtk.Window(type=Gtk.WindowType.POPUP)  # ne vole pas le focus
        win.set_name("loupewin")
        win.set_border_width(8)
        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        boite.get_style_context().add_class("loupe")
        boite.set_border_width(24)
        try:
            boite.pack_start(self._image(c, cote), False, False, 0)
        except Exception:
            return
        if c.get("libelle"):
            l = Gtk.Label(label=c["libelle"])
            l.get_style_context().add_class("grand")
            boite.pack_start(l, False, False, 0)
        aide = Gtk.Label(label="Click to close · closes automatically in 30 s")
        aide.get_style_context().add_class("aide")
        boite.pack_start(aide, False, False, 0)
        ev = Gtk.EventBox()
        ev.add(boite)
        ev.connect("button-press-event", lambda *a: self.fermer_loupe() or True)
        win.add(ev)
        win.show_all()
        a = win.get_allocation()
        win.move(g.x + (g.width - a.width) // 2, g.y + (g.height - a.height) // 2)
        self.loupe = win
        self.loupe_timer = GLib.timeout_add_seconds(30, self._fin_loupe, win)
        return True

    def _fin_loupe(self, win):
        if self.loupe is win:
            self.fermer_loupe()
        return False

    def fermer_loupe(self):
        if self.loupe:
            self.loupe.destroy()
            self.loupe = None


# --------------------------------------------------------------------------


def relancer():
    """Relance le programme à l'identique (le verrou est libéré automatiquement)."""
    args = [a for a in sys.argv if a != "--delai"]
    if "--delai" in sys.argv:
        i = sys.argv.index("--delai")
        if i + 1 < len(sys.argv):
            args.remove(sys.argv[i + 1])
    os.execv(sys.executable, [sys.executable] + args)


def regler_table(valeur):
    """Écrit le numéro de table dans le config.ini de l'utilisateur (version snap : qr-bandeau.table 12)."""
    os.makedirs(DOSSIER_CONF, exist_ok=True)
    dst = os.path.join(DOSSIER_CONF, "config.ini")
    if not os.path.isfile(dst):
        src = os.path.join(ICI, "config.ini")
        texte = open(src, encoding="utf-8-sig").read() if os.path.isfile(src) else "[banner]\n"
    else:
        texte = open(dst, encoding="utf-8-sig").read()
    if re.search(r"(?m)^table *=.*$", texte):
        texte = re.sub(r"(?m)^table *=.*$", "table = " + valeur, texte)
    else:
        sec = "[banner]" if "[banner]" in texte else "[bandeau]"
        texte = texte.replace(sec, sec + "\ntable = " + valeur, 1)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(texte)
    print("Table set to:", valeur or "(computer name)")


def activer_demarrage_snap():
    """Version snap : déclare le démarrage automatique à l'ouverture de session."""
    if not os.environ.get("SNAP"):
        return
    nom = os.environ.get("SNAP_NAME", "ct-qr-banner")  # must match "autostart:" in snapcraft.yaml
    dossier = os.path.join(HOME, ".config", "autostart")
    f = os.path.join(dossier, nom + ".desktop")
    if os.path.isfile(f):
        return
    os.makedirs(dossier, exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("[Desktop Entry]\nType=Application\nName=CT_QR Banner\n"
                 "Exec=%s --delai 8\nX-GNOME-Autostart-enabled=true\n" % nom)
    log("Autostart enabled (snap)")


def une_seule_instance():
    os.makedirs(DOSSIER_CACHE, exist_ok=True)
    f = open(os.path.join(DOSSIER_CACHE, "bandeau.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def main():
    if "--version" in sys.argv:
        print("CT_QR Banner", VERSION)
        return 0
    if "--table" in sys.argv:
        i = sys.argv.index("--table")
        regler_table(sys.argv[i + 1].strip() if i + 1 < len(sys.argv) else "")
        return 0
    if "--delai" in sys.argv:  # au démarrage du PC : laisse GNOME et les autres panneaux s'afficher
        i = sys.argv.index("--delai")
        try:
            time.sleep(min(60, float(sys.argv[i + 1])))
        except (IndexError, ValueError):
            time.sleep(8)
    if "--verifier" in sys.argv:  # affiche ce que le bandeau va montrer, sans fenêtre
        cfg = lire_config()
        ident = {"table": cfg["table"] or nom_pc(), "hostname": nom_pc(), "poste": nom_pc()}
        print("CT_QR Banner", VERSION, "- 100% local, no network access")
        print("Config :", cfg["chemin"] or "(none, using defaults)")
        print("Table  :", ident["table"], "| Computer :", ident["hostname"])
        for c in lire_codes_txt() + lire_images():
            ok = pour_cette_table(c.get("tables", ""), ident)
            print(" [%s] %-25s %s" % ("X" if ok else " ", remplacer_variables(c.get("libelle", ""), ident)[:25],
                                      c.get("image") or remplacer_variables(c.get("contenu", ""), ident)))
        return 0

    verrou = une_seule_instance()
    if verrou is None:
        print("CT_QR Banner is already running.")
        return CODE_DEJA_LANCE
    if not Gtk.init_check(sys.argv)[0]:
        log("No display available (DISPLAY?)")
        return 1
    log("Starting CT_QR Banner %s" % VERSION)
    activer_demarrage_snap()
    Bandeau()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
