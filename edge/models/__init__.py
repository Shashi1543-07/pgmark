"""Release-bundled, locally loaded model assets.

Binary weights are intentionally gitignored. A field release must populate
this directory through the offline packaging workflow and verify hashes with
``python -m tools.verify_offline_release`` before installation.
"""
