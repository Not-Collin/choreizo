"""Seed starter chores on first boot when the chores table is empty."""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chore

log = logging.getLogger("choreizo")

# (name, description, frequency_days, priority, estimated_minutes, allowed_months)
# allowed_months=None means any month; otherwise a comma-separated string like "4,5,6,7,8,9,10"
_STARTER_CHORES: list[tuple[str, str | None, int, int, int | None, str | None]] = [
    # ── Daily ──────────────────────────────────────────────────────────────
    ("Wash dishes / run dishwasher",    None,                                  1,   0, 15, None),
    ("Wipe kitchen counters",           None,                                  1,   0,  5, None),
    ("Sweep kitchen floor",             None,                                  1,   0, 10, None),

    # ── Every few days ─────────────────────────────────────────────────────
    ("Take out kitchen trash",          None,                                  3,   0,  5, None),
    ("Wipe stovetop",                   None,                                  3,   0, 10, None),

    # ── Weekly ─────────────────────────────────────────────────────────────
    ("Vacuum living room",              None,                                  7,   0, 20, None),
    ("Vacuum bedrooms",                 None,                                  7,   0, 20, None),
    ("Mop hard floors",                 None,                                  7,   0, 25, None),
    ("Change bed sheets",               None,                                  7,   0, 20, None),
    ("Do laundry",                      None,                                  7,   0, 60, None),
    ("Clean bathroom sink & mirror",    None,                                  7,   0, 15, None),
    ("Scrub toilet",                    None,                                  7,   0, 10, None),
    ("Clean shower / tub",              None,                                  7,   0, 20, None),
    ("Empty all trash cans",            None,                                  7,   0, 10, None),
    ("Take out recycling",              None,                                  7,   0,  5, None),
    ("Dust surfaces",                   "Shelves, TV stand, baseboards",       7,   0, 20, None),
    ("Wipe appliance exteriors",        "Fridge, microwave, dishwasher",       7,   0, 10, None),

    # ── Bi-weekly ──────────────────────────────────────────────────────────
    ("Vacuum upholstered furniture",    "Couch, chairs, cushions",            14,   0, 15, None),
    ("Wipe light switches & door handles", None,                              14,   0, 10, None),
    ("Clean microwave interior",        None,                                 14,   0, 10, None),
    ("Clean garbage disposal",          "Run ice cubes + salt or disposal cleaner", 14, 0, 5, None),

    # ── Monthly ────────────────────────────────────────────────────────────
    ("Deep clean refrigerator",         "Wipe shelves, drawers, door seals",  30,   0, 25, None),
    ("Clean oven",                      None,                                 30,   0, 30, None),
    ("Wipe cabinet fronts",             None,                                 30,   0, 20, None),
    ("Clean window sills & tracks",     None,                                 30,   0, 15, None),
    ("Wash windows",                    None,                                 30,   0, 30, None),
    ("Clean bathroom exhaust fan",      None,                                 30,   0, 10, None),
    ("Wash shower curtain / liner",     None,                                 30,   0, 10, None),
    ("Clean washing machine drum",      "Run a cleaning cycle",               30,   0, 15, None),
    ("Clean dryer lint trap & drum",    None,                                 30,   0, 10, None),
    ("Vacuum under furniture",          "Move couches, beds, etc.",           30,   0, 20, None),
    ("Wipe interior of trash cans",     None,                                 30,   0, 10, None),
    ("Organize junk drawer",            None,                                 30,   0, 15, None),

    # ── Seasonal (~quarterly) ──────────────────────────────────────────────
    ("Replace HVAC / furnace filter",   None,                                 90,   1, 15, None),
    ("Deep clean oven",                 "Self-clean cycle or manual scrub",   90,   0, 45, None),
    ("Wash comforter / duvet",          None,                                 90,   0, 30, None),
    ("Flip or rotate mattress",         None,                                 90,   0, 15, None),
    ("Clean dryer vent hose",           "Disconnect and clear lint buildup",  90,   1, 20, None),
    ("Vacuum refrigerator coils",       "Under or behind the fridge",         90,   0, 15, None),
    ("Organize pantry & cabinets",      None,                                 90,   0, 30, None),
    ("Test smoke & CO detectors",       "Replace batteries if needed",        90,   1, 10, None),
    ("Descale kettle / coffee maker",   None,                                 90,   0, 15, None),

    # ── Semi-annual ────────────────────────────────────────────────────────
    ("Clean gutters",                   None,                                180,   1, 60, None),
    ("Wash windows inside & out",       "Full exterior + interior pass",     180,   0, 60, None),
    ("Deep clean tile grout",           None,                                180,   0, 45, None),
    ("Check and restock first aid kit", None,                                180,   1, 10, None),

    # ── Outdoor / seasonal (month-restricted) ──────────────────────────────
    ("Mow lawn",                        None,                                  7,   0, 45, "4,5,6,7,8,9,10"),
    ("Edge lawn & trim",                None,                                 14,   0, 30, "4,5,6,7,8,9,10"),
    ("Water lawn / garden",             None,                                  3,   0, 15, "5,6,7,8,9"),
    ("Weed garden beds",                None,                                 14,   0, 30, "4,5,6,7,8,9,10"),
    ("Prune shrubs & rose bushes",      None,                                 30,   0, 45, "3,4,5,9,10"),
    ("Fertilize lawn",                  None,                                 60,   0, 20, "4,5,9,10"),
    ("Rake leaves",                     None,                                  7,   0, 45, "10,11"),
    ("Mulch garden beds",               None,                                180,   0, 60, "4,10"),
    ("Clean & store outdoor furniture", "Before first frost",                365,   0, 30, "10,11"),
    ("Set out outdoor furniture",       "After last frost",                  365,   0, 20, "4,5"),
    ("Check / clean gutters after leaves fall", None,                        365,   1, 45, "11,12"),
    ("Winterize sprinkler / irrigation","Blow out lines before freeze",       365,   1, 30, "10,11"),
    ("Wash exterior of house / siding", "Pressure wash or scrub",            365,   0, 90, "5,6,9"),
    ("Clean outdoor grill",             None,                                 30,   0, 20, "5,6,7,8,9"),
    ("Shovel / salt walkways",          "After snowfall",                      1,   1, 20, "12,1,2,3"),
]


async def seed_chores(session: AsyncSession) -> None:
    try:
        count = (await session.execute(select(func.count()).select_from(Chore))).scalar_one()
    except Exception:
        return
    if count > 0:
        return
    for name, desc, freq, priority, est, months in _STARTER_CHORES:
        session.add(Chore(
            name=name,
            description=desc,
            frequency_days=freq,
            priority=priority,
            estimated_minutes=est,
            allowed_months=months,
            enabled=True,
        ))
    await session.commit()
    log.info("Seeded %d starter chores", len(_STARTER_CHORES))
