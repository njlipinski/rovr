from app.migrations import m001_add_flags, m002_status_renumber, m003_drop_assigned_to

# Ordered migration list — add new migrations at the end.
# This explicit import list is required for PyInstaller compatibility:
# dynamic module discovery (glob, pkgutil) cannot traverse a frozen archive.
MIGRATIONS = [m001_add_flags, m002_status_renumber, m003_drop_assigned_to]
