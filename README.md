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

Geodit is in the official QGIS plugin repository:

1. **Plugins → Manage and Install Plugins… → Settings**, tick **Show also experimental plugins** (Geodit is still
   marked experimental; QGIS hides it otherwise).
2. Under **All**, search for **Geodit** and click **Install**. QGIS offers later versions under *Upgradeable*.

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
3. **Pick a map project** and click **Open project**. The list shows the map projects you own, or where you are an
   Admin or Editor. Projects whose owner's plan has expired, and projects where the owner has turned the Map
   page off for your role, are hidden (a note under the list says how many and why). A project where **no survey
   area is assigned to you** is listed, marked in orange, but can't be opened — QGIS would have nothing to
   download. Assign yourself an area on the web Map page (Assign area), or ask the owner, then refresh the list.
   The project appears as a group **Geodit – <project>** with:
   - **Survey area (assigned to you)**: read-only; orange = pending, green = completed. The map moves to it the
     first time it appears;
   - one editable layer per server layer. Each layer shows up as soon as it has downloaded (the first download
     can take a while on large layers).
4. **Edit** with any QGIS tool, then **Save Layer Edits**. Saved edits upload a few seconds later. Changes made on
   the web or in the Android app are downloaded every minute. Change the interval in the panel, or click
   **Sync now**.

### Sync rules

- **Only saved edits are synced.** A layer with unsaved edits keeps uploading its saved changes, but skips
  downloads until you save.
- **Last write wins**, as in the Android app. If a feature is changed here and elsewhere between two syncs, the
  version that reaches the server last is kept. An attribute-only edit never overwrites someone else's geometry
  change.
- **Only features that touch your survey area sync** — at least part of the feature must be inside it, the same
  rule that decides which features download. Draw or move a feature entirely outside it and QGIS shows an error
  right away; the feature is held back and listed under *Needs attention* (double-click to zoom). This applies
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
- **If a feature you edited was deleted on the server**, your edited copy is kept in a
  **“<layer> — discarded edits”** layer. Copy it back into the layer to re-create it as a new feature.
- **Fields are managed on the server.** Fields you add locally are not uploaded. A synced field you delete is
  restored on the next sync.
- **If the project owner's plan expires**, uploads pause (downloads continue). Click **Sync now** after renewal.

### Known limitations

- **Stale local copies.** A feature moved out of your area on the server, and a layer cleared on the web, stay
  visible locally until you click **Full re-sync**.
- **No form responses from QGIS.** Features you create here have no form response, so they show as not yet
  surveyed on the web and in the app.
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
