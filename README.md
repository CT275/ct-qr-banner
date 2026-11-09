# CT_QR Banner — Snap package

Permanent QR code banner for warehouse pack stations (Ubuntu), packaged as a **snap** for the Ubuntu store.

```
qr-bandeau-snap/
├── snap/snapcraft.yaml      build recipe
├── snap/gui/                icon + application menu entry
└── src/app/
    ├── codes.txt            ⬅ THE QR CODES (edit this)
    ├── images/              ⬅ existing QR images (optional)
    ├── config.ini           settings (size, position…)
    └── qr_bandeau.py        the program
```

## What it does

- Starts automatically when the user session opens; cannot be closed.
- Reserves its screen space (standard X11 `_NET_WM_STRUT_PARTIAL`): maximized windows stop above it.
- Never takes keyboard focus; does not read keyboard or scanner input.
- **No network code.** Codes come only from files shipped in the snap (`app/codes.txt`, `app/images/`).
- Strict confinement, GNOME extension (GTK 3). Runs as the logged-in user.
- Third-party code: `qrcodegen.py` (Nayuki, MIT license).

## ⚠️ Choose the store before publishing

| Option | Who can install it |
|---|---|
| **Global Snap Store — public** | anyone on the internet (including the content of `codes.txt`) |
| **Global Snap Store — private** | only the publisher and invited collaborators (meant for development) |
| **Dedicated Snap Store** (company private store, Canonical service) | only the company's authorised devices ✅ recommended |

## 1. Build (Ubuntu dev machine with admin rights)

```
sudo snap install snapcraft --classic
sudo snap install lxd && sudo lxd init --auto
snapcraft
```
Output: `ct-qr-banner_1.3_amd64.snap`

## 2. Test locally

```
sudo snap install --dangerous ./ct-qr-banner_1.3_amd64.snap
ct-qr-banner.table 12        # station number (no sudo)
ct-qr-banner                 # starts the banner + enables autostart
```

## 3. Publish

```
snapcraft login                     # Ubuntu One account
snapcraft register ct-qr-banner        # change the name if taken (also in snapcraft.yaml)
snapcraft upload --release=stable ct-qr-banner_1.3_amd64.snap
```
Then set visibility (public / private / unlisted) in the snap's **Settings** on snapcraft.io.
For a Dedicated Snap Store, IT uploads to the company store instead.

## 4. Install on each pack station

```
sudo snap install ct-qr-banner         # admin rights required
ct-qr-banner.table 12                  # as the station user
ct-qr-banner                           # first run: enables autostart
```

## 5. Update the QR codes

1. Edit `src/app/codes.txt` (format: `Label | QR content | stations`) or `src/app/images/`.
2. Bump `version:` in `snap/snapcraft.yaml`.
3. Rebuild and upload. Stations update automatically (snap refresh).

## Uninstall

```
sudo snap remove ct-qr-banner
```

## Security notes

- The program itself has **no network code**. `snapd` (Ubuntu's package manager) contacts the store to install and update the snap.
- On the public store, `codes.txt` can be read by anyone who downloads the snap: **never put passwords or credentials in QR codes.**
