"""Orchestration for disclosure-checked publication of external audience tables.

Lifecycle of a publication request::

    create (check runs)  -> awaiting_approval | blocked
    recheck              -> awaiting_approval | blocked      (after the catalog moved)
    approve              -> approved                          (research director only)
    reject               -> rejected                          (terminal)
    export               -> exported                          (terminal; registers the catalog)

The export is the only public artefact. It is generated strictly from the
approved plan: a cell the plan marks suppressed carries no value in the
export, and the document is asserted to contain no household/person
identifier before it is stored. Approval and export are serialised against
the catalog revision so a later table can never bypass an earlier one, and
withdrawn tables keep constraining future checks.
"""
from __future__ import annotations

import uuid
from typing import Any

from .analytics import sdc
from .db import Database
from .errors import conflict, invalid, not_found
from .jsonutil import canonical_hash, jb, loads
from .runs import fetch_run
from .timeutil import iso

WITHHELD_DERIVATIVES = [
    "mean_minutes_per_person",
    "mean_minutes_per_listener",
    "unweighted_persons",
    "listener_unweighted_n",
    "per-person weights and weight ranges",
    "any ratio whose numerator or denominator is not public",
]

_EXPORT_NOTES = [
    "Cells marked 'suppressed' carry no values anywhere in this document.",
    "Suppression follows a threshold rule on unweighted household counts plus secondary suppression; it is not differential privacy and no random noise was added.",
    "Totals are union counts over households and are not the sum of overlapping audience cells.",
    "Feasible ranges of suppressed cells and the blocking basis of refused releases are kept in the internal plan, not in this public file.",
]

_FORBIDDEN_KEY_FRAGMENTS = ("person_id", "household_id", "final_weight", "base_weight", "inclusion_probability")


# ---------------------------------------------------------------------------
# Snapshot and catalog loading
# ---------------------------------------------------------------------------
def _snapshot_persons(cur, run_id: str) -> list[sdc.SnapshotPerson]:
    cur.execute(
        """
        SELECT person_id, household_id, age_band, region, sex, final_weight
          FROM run_persons WHERE run_id=%s AND eligible ORDER BY person_id
        """,
        (run_id,),
    )
    rows = cur.fetchall()
    cur.execute(
        "SELECT person_id, program_key FROM run_person_program WHERE run_id=%s AND reached",
        (run_id,),
    )
    reached: dict[str, set[str]] = {}
    for r in cur.fetchall():
        reached.setdefault(r["person_id"], set()).add(r["program_key"])
    persons = []
    for r in rows:
        persons.append(
            sdc.SnapshotPerson(
                person_id=r["person_id"],
                household_id=r["household_id"],
                attrs={"age_band": r["age_band"], "region": r["region"], "sex": r["sex"]},
                weight=float(r["final_weight"]),
                reached=frozenset(reached.get(r["person_id"], set())),
            )
        )
    return persons


def _load_catalog(cur, run_id: str) -> dict[str, Any]:
    """Already-public tables on the same snapshot, including withdrawn ones."""
    cur.execute(
        """
        SELECT table_id, name, spec, public_cells, hidden_cells, status, catalog_revision
          FROM published_tables WHERE run_id=%s ORDER BY published_at, table_id
        """,
        (run_id,),
    )
    counts: dict[str, int] = {}
    weighted: dict[str, float] = {}
    cells: dict[str, sdc.CellDef] = {}
    hidden: dict[str, str] = {}
    tables = []
    for row in cur.fetchall():
        tables.append(
            {
                "table_id": str(row["table_id"]),
                "name": row["name"],
                "status": row["status"],
                "catalog_revision": row["catalog_revision"],
            }
        )
        for cell in loads(row["public_cells"]):
            if cell.get("status") != "published":
                continue
            cid = cell["cell_id"]
            count = int(cell["unweighted_households"])
            if cid in counts and counts[cid] != count:
                raise invalid(
                    "catalog_inconsistent",
                    "two catalog tables publish the same cell with different counts",
                    {"cell_id": cid},
                )
            counts[cid] = count
            weighted[cid] = float(cell["weighted_persons"])
            cells[cid] = sdc.make_cell(cell["dims"], cell["programs"], cell["kind"])
        for cell in loads(row["hidden_cells"]):
            cid = cell["cell_id"]
            if cid not in counts:
                hidden.setdefault(cid, cell["reason"])
                cells.setdefault(cid, sdc.make_cell(cell["dims"], cell["programs"], cell["kind"]))
    # A cell that any catalog table publishes is public, even if another
    # table suppressed the same predicate.
    hidden = {cid: reason for cid, reason in hidden.items() if cid not in counts}
    return {"counts": counts, "weighted": weighted, "cells": cells, "hidden": hidden, "tables": tables}


