"""Fictional survey dataset used by the demo, the seed script and the tests.

Not derived from any real population register or listening panel.

Shape of the data
-----------------
* Survey period 2026-09-01 .. 2026-09-03, timezone Asia/Shanghai.
* A deterministic frame of households and persons. Diary return differs by
  age: young listeners return almost every diary, older listeners return
  fewer, so averaging returned diaries alone over-states youth programming.
* Recruitment records stratum, household, person, inclusion probability and
  the survey period. One person has an unknown inclusion probability.
* Diaries cover: cross-midnight entries, a resubmission replacing one whole
  survey day (with the original rows retained), a cross-day partial
  replacement (rejected), a duplicate same-station line (counted once), a
  cross-station conflict (left unconfirmed), a filled-no-listening diary, a
  diary with one unfilled hour, and diaries that never arrive.
* One household (H04) has four members and enters the bootstrap as one
  cluster.
"""
from __future__ import annotations

from datetime import date, timedelta

TZ = "Asia/Shanghai"
SURVEY_START = date(2026, 9, 1)
SURVEY_END = date(2026, 9, 3)
DAYS = [SURVEY_START + timedelta(days=i) for i in range(3)]

AGE_BANDS = ["18-24", "25-44", "45-64", "65+"]
REGIONS = ["north", "south"]
SEXES = ["F", "M"]
STRATA = ["urban", "rural"]

# Target population shares (age, region, sex) - used to build coherent margins.
TARGET_SHARES = {
    "age_band": {"18-24": 0.22, "25-44": 0.31, "45-64": 0.30, "65+": 0.17},
    "region": {"north": 0.55, "south": 0.45},
    "sex": {"F": 0.51, "M": 0.49},
}
POPULATION_TOTAL = 10_000

# How many diarists exist per age band (before special-case adjustments).
SAMPLE_COUNTS = {
    "18-24": 11,
    "25-44": 8,
    "45-64": 6,
    "65+": 5,
}

# Design-weight distortion per age band: the frame is *not* self-weighting.
# Multipliers below 1 make the group cheap to over-sample; raking then has to
# move weights materially, which is what we want to exercise.
DESIGN_DISTORTION = {
    "18-24": 0.55,
    "25-44": 1.00,
    "45-64": 1.35,
    "65+": 1.70,
}

PROGRAM_SPECS = [
    ("youth_drive", "Youth Drive", "RADIO-1", "07:00", "08:30"),
    ("midnight_news", "Midnight News", "RADIO-2", "23:30", "24:30"),
    ("afternoon_talk", "Afternoon Talk", "RADIO-3", "14:00", "15:00"),
]


def _at(day: date, hh: int, mm: int = 0) -> str:
    return f"{day.isoformat()}T{hh:02d}:{mm:02d}:00+08:00"


def _day_window(day: date) -> dict[str, str]:
    return {"start": _at(day, 0), "end": _at(day + timedelta(days=1), 0)}


def _all_days() -> list[dict[str, str]]:
    return [_day_window(d) for d in DAYS]


# ---------------------------------------------------------------------------
# Frame generation
# ---------------------------------------------------------------------------
def _frame_persons() -> list[dict]:
    """Deterministic list of persons with attributes and design distortion."""
    persons: list[dict] = []
    idx = 0
    for age in AGE_BANDS:
        for region in REGIONS:
            for sex in SEXES:
                for k in range(SAMPLE_COUNTS[age]):
                    idx += 1
                    # Stratum alternates so both strata hold every cell.
                    stratum = "urban" if (idx + (0 if region == "north" else 1)) % 3 else "rural"
                    if region == "south" and age == "65+":
                        stratum = "rural"
                    person = {
                        "person_id": f"F{idx:03d}",
                        "household_id": f"HF{idx:03d}",
                        "stratum": stratum,
                        "age_band": age,
                        "region": region,
                        "sex": sex,
                        "_distortion": DESIGN_DISTORTION[age],
                    }
                    persons.append(person)
    # Merge some consecutive urban singles into 2-3 person households so the
    # cluster structure is realistic and household_size > 1 is common.
    grouped: list[dict] = []
    pending: list[dict] = []
    household_no = 0
    for p in persons:
        pending.append(p)
        target_size = 1 + (household_no % 3)
        if len(pending) >= target_size:
            household_no += 1
            hid = f"HF{household_no:03d}"
            for member in pending:
                grouped.append({**member, "household_id": hid})
            pending = []
    if pending:
        household_no += 1
        hid = f"HF{household_no:03d}"
        for member in pending:
            grouped.append({**member, "household_id": hid})
    return grouped


