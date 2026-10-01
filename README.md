# Geodit for QGIS

A QGIS plugin for editing a Geodit survey area on the desktop. It signs you in to Geodit and shows your
**map-based projects**. When you open one, it downloads the project's layers — only the features inside the
**survey area assigned to you** — into local GeoPackages. You edit them with any QGIS tool, and the plugin keeps
them **in sync with the server automatically**.

**Who can use it:** project **Owners, Admins and Editors**. Supervisors, Surveyors and Clients use the Geodit
Android app or the web; the server refuses their QGIS sign-in.

- QGIS 3.40 LTR or newer, including QGIS 4 (Qt 6).
- Works offline: unsynced edits stay on your computer and upload on the next sync.
- Uses the same server API and rules as the Geodit Android app.

## Install

Geodit is in the official QGIS plugin repository: open **Plugins → Manage and Install Plugins…**, search for
**Geodit** under **All** and click **Install**. QGIS offers later versions under *Upgradeable*.

New versions reach the official repository once a QGIS volunteer approves them, which can take a few days. To get
them as soon as they're released, also add the Geodit plugin repository: **Settings → Add…**, Name `Geodit`, URL
`https://arshdoda.github.io/geodit-qgis-plugin/plugins.xml`.

(Or **Install from ZIP** with `geodit.<version>.zip`.)

## Use

1. Click the **Geodit** toolbar button to open the Geodit panel.
2. **Sign in.**
   - Enter your username (or phone number) and password.
   - If your account uses two-factor authentication, you'll be asked for the 6-digit code (or a backup code).
   - "Stay signed in" keeps you signed in for up to 30 days. The sign-in is stored in QGIS's encrypted
     password store, which may ask for the QGIS master password.
3. **Click a map project** to open it. The list shows the map projects you own, or where you are an Admin or
   Editor. Projects whose owner's plan has expired, and projects where the owner has turned the Map page off for
   your role, are hidden (a note under the list says how many and why). A project where **no survey area is
   assigned to you** is listed, marked in orange, but can't be opened — QGIS would have nothing to download;
   clicking it says so. Assign yourself an area on the web Map page (Assign area), or ask the owner, then refresh
   the list. The project appears as a group **Geodit – <project>** with:
   - **Survey area (assigned to you)**: read-only; orange = pending, green = completed. The map moves to it the
     first time it appears;
   - one editable layer per server layer. Each layer shows up as soon as it has downloaded (the first download
     can take a while on large layers).
4. **Edit** with any QGIS tool, then **Save Layer Edits**. Saved edits upload a few seconds later. Changes made on
   the web or in the Android app are downloaded every minute. Change the interval in the panel, or click
   **Sync now**. With **Sync automatically** off, nothing uploads or downloads until you click **Sync now**.
   **‹ Projects** stops syncing the project and goes back to the list; its layers stay in your QGIS project, and
   syncing resumes when you open it again.
5. **Open a feature's data**, the survey response linked to it, as with the web map's **Data** tool. Click
   **Data** in the Geodit panel, then click a feature on a Geodit layer: its form opens in a separate
   **Feature form** window, which you can move, resize or maximise (it keeps its size). While Data is on, the panel
   lists which layer has which form, and clicking another feature switches the form to it; click **Data** again,
   or choose another map tool, to stop. If one feature is already selected when you turn it on, its form opens at
   once. Where features of layers with a form overlap, a click lists the features under it, the top one first, as
   the web map does: hover a row to see the feature on the map, and click it to open its form. As on the web, the window shows the feature, the response (click an id to copy it) and the layer, who
   surveyed the response and when it was last edited, who verified it, its status, and its answers with a tab per
   page. Close the window, or press Esc in it, to close the form.
   - **Edit** the answers and click **Save changes** (Ctrl+S) on any page; every page is checked first, and only
     the answers you changed are sent. The form stays on the page you were on. A status you pick (Pending /
     Approved / Rejected) applies at once.
   - A feature **without a response** opens a blank form, and submitting creates the response for it. A feature
     you drew in QGIS gets its form once it has uploaded.
   - Repeating pages, rules, calculated and layer defaults, uniqueness checks and photo, signature, PDF, audio
     and video answers work as on the web.
   - What you may see and change follows the project's **Web access** settings (Settings → Web access), exactly
     as on the web, for owners, admins and editors alike: the **Map** page decides whether you can edit and delete
     features in the layers, and the **Data** page and the answer-sheet switches decide what you can do in the
     form. Without Data *write* the form is view only.
   - Switching features, closing the window, leaving the project, signing out or quitting QGIS with unsaved
     answers asks whether to save or discard them.