def _catalog_revision(cur) -> int:
    cur.execute("SELECT revision FROM catalog_state WHERE singleton")
    return int(cur.fetchone()["revision"])


# ---------------------------------------------------------------------------
# Check execution and plan rendering
# ---------------------------------------------------------------------------
def _execute_check(cur, run_id: str, spec: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    run = fetch_run(cur, run_id)
    if run["status"] != "published":
        raise invalid(
            "run_not_published",
            "disclosure-checked tables can only be requested for a published run snapshot",
            {"run_id": run_id, "run_status": run["status"]},
        )
    persons = _snapshot_persons(cur, run_id)
    if not persons:
        raise invalid("snapshot_empty", "the run snapshot has no eligible persons")

    cur.execute("SELECT program_key FROM run_program_estimates WHERE run_id=%s", (run_id,))
    valid_programs = {r["program_key"] for r in cur.fetchall()}
    for table in spec["tables"]:
        unknown = sorted(set(table["programs"]) - valid_programs)
        if unknown:
            raise invalid(
                "program_not_in_run",
                "table spec references programs that the run did not estimate",
                {"unknown_programs": unknown, "available": sorted(valid_programs)},
            )

    categories = {dim: sorted({p.attrs[dim] for p in persons}) for dim in sdc.DIMENSIONS}
    catalog = _load_catalog(cur, run_id)
    cells: dict[str, sdc.CellDef] = dict(catalog["cells"])
    for table in spec["tables"]:
        for cell in sdc.table_cell_defs(table, categories):
            cells.setdefault(cell.cell_id, cell)

    result = sdc.run_check(
        persons=persons,
        tables=spec["tables"],
        categories=categories,
        cells=cells,
        catalog_counts=catalog["counts"],
        catalog_hidden=catalog["hidden"],
        policy=sdc.Policy(
            min_unweighted_households=int(policy["min_unweighted_households"]),
            min_protection_width=int(policy["min_protection_width"]),
            enumeration_node_budget=int(policy["enumeration_node_budget"]),
        ),
    )

    snapshot = {
        "run_id": run_id,
        "eligible_persons": len(persons),
        "households": len({p.household_id for p in persons}),
    }
    base = {
        "snapshot": snapshot,
        "policy": policy,
        "policy_statement": sdc.POLICY_STATEMENT,
        "catalog_revision": _catalog_revision(cur),
        "catalog_tables": catalog["tables"],
    }
    if result["status"] != "ok":
        return {"blocking": {**base, **{k: v for k, v in result.items() if k != "status"}}}

    # Values of cells that stay public. Suppressed cells never get values
    # anywhere in the plan.
    published_values: dict[str, dict[str, Any]] = {}
    persons_by_id = persons
    for cid, decision in result["decisions"].items():
        if decision != "published":
            continue
        if cid in catalog["counts"]:
            published_values[cid] = {
                "unweighted_households": catalog["counts"][cid],
                "weighted_persons": catalog["weighted"][cid],
                "already_public_in_catalog": True,
            }
        else:
            count, weighted = sdc.cell_values(persons_by_id, result["atoms"], cells[cid])
            published_values[cid] = {
                "unweighted_households": count,
                "weighted_persons": weighted,
            }

    tables_rendered = []
    readability = {"cells_total": 0, "cells_published": 0, "suppressed_primary": 0, "suppressed_secondary": 0}
    for table_spec, display in zip(spec["tables"], result["display_cells"]):
        cells_out = []
        body_weighted_total = 0.0
        body_weighted_suppressed = 0.0
        stats = {"cells": 0, "published": 0, "suppressed_primary": 0, "suppressed_secondary": 0}
        for cell in display:
            cid = cell.cell_id
            decision = result["decisions"][cid]
            entry = {
                "cell_id": cid,
                "kind": cell.kind,
                "dims": cell.dims_dict,
                "programs": list(cell.programs),
                "decision": decision,
            }
            stats["cells"] += 1
            if decision == "published":
                values = published_values[cid]
                entry.update(values)
                stats["published"] += 1
                if cell.kind == "body":
                    body_weighted_total += values["weighted_persons"]
            else:
                rng = result["protected_ranges"][cid]
                entry["feasible_range"] = rng
                if cell.kind == "body":
                    w = sum(
                        p.weight
                        for p in persons_by_id
                        if sdc.person_matches(p, cell.dims, cell.programs)
                    )
                    body_weighted_suppressed += w
                    body_weighted_total += w
            cells_out.append(entry)
        stats["suppressed_primary"] = sum(1 for c in cells_out if c["decision"] == "suppressed_primary")
        stats["suppressed_secondary"] = sum(1 for c in cells_out if c["decision"] == "suppressed_secondary")
        stats["suppressed_catalog"] = sum(1 for c in cells_out if c["decision"] == "suppressed_catalog")
        stats["suppressed_body_weighted_share"] = (
            round(body_weighted_suppressed / body_weighted_total, 3) if body_weighted_total else 0.0
        )
        for key in ("cells", "published", "suppressed_primary", "suppressed_secondary", "suppressed_catalog"):
            readability_key = "cells_total" if key == "cells" else ("cells_published" if key == "published" else key)
            readability.setdefault(readability_key, 0)
            readability[readability_key] += stats[key]
        tables_rendered.append(
            {
                "name": table_spec["name"],
                "dimensions": table_spec["dimensions"],
                "programs": sorted(table_spec["programs"]),
                "include_totals": bool(table_spec.get("include_totals", True)),
                "cells": cells_out,
                "stats": stats,
            }
        )

    plan = {
        **base,
        "status": "ok",
        "decisions": result["decisions"],
        "published_values": published_values,
        "protected_ranges": result["protected_ranges"],
        "catalog_protected_ranges": result["catalog_protected_ranges"],
        "tables": tables_rendered,
        "verification": result["verification"],
        "secondary_suppression": result["secondary_suppression"],
        "readability": readability,
        "withheld_derivatives": WITHHELD_DERIVATIVES,
    }
    return {"plan": plan}


# ---------------------------------------------------------------------------
# Request persistence helpers
# ---------------------------------------------------------------------------
def _insert_request(cur, run_id: str, spec: dict[str, Any], policy: dict[str, Any], outcome: dict[str, Any], actor: str) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    status = "awaiting_approval" if "plan" in outcome else "blocked"
    cur.execute(
        """
        INSERT INTO publication_requests
            (request_id, run_id, status, spec, spec_hash, policy, plan, blocking,
             catalog_revision_checked, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            request_id,
            run_id,
            status,
            jb(spec),
            canonical_hash({"run_id": run_id, **spec, "policy": policy}),
            jb(policy),
            jb(outcome["plan"]) if "plan" in outcome else None,
            jb(outcome["blocking"]) if "blocking" in outcome else None,
            outcome.get("plan", outcome.get("blocking", {})).get("catalog_revision"),
            actor,
        ),
    )
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
        (actor, "disclosure.request.create", "publication_request", request_id, jb({"run_id": run_id, "status": status})),
    )
    return _request_detail(cur, request_id)


def _request_detail(cur, request_id: str) -> dict[str, Any]:
    cur.execute("SELECT * FROM publication_requests WHERE request_id=%s", (request_id,))
    row = cur.fetchone()
    if row is None:
        raise not_found("publication_request_not_found", "publication request does not exist")
    return _serialize_request(row)


def _serialize_request(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": str(row["request_id"]),
        "run_id": str(row["run_id"]),
        "status": row["status"],
        "spec": loads(row["spec"]),
        "spec_hash": row["spec_hash"],
        "policy": loads(row["policy"]),
        "plan": loads(row["plan"]),
        "blocking": loads(row["blocking"]),
        "catalog_revision_checked": row["catalog_revision_checked"],
        "approved_by": row["approved_by"],
        "approved_at": iso(row["approved_at"]) if row["approved_at"] else None,
        "decided_note": row["decided_note"],
        "created_by": row["created_by"],
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }


# ---------------------------------------------------------------------------
# Public service operations
# ---------------------------------------------------------------------------
def create_request(db: Database, body: dict[str, Any], actor: str) -> dict[str, Any]:
    spec = {"tables": body["tables"]}
    policy = body["policy"]
    with db.scoped() as conn:
        cur = conn.cursor()
        outcome = _execute_check(cur, body["run_id"], spec, policy)
        detail = _insert_request(cur, body["run_id"], spec, policy, outcome, actor)
        conn.commit()
    return detail


def list_requests(db: Database, run_id: str | None) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if run_id:
        clauses.append("run_id=%s")
        params.append(run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT request_id, run_id, status, spec_hash, catalog_revision_checked,
                   approved_by, created_by, created_at, updated_at
              FROM publication_requests {where} ORDER BY created_at DESC
            """,
            params,
        )
        return [
            {
                **{k: r[k] for k in ("status", "spec_hash", "catalog_revision_checked", "approved_by", "created_by")},
                "request_id": str(r["request_id"]),
                "run_id": str(r["run_id"]),
                "created_at": iso(r["created_at"]),
                "updated_at": iso(r["updated_at"]),
            }
            for r in cur.fetchall()
        ]


def get_request(db: Database, request_id: str) -> dict[str, Any]:
    with db.scoped() as conn:
        return _request_detail(conn.cursor(), request_id)


def recheck_request(db: Database, request_id: str, actor: str) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM publication_requests WHERE request_id=%s FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if row is None:
            raise not_found("publication_request_not_found", "publication request does not exist")
        if row["status"] in ("exported", "rejected"):
            raise conflict(
                "request_terminal",
                f"a {row['status']} request cannot be rechecked; create a new request instead",
            )
        spec = loads(row["spec"])
        policy = loads(row["policy"])
        outcome = _execute_check(cur, str(row["run_id"]), spec, policy)
        status = "awaiting_approval" if "plan" in outcome else "blocked"
        cur.execute(
            """
            UPDATE publication_requests
               SET status=%s, plan=%s, blocking=%s, catalog_revision_checked=%s,
                   approved_by=NULL, approved_at=NULL, updated_at=now()
             WHERE request_id=%s
            """,
            (
                status,
                jb(outcome["plan"]) if "plan" in outcome else None,
                jb(outcome["blocking"]) if "blocking" in outcome else None,
                outcome.get("plan", outcome.get("blocking", {})).get("catalog_revision"),
                request_id,
            ),
        )
        cur.execute(
            "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
            (actor, "disclosure.request.recheck", "publication_request", request_id, jb({"status": status})),
        )
        conn.commit()
        return _request_detail(cur, request_id)


def _transition(cur, request_id: str, actor: str, note: str | None, *, approve: bool) -> dict[str, Any]:
    cur.execute("SELECT revision FROM catalog_state WHERE singleton FOR UPDATE")
    revision = int(cur.fetchone()["revision"])
    cur.execute("SELECT * FROM publication_requests WHERE request_id=%s FOR UPDATE", (request_id,))
    row = cur.fetchone()
    if row is None:
        raise not_found("publication_request_not_found", "publication request does not exist")
    if row["status"] != "awaiting_approval":
        raise conflict(
            "request_not_awaiting_approval",
            f"only an awaiting_approval request can be decided (current: {row['status']})",
        )
    if int(row["catalog_revision_checked"]) != revision:
        cur.execute(
            "UPDATE publication_requests SET status='stale', updated_at=now() WHERE request_id=%s",
            (request_id,),
        )
        conn = cur.connection
        conn.commit()
        raise conflict(
            "catalog_revision_changed",
            "the public catalog changed since this plan was checked; recheck the request before deciding",
            {"catalog_revision_checked": row["catalog_revision_checked"], "catalog_revision_current": revision},
        )
    new_status = "approved" if approve else "rejected"
    cur.execute(
        """
        UPDATE publication_requests
           SET status=%s, approved_by=%s, approved_at=now(), decided_note=%s, updated_at=now()
         WHERE request_id=%s
        """,
        (new_status, actor, note, request_id),
    )
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
        (actor, f"disclosure.request.{new_status}", "publication_request", request_id, jb({"note": note})),
    )
    cur.connection.commit()
    return _request_detail(cur, request_id)


