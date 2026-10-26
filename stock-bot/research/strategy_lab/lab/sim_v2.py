"""Build sim_2021_2026_v2 from the preserved v1 bundle. v1 stays read-only."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from lab.paths import SIM_V1, SIM_V2

V1_SNAPSHOT = "a0734bccd8f7e99db9fd3b5ac28b7bfd5b3cd5d3f6d1206fa97dd2e54b30a018"

# Distribution-day dollars per THNPF share. €9.00 is the Euronext reference price
# and €12.67 is the company's stated 2021-02-16 VWAP. Both use the company's 1.21 FX.
THNPF_GRANT_LOW = 10.89
THNPF_GRANT_HIGH = 15.33

SEARCH_BLOCK_REASONS = [
    "sim_2021_2026_v2 is not a representative study of the full requested universe.",
    "THNPF has no priced path after the 2021-02-16 distribution. The documented distribution-day range does not bound a later mark, so a book that still holds those units is inconclusive.",
    "PARA stays in the requested membership, and its Alpaca rows stay unusable. No Paramount Class B daily tape is attached. Massive daily aggregates are not in the current plan.",
    "HOLX and WBA contractual cash is booked. Each non-transferable right is kept and valued only inside its contractual range. A missing quote is not zero. A comparison that changes across that range is inconclusive.",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valuation() -> dict:
    return {
        "as_of": "2026-10-15",
        "rule": (
            "A missing quote is not a value of zero. Contractual cash is booked. "
            "The residual entitlement is kept. A comparison is inconclusive when it changes between the low and high bounds, "
            "or when a remaining entitlement has no terminal bound."
        ),
        "massive_daily_aggregates": {
            "PARA": "not_entitled",
            "VIAC": "not_entitled",
            "THNPF": "not_entitled",
            "note": "Checked against the current Massive plan. No other vendor tape was substituted.",
        },
        "entitlements": [
            {
                "parent": "FTI",
                "name": "THNPF",
                "session": "2021-02-16",
                "units_per_parent_share": 0.2,
                "terminal": "unpriced_path",
                "grant_value_low": THNPF_GRANT_LOW,
                "grant_value_high": THNPF_GRANT_HIGH,
                "grant_value_unit": "USD per THNPF share on the distribution day only",
                "fractional_cash": None,
                "facts": [
                    "One Technip Energies share per five FTI shares. NYSE ex-date 2021-02-16, record date 2021-02-17, payment date 2021-02-23.",
                    "Cash in lieu of fractions is set by the holder's intermediary. No single company-wide fractional rate is applied.",
                    "Euronext listing reference price for TE was €9.00 on 2021-02-16. TechnipFMC's FAQ, updated 2021-05-21, states the 2021-02-16 fair market value as the VWAP €12.67, equal to $15.33 at the company's 1.21 FX, and uses that dollar figure for the ADR.",
                    "A Reuters report of an intraday print at €11.83 sits inside that range. It is not a daily bar.",
                    "Alpaca SIP returned no THNPF bars. Later website prices were not copied in as a tape.",
                ],
            },
            {
                "parent": "HOLX",
                "name": "436CVR021",
                "session": "2026-04-07",
                "cash_per_share": 76.0,
                "units_per_parent_share": 1.0,
                "terminal": "bounded",
                "value_low": 0.0,
                "value_high": 3.0,
                "transferable": False,
                "payments_found": False,
                "facts": [
                    "The acquisition closed 2026-04-07. Consideration is $76.00 cash plus one non-transferable contingent value right.",
                    "The right pays at most $3.00 in total, in two milestones of up to $1.50 tied to Breast Health revenue, with a later catch-up. It pays no interest and cannot be sold or listed.",
                    "The FY2026 milestone period runs through 2026-09-26. No payment announcement was found as of 2026-10-15.",
                    "A later company filing said a Brevera recall makes the 2026 milestone harder and that a prior risk-adjusted figure of about $2.54 is likely too high. That figure is not a booked value.",
                ],
            },
            {
                "parent": "WBA",
                "name": "931CVR013",
                "session": "2025-08-29",
                "cash_per_share": 11.45,
                "units_per_parent_share": 1.0,
                "terminal": "bounded",
                "value_low": 0.0,
                "value_high": 3.0,
                "transferable": False,
                "payments_found": False,
                "facts": [
                    "Consideration is $11.45 cash plus one non-transferable Divested Asset Proceed right, CUSIP 931CVR013.",
                    "The right receives 70% of net proceeds from monetizing the VillageMD interests, capped at $3.00 per right. The proxy says no payment is assured.",
                    "The company closing date in the acquisition announcement is 2025-08-28. This bundle settles on the stored effective date 2025-08-29. No payment was found.",
                ],
            },
        ],
    }


def _limitations() -> dict:
    return {
        "representative_universe": False,
        "search_blocked": True,
        "para": {
            "symbol": "PARA",
            "in_requested_universe": True,
            "membership": ["2022-02-17", "2025-08-08"],
            "tradable": False,
            "reason": (
                "Alpaca's PARA series is not Paramount Class B. Those rows stay in the file with status unusable_wrong_issuer. "
                "Stored cash and stock terms for the same 2025-08-07 acquisition conflict, so neither is applied. "
                "The documented non-election default is one PSKY share. The cash election was prorated and is not assumed. "
                "No replacement Class B daily tape is attached. Until one is, this bundle is not a study of the full requested universe."
            ),
        },
        "kept_names": ["FTI", "HOLX", "WBA", "PARA"],
    }


def _overlay(accounting: dict) -> dict:
    blocked = {("FTI", "THNPF"), ("HOLX", "436CVR021"), ("WBA", "931CVR013")}

    def identity(row: dict) -> tuple[str | None, str | None]:
        parent = row.get("source_symbol") or row.get("acquiree_symbol")
        child = row.get("new_symbol") or row.get("acquirer_symbol")
        return parent, child

    accounting["reclassified_from_v1"] = [
        row for row in accounting.get("material_unresolved", []) if identity(row) in blocked
    ]
    accounting["material_unresolved"] = [
        row for row in accounting.get("material_unresolved", []) if identity(row) not in blocked
    ]
    accounting["unresolved"] = [row for row in accounting.get("unresolved", []) if identity(row) not in blocked]
    accounting["settlements"] = list(accounting.get("settlements", [])) + [
        {
            "session": "2026-04-07",
            "symbol": "HOLX",
            "acquirer": None,
            "cash_per_share": 76.0,
            "share_ratio": 0.0,
            "residual_name": "436CVR021",
            "residual_per_share": 1.0,
            "residual_low": 0.0,
            "residual_high": 3.0,
            "residual_terminal": "bounded",
            "consideration": "contractual cash plus a non-transferable right",
        },
        {
            "session": "2025-08-29",
            "symbol": "WBA",
            "acquirer": None,
            "cash_per_share": 11.45,
            "share_ratio": 0.0,
            "residual_name": "931CVR013",
            "residual_per_share": 1.0,
            "residual_low": 0.0,
            "residual_high": 3.0,
            "residual_terminal": "bounded",
            "consideration": "contractual cash plus a non-transferable right",
        },
    ]
    accounting["grants"] = [
        {
            "session": "2021-02-16",
            "parent": "FTI",
            "name": "THNPF",
            "ratio": 0.2,
            "grant_value_low": THNPF_GRANT_LOW,
            "grant_value_high": THNPF_GRANT_HIGH,
            "terminal": "unpriced_path",
            "fractional_cash": None,
        }
    ]
    accounting["simulation_ready"] = False
    accounting["search_blocked"] = True
    accounting["representative_universe"] = False
    return accounting


def build() -> dict:
    source = SIM_V1
    dest = SIM_V2
    if dest.resolve() == source.resolve():
        raise RuntimeError("v2 collided with v1")
    v1_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if v1_manifest.get("snapshot_id") != V1_SNAPSHOT:
        raise RuntimeError("v1 snapshot id changed before the v2 copy")
    v1_bars = _sha256(source / "bars.sqlite")
    v1_accounting = _sha256(source / "accounting.json")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    if _sha256(source / "accounting.json") != v1_accounting or _sha256(source / "bars.sqlite") != v1_bars:
        raise RuntimeError("copying v2 changed v1")
    accounting = _overlay(json.loads((dest / "accounting.json").read_text(encoding="utf-8")))
    valuation = _valuation()
    limitations = _limitations()
    (dest / "accounting.json").write_text(json.dumps(accounting, indent=2), encoding="utf-8")
    (dest / "valuation_bounds.json").write_text(json.dumps(valuation, indent=2), encoding="utf-8")
    (dest / "limitations.json").write_text(json.dumps(limitations, indent=2), encoding="utf-8")
    if _sha256(source / "accounting.json") != v1_accounting:
        raise RuntimeError("v2 overlay wrote the v1 accounting file")
    snapshot_id = hashlib.sha256(
        "\n".join(
            (
                v1_bars,
                _sha256(dest / "accounting.json"),
                _sha256(dest / "valuation_bounds.json"),
                _sha256(dest / "limitations.json"),
            )
        ).encode()
    ).hexdigest()
    manifest = {
        "source": "sim bundle derived from sim_2021_2026_v1",
        "parent_bundle": source.name,
        "parent_snapshot_id": V1_SNAPSHOT,
        "parent_bars_sha256": v1_bars,
        "v1_accounting_sha256": v1_accounting,
        "snapshot_id": snapshot_id,
        "bundle": dest.name,
        "feed": v1_manifest.get("feed"),
        "adjustment": v1_manifest.get("adjustment"),
        "request_start": "2021-01-01",
        "request_end": "2026-10-14",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "simulation_ready": False,
        "search_blocked": True,
        "representative_universe": False,
        "search_block_reasons": SEARCH_BLOCK_REASONS,
        "material_unresolved": len(accounting["material_unresolved"]),
        "credentials_printed": False,
        "market_data_db_written": False,
        "frozen_snapshot_written": False,
        "span_snapshot_written": False,
        "v1_snapshot_written": False,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