# Special persons layered on top of the generated frame.
SPECIAL_PERSONS = [
    {"person_id": "S101", "household_id": "H01", "stratum": "urban", "age_band": "18-24", "region": "north", "sex": "F"},
    {"person_id": "S201", "household_id": "H02", "stratum": "urban", "age_band": "25-44", "region": "north", "sex": "M"},
    {"person_id": "S301", "household_id": "H03", "stratum": "urban", "age_band": "45-64", "region": "north", "sex": "F"},
    # Four-person household.
    {"person_id": "S401", "household_id": "H04", "stratum": "urban", "age_band": "18-24", "region": "north", "sex": "F"},
    {"person_id": "S402", "household_id": "H04", "stratum": "urban", "age_band": "25-44", "region": "north", "sex": "M"},
    {"person_id": "S403", "household_id": "H04", "stratum": "urban", "age_band": "45-64", "region": "south", "sex": "F"},
    {"person_id": "S404", "household_id": "H04", "stratum": "urban", "age_band": "65+", "region": "south", "sex": "M"},
    # Conflict person.
    {"person_id": "S501", "household_id": "H05", "stratum": "urban", "age_band": "25-44", "region": "north", "sex": "M"},
    # Missing-hour person.
    {"person_id": "S601", "household_id": "H06", "stratum": "urban", "age_band": "18-24", "region": "south", "sex": "F"},
    # Resubmission person.
    {"person_id": "S701", "household_id": "H07", "stratum": "urban", "age_band": "65+", "region": "south", "sex": "M"},
    # Thin rural persons.
    {"person_id": "S801", "household_id": "H08", "stratum": "rural", "age_band": "45-64", "region": "south", "sex": "F"},
    {"person_id": "S901", "household_id": "H09", "stratum": "rural", "age_band": "25-44", "region": "south", "sex": "M"},
    # Unknown inclusion probability.
    {"person_id": "S902", "household_id": "H10", "stratum": "rural", "age_band": "65+", "region": "south", "sex": "F"},
    # Never returns a diary.
    {"person_id": "S1101", "household_id": "H11", "stratum": "urban", "age_band": "25-44", "region": "north", "sex": "F"},
]

UNKNOWN_PROBABILITY_PERSON = "S902"
MISSING_DIARY_PERSONS = {"S1101", "F007", "F019", "F031"}
INCOMPLETE_DIARY_PERSONS = {"S601"}
NO_LISTENING_PERSONS = {"S404", "F004", "F016"}
# Special persons receive a hand-written diary below instead of the generic
# pattern, so they are skipped by the generic loop.
SPECIAL_DIARY_PERSONS = {
    "S101",
    "S201",
    "S301",
    "S401",
    "S402",
    "S403",
    "S404",
    "S501",
    "S601",
    "S701",
    "S801",
    "S901",
    "S902",
}


def frame_persons() -> list[dict]:
    out = []
    for p in _frame_persons():
        if p["person_id"] in MISSING_DIARY_PERSONS:
            p = {**p, "notes": "diary not returned at cutoff"}
        out.append(p)
    out.extend(SPECIAL_PERSONS)
    return out