### Sync rules

- **Only saved edits are synced.** A layer with unsaved edits keeps uploading its saved changes, but skips
  downloads until you save.
- **Last write wins**, as in the Android app. If a feature is changed here and elsewhere between two syncs, the
  version that reaches the server last is kept. An attribute-only edit never overwrites someone else's geometry
  change.
- **Only features that touch your survey area sync** — at least part of the feature must be inside it, the same
  rule that decides which features download. Draw or move a feature entirely outside it and QGIS shows an error
  right away; the feature is held back and listed under *Needs attention* (click it to zoom to it). This applies
  to everyone in QGIS — the team "off-site" permission only applies in the Android app.
- **If your survey area is unassigned while the project is open**, the project stays open with a warning: the
  layers become read-only, nothing new downloads, and changes you had already saved still upload. Once you go
  back to the project list it can't be opened again until an area is assigned to you.
- **What you may change follows the project's Page access** (Settings → Page access → Map, set by the
  project owner; the owner can always do everything):
  - no Map *write*: the layers are **view only** — they are read-only in QGIS and nothing is uploaded;
  - Map *write* but no Map *delete*: you can add and edit features, but **deleted features are restored** on
    the next sync (undo with Ctrl+Z before saving to keep them now).

  The plugin re-reads your permissions hourly and whenever the server refuses a change; a change the server
  refused stays on your computer and uploads once you are allowed. **⋯ → Discard unsynced changes** puts the
  layers back to the server's copy (your versions go to the “discarded edits” layer).
- **A layer removed on the server** stays in QGIS as “<layer> (removed on server)”, read-only and no longer
  synced, and is listed under *Needs attention*. When you no longer need it, click its row there: once you
  confirm, the layer leaves QGIS and its copy on this computer is deleted (export it first to keep its
  features). Removing it only from the Layers panel doesn't last — the next sync adds it back.
- **If a feature you edited was deleted on the server**, your edited copy is kept in a
  **“<layer> — discarded edits”** layer. Copy it back into the layer to re-create it as a new feature.
- **Fields are managed on the server.** Fields you add locally are not uploaded. A synced field you delete is
  restored on the next sync.
- **If the project owner's plan expires**, uploads pause (downloads continue). Click **Sync now** after renewal.

### Known limitations

- **Stale local copies.** A feature moved out of your area on the server, and a layer cleared on the web, stay
  visible locally until you click **Full re-sync**.
- **Feature form differences from the web.**
  - Audio and video can't be recorded or converted in QGIS: only files already in the upload format (AAC audio
    in M4A / MP4, H.264 video in MP4) can be picked.
  - Photos are converted by QGIS (HEIC only where QGIS can read it); the capture time and GPS position are kept
    only from JPEG photos.
  - Phone numbers are checked by calling code and number length (India's in full), a lighter check than the
    web's.
  - No comments thread, and no "Use my location" (a location answer can use **Pick on map** or the feature's
    location instead).
- **You need an assigned area.** Everyone — owners, admins and editors included — syncs only the survey-area
  polygons assigned to them, and can't open a project without one. Assign yourself an area on the web Map page
  (Assign area).
- **One project at a time.** Only one project syncs at a time per QGIS window.

Local data lives under QGIS's app-data folder: `geodit/<server>/<user>/<project>/` — `project.gpkg` (your
survey area) and one `layer_<id>.gpkg` per layer (the features you edit, plus hidden tables the sync keeps). Use a
local disk, not a network share. The Geodit tab of QGIS's log panel shows a line per sync that changed something
or was slow (time per step, requests made).

## About this repository

The source of each released version of Geodit for QGIS, published automatically when a version is released.
Install the plugin as described under *Install*, and report problems in
[Issues](https://github.com/arshdoda/geodit-qgis-plugin/issues). Pull requests aren't merged here directly: changes are made upstream and
appear with the next release.

## License

GPL-2.0-or-later (see `LICENSE`), as required for QGIS plugins.
