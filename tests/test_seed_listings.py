"""scripts/seed_listings.py spends the payer wallet, so its plan must be exact
and its route list must match what the node actually sells."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP = REPO_ROOT / "wcag-audit-engine" / "app"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = _load("seed_listings_under_test", REPO_ROOT / "scripts" / "seed_listings.py")


def test_only_selects_tiers_and_empty_selects_everything():
    assert SEED.select(SEED.ROUTES, "") == SEED.ROUTES
    chosen = SEED.select(SEED.ROUTES, "utility, premium")
    assert chosen and {tier for tier, _, _ in chosen} == {"utility", "premium"}


def test_plan_total_counts_only_routes_that_quoted_a_price():
    priced = [("utility", "/a", {}, 0.02), ("advanced", "/b", {}, 5.0), ("premium", "/c", {}, None)]
    assert SEED.plan_total(priced) == 5.02


def test_every_seeded_route_is_a_route_the_node_sells():
    """A path renamed in a catalog must not leave this script paying a 404."""
    workers = _load("seed_workers_catalog", APP / "workers" / "catalog.py")
    sold = {w.path for w in workers.CATALOG}
    unknown = [route for _, route, _ in SEED.ROUTES if route not in sold]
    assert not unknown, unknown


def test_every_worker_is_seeded_so_none_stays_unlisted():
    workers = _load("seed_workers_catalog_all", APP / "workers" / "catalog.py")
    seeded = {route for _, route, _ in SEED.ROUTES}
    missing = [w.path for w in workers.CATALOG if w.path not in seeded]
    assert not missing, missing


def test_seed_bodies_carry_every_required_worker_field():
    workers = _load("seed_workers_catalog_fields", APP / "workers" / "catalog.py")
    by_path = {w.path: w for w in workers.CATALOG}
    short = {
        route: sorted(set(by_path[route].input_schema.get("required") or []) - set(body))
        for _, route, body in SEED.ROUTES if route in by_path
    }
    short = {route: fields for route, fields in short.items() if fields}
    assert not short, short