def approve_request(db: Database, request_id: str, actor: str, note: str | None) -> dict[str, Any]:
    with db.scoped() as conn:
        return _transition(conn.cursor(), request_id, actor, note, approve=True)


def reject_request(db: Database, request_id: str, actor: str, note: str | None) -> dict[str, Any]:
    with db.scoped() as conn:
        return _transition(conn.cursor(), request_id, actor, note, approve=False)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _build_export_document(
    request_row: dict[str, Any],
    plan: dict[str, Any],
    catalog: dict[str, Any],
    actor: str,
    export_id: str,
    new_revision: int,
) -> dict[str, Any]:
    published_values = plan["published_values"]
    catalog_weighted = catalog["weighted"]
    catalog_counts = catalog["counts"]

    def col_total_weighted(program: str) -> float | None:
        cid = sdc.canonical_cell_id({}, (program,))
        if cid in published_values:
            return float(published_values[cid]["weighted_persons"])
        if cid in catalog_counts:
            return float(catalog_weighted[cid])
        return None

    tables_out = []
    for table in plan["tables"]:
        cells_out = []
        for cell in table["cells"]:
            base = {
                "cell_id": cell["cell_id"],
                "kind": cell["kind"],
                "dims": cell["dims"],
                "programs": cell["programs"],
            }
            if cell["decision"] == "published":
                values = published_values[cell["cell_id"]]
                entry = {
                    **base,
                    "status": "published",
                    "unweighted_households": values["unweighted_households"],
                    "weighted_persons": values["weighted_persons"],
                }
                if cell["kind"] == "body" and len(cell["programs"]) == 1:
                    denominator = col_total_weighted(cell["programs"][0])
                    if denominator:
                        entry["pct_of_program"] = round(values["weighted_persons"] / denominator, 6)
                cells_out.append(entry)
            else:
                cells_out.append({**base, "status": "suppressed", "marker": "S"})
        tables_out.append(
            {
                "name": table["name"],
                "dimensions": table["dimensions"],
                "programs": table["programs"],
                "cells": cells_out,
            }
        )
    return {
        "export_id": export_id,
        "request_id": str(request_row["request_id"]),
        "run_id": str(request_row["run_id"]),
        "catalog_revision": new_revision,
        "exported_by": actor,
        "policy": plan["policy"],
        "policy_statement": plan["policy_statement"],
        "tables": tables_out,
        "withheld_derivatives": WITHHELD_DERIVATIVES,
        "notes": _EXPORT_NOTES,
    }


