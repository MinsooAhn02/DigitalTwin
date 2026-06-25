@echo off
if not exist "node-link-data\nodelink.sqlite" (
    if exist "node-link-data\MOCT_NODE.shp" (
        echo [nodelink] Building DB - first run, ~2 min...
        call scripts\with_best_python.cmd -m pip install pyshp pyproj -q
        call scripts\with_best_python.cmd scripts\build_nodelink_db.py
    ) else (
        echo [nodelink] No shapefile - road info disabled
    )
) else (
    echo [nodelink] DB ready
)