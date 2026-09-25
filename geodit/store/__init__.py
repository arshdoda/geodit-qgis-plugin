"""Local GeoPackage store.

All access goes through GDAL (``gdal.OpenEx`` / ``ExecuteSQL``), never Python's
``sqlite3``:

* GeoPackage R-tree triggers call ``ST_*`` SQL functions that only GDAL's
  connection registers — a raw sqlite3 write to a feature table fails.
* If Python and GDAL linked different SQLite copies (bundled builds), their
  POSIX locks would not see each other — a documented corruption cause.
"""