def _assert_export_clean(document: dict[str, Any]) -> None:
    """Hard guard: the public file must not leak suppressed values or identifiers."""

    def walk(node: Any, path: str = "$") -> None:
        if isinstance(node, dict):
            if node.get("status") == "suppressed":
                extra = set(node) - {"cell_id", "kind", "dims", "programs", "status", "marker"}
                if extra:
                    raise invalid(
                        "export_leak_guard",
                        "a suppressed cell carries values in the export",
                        {"path": path, "unexpected_keys": sorted(extra)},
                    )
            for key, value in node.items():
                if any(fragment in key for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                    raise invalid(
                        "export_leak_guard",
                        "the export contains a forbidden identifier or weight key",
                        {"path": f"{path}.{key}", "key": key},
                    )
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(document)


def export_request(db: Database, request_id: str, actor: str) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT revision FROM catalog_state WHERE singleton FOR UPDATE")
        revision = int(cur.fetchone()["revision"])
        cur.execute("SELECT * FROM publication_requests WHERE request_id=%s FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if row is None:
            raise not_found("publication_request_not_found", "publication request does not exist")
        if row["status"] == "exported":
            raise conflict("request_already_exported", "this request was already exported")
        if row["status"] != "approved":
            raise conflict(
                "request_not_approved",
                f"only an approved request can be exported (current: {row['status']})",
            )
        if int(row["catalog_revision_checked"]) != revision:
            cur.execute(
                "UPDATE publication_requests SET status='stale', updated_at=now() WHERE request_id=%s",
                (request_id,),
            )
            conn.commit()
            raise conflict(
                "catalog_revision_changed",
                "the public catalog changed after approval; recheck and re-approve before exporting",
                {"catalog_revision_checked": row["catalog_revision_checked"], "catalog_revision_current": revision},
            )
        plan = loads(row["plan"])
        run_id = str(row["run_id"])
        catalog = _load_catalog(cur, run_id)
        export_id = str(uuid.uuid4())
        document = _build_export_document(row, plan, catalog, actor, export_id, revision + 1)
        _assert_export_clean(document)

        cur.execute(
            """
            INSERT INTO publication_exports (export_id, request_id, catalog_revision, document, exported_by)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (export_id, request_id, revision + 1, jb(document), actor),
        )
        spec = loads(row["spec"])
        for index, (table_spec, table_doc, table_plan) in enumerate(
            zip(spec["tables"], document["tables"], plan["tables"])
        ):
            hidden_cells = [
                {
                    "cell_id": cell["cell_id"],
                    "kind": cell["kind"],
                    "dims": cell["dims"],
                    "programs": cell["programs"],
                    "reason": cell["decision"],
                }
                for cell in table_plan["cells"]
                if cell["decision"] != "published"
            ]
            cur.execute(
                """
                INSERT INTO published_tables
                    (table_id, request_id, run_id, table_index, name, spec,
                     public_cells, hidden_cells, catalog_revision)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid.uuid4()),
                    request_id,
                    run_id,
                    index,
                    table_spec["name"],
                    jb(table_spec),
                    jb(table_doc["cells"]),
                    jb(hidden_cells),
                    revision + 1,
                ),
            )
        cur.execute("UPDATE catalog_state SET revision=%s WHERE singleton", (revision + 1,))
        cur.execute(
            "UPDATE publication_requests SET status='exported', updated_at=now() WHERE request_id=%s",
            (request_id,),
        )
        cur.execute(
            "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
            (
                actor,
                "disclosure.request.export",
                "publication_request",
                request_id,
                jb({"export_id": export_id, "catalog_revision": revision + 1}),
            ),
        )
        conn.commit()
    return {"export_id": export_id, "catalog_revision": revision + 1, "document": document}


