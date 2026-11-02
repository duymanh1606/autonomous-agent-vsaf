"""Supervisor entry point: `python -m dashboard_api`.

The supervisor must never be started as `python -m dashboard_api.main`: that
runs main.py as `__main__`, and the route modules in `routes/` reach the shared
state through `from dashboard_api import main` - which then imports main.py a
second time, with its own account registry and stores. A workspace created
after that second import was invisible to every split-out route (products,
orders, chat media, translations, push): "unknown account" on a shop that
plainly exists. Importing main.py here keeps exactly one copy of it.
"""

from dashboard_api.main import main

main()
