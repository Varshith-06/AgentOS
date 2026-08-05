"""Marks tests/ a regular package.

Not optional. `tests/run.py` invokes each file as `python -m unittest
tests.<name>`, and without this file `tests` is only a namespace portion —
which loses to any regular `tests` package that happens to sit in
site-packages, so the whole suite fails to import on a polluted environment.
"""