def get_export(db: Database, request_id: str) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT export_id, catalog_revision, document, exported_by, exported_at FROM publication_exports WHERE request_id=%s",
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise not_found("export_not_found", "this request has no export")
        return {
            "export_id": str(row["export_id"]),
            "request_id": request_id,
            "catalog_revision": row["catalog_revision"],
            "exported_by": row["exported_by"],
            "exported_at": iso(row["exported_at"]),
            "document": loads(row["document"]),
        }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def list_catalog(db: Database, run_id: str | None) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if run_id:
        clauses.append("run_id=%s")
        params.append(run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT table_id, request_id, run_id, table_index, name, spec, public_cells,
                   hidden_cells, catalog_revision, status, published_at, withdrawn_at, withdrawn_by
              FROM published_tables {where} ORDER BY catalog_revision, table_index
            """,
            params,
        )
        out = []
        for r in cur.fetchall():
            public_cells = loads(r["public_cells"])
            out.append(
                {
                    "table_id": str(r["table_id"]),
                    "request_id": str(r["request_id"]),
                    "run_id": str(r["run_id"]),
                    "name": r["name"],
                    "spec": loads(r["spec"]),
                    "catalog_revision": r["catalog_revision"],
                    "status": r["status"],
                    "published_cell_count": sum(1 for c in public_cells if c["status"] == "published"),
                    "suppressed_cell_count": sum(1 for c in public_cells if c["status"] == "suppressed"),
                    "hidden_cells": loads(r["hidden_cells"]),
                    "published_at": iso(r["published_at"]),
                    "withdrawn_at": iso(r["withdrawn_at"]) if r["withdrawn_at"] else None,
                    "withdrawn_by": r["withdrawn_by"],
                }
            )
        return out


def withdraw_table(db: Database, table_id: str, actor: str, note: str | None) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM published_tables WHERE table_id=%s FOR UPDATE", (table_id,))
        row = cur.fetchone()
        if row is None:
            raise not_found("published_table_not_found", "published table does not exist")
        if row["status"] == "withdrawn":
            raise conflict("table_already_withdrawn", "this table is already withdrawn")
        cur.execute(
            "UPDATE published_tables SET status='withdrawn', withdrawn_at=now(), withdrawn_by=%s WHERE table_id=%s",
            (actor, table_id),
        )
        cur.execute(
            "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
            (
                actor,
                "disclosure.catalog.withdraw",
                "published_table",
                table_id,
                jb({"note": note, "still_constrains_future_checks": True}),
            ),
        )
        conn.commit()
    return {
        "table_id": table_id,
        "status": "withdrawn",
        "still_constrains_future_checks": True,
        "note": "withdrawal does not assume the audience forgot the table; its published cells remain constraints in every future disclosure check on the same snapshot",
    }
