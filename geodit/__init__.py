"""Geodit for QGIS.

Sign in to Geodit, open a map project, edit the features in your assigned
survey area and keep them in sync with the server.
"""


def classFactory(iface):  # noqa: N802 - QGIS plugin entry point
    # Imported lazily so the pure-Python parts of the package (geodit.core,
    # geodit.store, geodit.sync.engine) import without a running QGIS.
    from .plugin import GeoditPlugin

    return GeoditPlugin(iface)