def _age_sample_counts(persons: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in persons:
        counts[p["age_band"]] = counts.get(p["age_band"], 0) + 1
    return counts


def recruitment_batch(
    include_unknown_probability: bool = True,
    batch_ref: str = "frame-v1",
) -> dict:
    """Recruitment frame with known inclusion probabilities.

    The frame is a *sample* of a 10,000-person target population. Within an age
    band the design weight is ``N_age / n_age`` so the weights sum to the
    population total, but the sample composition does not match the region and
    sex margins - calibration has real work to do. Young people return diaries
    far more often, which is why unweighted diary averages over-state youth
    programming.
    """
    persons = frame_persons()
    if not include_unknown_probability:
        persons = [p for p in persons if p["person_id"] != UNKNOWN_PROBABILITY_PERSON]
    counts = _age_sample_counts(persons)
    age_weight = {
        age: TARGET_SHARES["age_band"][age] * POPULATION_TOTAL / counts[age]
        for age in counts
    }
    rows = []
    for p in persons:
        if p["person_id"] == UNKNOWN_PROBABILITY_PERSON:
            prob = None
        else:
            prob = round(1.0 / age_weight[p["age_band"]], 10)
            prob = min(prob, 1.0)
        rows.append(
            {
                "household_id": p["household_id"],
                "person_id": p["person_id"],
                "stratum": p["stratum"],
                "age_band": p["age_band"],
                "region": p["region"],
                "sex": p["sex"],
                "inclusion_probability": prob,
            }
        )
    return {
        "batch_ref": batch_ref,
        "survey_period": {"start": SURVEY_START.isoformat(), "end": SURVEY_END.isoformat()},
        "rows": rows,
    }


def design_weight_table(include_unknown_probability: bool = True) -> dict[str, float]:
    rows = recruitment_batch(include_unknown_probability)["rows"]
    return {
        r["person_id"]: (round(1.0 / r["inclusion_probability"], 6) if r["inclusion_probability"] else None)
        for r in rows
    }


def target_batch(batch_ref: str = "targets-v1", population_total: int = POPULATION_TOTAL) -> dict:
    margins = {}
    for dim, shares in TARGET_SHARES.items():
        cats = {}
        remaining = population_total
        items = list(shares.items())
        for i, (cat, share) in enumerate(items):
            if i == len(items) - 1:
                cats[cat] = remaining
            else:
                value = round(population_total * share)
                cats[cat] = value
                remaining -= value
        margins[dim] = cats
    return {"batch_ref": batch_ref, "population_total": population_total, "margins": margins}


def program_batch(batch_ref: str = "schedule-v1") -> dict:
    airings = []
    for day in DAYS:
        for key, title, station, start, end in PROGRAM_SPECS:
            airings.append(
                {
                    "program_key": key,
                    "title": title,
                    "station": station,
                    "air_date": day.isoformat(),
                    "start_local": start,
                    "end_local": end,
                }
            )
    return {"batch_ref": batch_ref, "timezone": TZ, "airings": airings}


# ---------------------------------------------------------------------------
# Diaries
# ---------------------------------------------------------------------------
def _sub(person, household, coverage, entries, revision=1, submitted_at="2026-09-04T08:00:00+08:00"):
    return {
        "person_id": person,
        "household_id": household,
        "revision": revision,
        "submitted_at": submitted_at,
        "coverage": coverage,
        "entries": entries,
    }


def _pattern_for(person: dict, index: int) -> list[dict]:
    """Deterministic listening pattern by age band."""
    age = person["age_band"]
    day1, day2, day3 = DAYS
    if age == "18-24":
        return [
            {"start": _at(day1, 7, 10 + (index % 3) * 5), "end": _at(day1, 8, 20), "station": "RADIO-1", "source_serial": f"{person['person_id']}-D1-001"},
            {"start": _at(day2, 7, 10 + (index % 3) * 5), "end": _at(day2, 8, 20), "station": "RADIO-1", "source_serial": f"{person['person_id']}-D2-001"},
            {"start": _at(day3, 7, 10 + (index % 3) * 5), "end": _at(day3, 8, 20), "station": "RADIO-1", "source_serial": f"{person['person_id']}-D3-001"},
        ]
    if age == "25-44":
        if index % 3 == 0:
            return [
                {"start": _at(day2, 14, 0), "end": _at(day2, 14, 45), "station": "RADIO-3", "source_serial": f"{person['person_id']}-D2-001"},
            ]
        return []
    if age == "45-64":
        if index % 2 == 0:
            return [
                {"start": _at(day1, 23, 30), "end": _at(day2, 0, 15), "station": "RADIO-2", "source_serial": f"{person['person_id']}-S1-001"},
            ]
        return [
            {"start": _at(day3, 14, 0), "end": _at(day3, 15, 0), "station": "RADIO-3", "source_serial": f"{person['person_id']}-D3-001"},
        ]
    # 65+
    if index % 4 == 0:
        return [
            {"start": _at(day3, 23, 30), "end": _at(SURVEY_END + timedelta(days=1), 0, 10), "station": "RADIO-2", "source_serial": f"{person['person_id']}-S3-001"},
        ]
    return []


def diary_submissions() -> list[dict]:
    persons = frame_persons()
    out: list[dict] = []
    index = 0
    for person in persons:
        pid = person["person_id"]
        index += 1
        if pid in MISSING_DIARY_PERSONS:
            continue
        if pid == UNKNOWN_PROBABILITY_PERSON:
            out.append(_sub(pid, person["household_id"], _all_days(), []))
            continue
        if pid in SPECIAL_DIARY_PERSONS:
            continue
        # Diary-return propensity differs by age: young listeners return a
        # complete diary almost always; older listeners return fewer and leave
        # gaps. This is what makes an unweighted diary average over-state youth
        # programming.
        age = person["age_band"]
        if age == "25-44" and index % 6 == 0:
            continue
        if age == "45-64" and index % 4 == 0:
            out.append(
                _sub(
                    pid,
                    person["household_id"],
                    [
                        _day_window(DAYS[0]),
                        {"start": _at(DAYS[1], 0), "end": _at(DAYS[1], 4)},
                        {"start": _at(DAYS[1], 20), "end": _at(DAYS[2], 0)},
                        _day_window(DAYS[2]),
                    ],
                    _pattern_for(person, index),
                )
            )
            continue
        if age == "65+" and index % 3 == 0:
            continue
        out.append(_sub(pid, person["household_id"], _all_days(), _pattern_for(person, index)))

    # ---- special diaries -------------------------------------------------
    out.extend(
        [
            # S101: heavy youth listener.
            _sub(
                "S101", "H01", _all_days(),
                [
                    {"start": _at(DAYS[0], 7, 40), "end": _at(DAYS[0], 8, 20), "station": "RADIO-1", "source_serial": "S101-D1-001"},
                    {"start": _at(DAYS[1], 7, 30), "end": _at(DAYS[1], 8, 20), "station": "RADIO-1", "source_serial": "S101-D2-001"},
                    {"start": _at(DAYS[2], 7, 10), "end": _at(DAYS[2], 8, 20), "station": "RADIO-1", "source_serial": "S101-D3-001"},
                ],
            ),
            # S601: complete day 1 and day 3, but day 2 leaves 13:00-14:00 unfilled.
            # Under the pre-declared full_coverage policy this person is excluded
            # from the analysis sample; the uncovered hour is never zero listening.
            _sub(
                "S601", "H06",
                [
                    _day_window(DAYS[0]),
                    {"start": _at(DAYS[1], 0), "end": _at(DAYS[1], 13)},
                    {"start": _at(DAYS[1], 14), "end": _at(DAYS[2], 0)},
                    _day_window(DAYS[2]),
                ],
                [
                    {"start": _at(DAYS[0], 7, 0), "end": _at(DAYS[0], 8, 30), "station": "RADIO-1", "source_serial": "S601-D1-001"},
                    {"start": _at(DAYS[2], 7, 0), "end": _at(DAYS[2], 8, 30), "station": "RADIO-1", "source_serial": "S601-D3-001"},
                ],
            ),
            # S201 duplicate same-station line.
            _sub(
                "S201", "H02", _all_days(),
                [
                    {"start": _at(DAYS[1], 14, 0), "end": _at(DAYS[1], 15, 0), "station": "RADIO-3", "source_serial": "S201-D2-001"},
                    {"start": _at(DAYS[1], 14, 0), "end": _at(DAYS[1], 14, 30), "station": "RADIO-3", "source_serial": "S201-D2-002"},
                ],
            ),
            # S301 cross-midnight 23:30 -> 00:20.
            _sub(
                "S301", "H03", _all_days(),
                [
                    {"start": _at(DAYS[0], 23, 30), "end": _at(DAYS[1], 0, 20), "station": "RADIO-2", "source_serial": "S301-S1-001"},
                ],
            ),
            # S401/S402 youth listening; S403 afternoon; S404 filled, no listening.
            _sub(
                "S401", "H04", _all_days(),
                [
                    {"start": _at(d, 7, 0), "end": _at(d, 8, 30), "station": "RADIO-1", "source_serial": f"S401-D{i+1}-001"}
                    for i, d in enumerate(DAYS)
                ],
            ),
            _sub(
                "S402", "H04", _all_days(),
                [
                    {"start": _at(d, 7, 0), "end": _at(d, 8, 30), "station": "RADIO-1", "source_serial": f"S402-D{i+1}-001"}
                    for i, d in enumerate(DAYS)
                ],
            ),
            _sub(
                "S403", "H04", _all_days(),
                [
                    {"start": _at(d, 14, 0), "end": _at(d, 15, 0), "station": "RADIO-3", "source_serial": f"S403-D{i+1}-001"}
                    for i, d in enumerate(DAYS)
                ],
            ),
            _sub("S404", "H04", _all_days(), []),
            # S501 cross-station conflict on day 2.
            _sub(
                "S501", "H05", _all_days(),
                [
                    {"start": _at(DAYS[1], 14, 0), "end": _at(DAYS[1], 14, 45), "station": "RADIO-1", "source_serial": "S501-D2-001"},
                    {"start": _at(DAYS[1], 14, 15), "end": _at(DAYS[1], 15, 0), "station": "RADIO-3", "source_serial": "S501-D2-002"},
                ],
            ),
            # S701 revision 1; revision 2 replaces survey day 2.
            _sub(
                "S701", "H07", _all_days(),
                [
                    {"start": _at(DAYS[1], 14, 0), "end": _at(DAYS[1], 15, 0), "station": "RADIO-3", "source_serial": "S701-D2-001"},
                    {"start": _at(DAYS[1], 23, 30), "end": _at(DAYS[2], 0, 30), "station": "RADIO-2", "source_serial": "S701-S1-001"},
                ],
            ),
            # S801 rural, sparse listening.
            _sub(
                "S801", "H08", _all_days(),
                [
                    {"start": _at(DAYS[0], 7, 0), "end": _at(DAYS[0], 7, 40), "station": "RADIO-1", "source_serial": "S801-D1-001"},
                ],
            ),
            # S901 rural cross-midnight on day 1 and day 3.
            _sub(
                "S901", "H09", _all_days(),
                [
                    {"start": _at(DAYS[0], 23, 30), "end": _at(DAYS[1], 0, 20), "station": "RADIO-2", "source_serial": "S901-S1-001"},
                    {"start": _at(DAYS[2], 23, 30), "end": _at(SURVEY_END + timedelta(days=1), 0, 10), "station": "RADIO-2", "source_serial": "S901-S3-001"},
                ],
            ),
        ]
    )
    return out


S701_RESUBMISSION = {
    "person_id": "S701",
    "household_id": "H07",
    "revision": 2,
    "submitted_at": "2026-09-04T09:00:00+08:00",
    "replacement_scope": {"type": "replace", "start": _at(DAYS[1], 0), "end": _at(DAYS[2], 0)},
    "coverage": [_day_window(DAYS[1])],
    "entries": [
        {"start": _at(DAYS[1], 23, 30), "end": _at(DAYS[2], 0, 10), "station": "RADIO-2", "source_serial": "S701-D2-003"},
    ],
}

S701_PARTIAL_REPLACEMENT = {
    "person_id": "S701",
    "household_id": "H07",
    "revision": 3,
    "submitted_at": "2026-09-04T10:00:00+08:00",
    "replacement_scope": {"type": "replace", "start": _at(DAYS[1], 22), "end": _at(DAYS[2], 6)},
    "coverage": [],
    "entries": [],
}

S801_LATE_DIARY = {
    "person_id": "S801",
    "household_id": "H08",
    "revision": 2,
    "submitted_at": "2026-09-05T09:00:00+08:00",
    "replacement_scope": {"type": "replace", "start": _at(DAYS[0], 0), "end": _at(SURVEY_END + timedelta(days=1), 0)},
    "coverage": _all_days(),
    "entries": [
        {"start": _at(DAYS[0], 7, 0), "end": _at(DAYS[0], 8, 30), "station": "RADIO-1", "source_serial": "S801-D1-009"},
    ],
}

# Infeasible target: the 18-24 margin exceeds upper-bound capacity.
BOUNDS_TARGET = {
    "batch_ref": "targets-bounds",
    "population_total": 1000,
    "margins": {
        "age_band": {"18-24": 900, "25-44": 40, "45-64": 40, "65+": 20},
        "region": {"north": 550, "south": 450},
        "sex": {"F": 510, "M": 490},
    },
}

# A category with no sample: "under-18" appears in the margin but not the frame.
EMPTY_CELL_TARGET = {
    "batch_ref": "targets-empty-cell",
    "population_total": 1000,
    "margins": {
        "age_band": {"18-24": 200, "25-44": 300, "45-64": 300, "65+": 150, "under-18": 50},
        "region": {"north": 550, "south": 450},
        "sex": {"F": 510, "M": 490},
    },
}

# A corrected population target: using it creates a new run.
CORRECTED_TARGET_BATCH = target_batch("targets-v2", population_total=10_100)

# Deliberately inconsistent margins (age sums to 1000, region to 900).
INCONSISTENT_TARGET = {
    "batch_ref": "targets-inconsistent",
    "population_total": 1000,
    "margins": {
        "age_band": {"18-24": 220, "25-44": 310, "45-64": 300, "65+": 170},
        "region": {"north": 500, "south": 400},
        "sex": {"F": 510, "M": 490},
    },
}

# A target batch that asks for a region margin the frame cannot supply at all.
# Every frame person is north or south, so adding an "east" category with a
# positive target is an empty calibration cell. Calibration must report it and
# refuse to publish, and must not add a synthetic respondent.
EMPTY_STRATUM_TARGET = {
    "batch_ref": "targets-empty-stratum",
    "population_total": 1000,
    "margins": {
        "age_band": {"18-24": 220, "25-44": 310, "45-64": 300, "65+": 170},
        "region": {"north": 450, "south": 450, "east": 100},
        "sex": {"F": 510, "M": 490},
    },
}


def stratum_empty_frame() -> dict:
    """Frame batch where one effective stratum has no eligible diarist.

    Two diarists who never return a diary are placed alone in ``rural-remote``.
    The stratum therefore has frame rows but zero eligible sample, which the
    build stage reports as
    ``strata_without_eligible_sample``. Calibration still succeeds because no
    target margin refers to the stratum itself, but the run is honest about the
    empty stratum instead of inventing a respondent.
    """
    # Built from the usable frame (everyone has a known probability), so
    # the empty stratum is the only anomaly this run demonstrates.
    batch = recruitment_batch(include_unknown_probability=False, batch_ref="frame-stratum-empty")
    # Two people who never return a diary sit alone in ``rural-remote``.
    for row in batch["rows"]:
        if row["person_id"] in {"F007", "F019"}:
            row["stratum"] = "rural-remote"
    return batch
