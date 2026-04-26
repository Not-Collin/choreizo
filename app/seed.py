"""Seed starter chores on first boot when the chores table is empty."""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chore

log = logging.getLogger("choreizo")

# (name, description, frequency_days, priority, estimated_minutes)
_STARTER_CHORES: list[tuple[str, str | None, int, int, int | None]] = [
    # ── Daily ──────────────────────────────────────────────────────────────
    ("Wash dishes / run dishwasher",    None,                                  1,   0, 15),
    ("Wipe kitchen counters",           None,                                  1,   0,  5),
    ("Sweep kitchen floor",             None,                                  1,   0, 10),

    # ── Every few days ─────────────────────────────────────────────────────
    ("Take out kitchen trash",          None,                                  3,   0,  5),
    ("Wipe stovetop",                   None,                                  3,   0, 10),

    # ── Weekly ─────────────────────────────────────────────────────────────
    ("Vacuum living room",              None,                                  7,   0, 20),
    ("Vacuum bedrooms",                 None,                                  7,   0, 20),
    ("Mop hard floors",                 None,                                  7,   0, 25),
    ("Change bed sheets",               None,                                  7,   0, 20),
    ("Do laundry",                      None,                                  7,   0, 60),
    ("Clean bathroom sink & mirror",    None,                                  7,   0, 15),
    ("Scrub toilet",                    None,                                  7,   0, 10),
    ("Clean shower / tub",              None,                                  7,   0, 20),
    ("Empty all trash cans",            None,                                  7,   0, 10),
    ("Take out recycling",              None,                                  7,   0,  5),
    ("Dust surfaces",                   "Shelves, TV stand, baseboards",       7,   0, 20),
    ("Wipe appliance exteriors",        "Fridge, microwave, dishwasher",       7,   0, 10),

    # ── Bi-weekly ──────────────────────────────────────────────────────────
    ("Vacuum upholstered furniture",    "Couch, chairs, cushions",            14,   0, 15),
    ("Wipe light switches & door handles", None,                              14,   0, 10),
    ("Clean microwave interior",        None,                                 14,   0, 10),
    ("Clean garbage disposal",          "Run ice cubes + salt or disposal cleaner", 14, 0, 5),

    # ── Monthly ────────────────────────────────────────────────────────────
    ("Deep clean refrigerator",         "Wipe shelves, drawers, door seals",  30,   0, 25),
    ("Clean oven",                      None,                                 30,   0, 30),
    ("Wipe cabinet fronts",             None,                                 30,   0, 20),
    ("Clean window sills & tracks",     None,                                 30,   0, 15),
    ("Wash windows",                    None,                                 30,   0, 30),
    ("Clean bathroom exhaust fan",      None,                                 30,   0, 10),
    ("Wash shower curtain / liner",     None,                                 30,   0, 10),
    ("Clean washing machine drum",      "Run a cleaning cycle",               30,   0, 15),
    ("Clean dryer lint trap & drum",    None,                                 30,   0, 10),
    ("Vacuum under furniture",          "Move couches, beds, etc.",           30,   0, 20),
    ("Wipe interior of trash cans",     None,                                 30,   0, 10),
    ("Organize junk drawer",            None,                                 30,   0, 15),

    # ── Seasonal (~quarterly) ──────────────────────────────────────────────
    ("Replace HVAC / furnace filter",   None,                                 90,   1, 15),
    ("Deep clean oven",                 "Self-clean cycle or manual scrub",   90,   0, 45),
    ("Wash comforter / duvet",          None,                                 90,   0, 30),
    ("Flip or rotate mattress",         None,                                 90,   0, 15),
    ("Clean dryer vent hose",           "Disconnect and clear lint buildup",  90,   1, 20),
    ("Vacuum refrigerator coils",       "Under or behind the fridge",         90,   0, 15),
    ("Organize pantry & cabinets",      None,                                 90,   0, 30),
    ("Test smoke & CO detectors",       "Replace batteries if needed",        90,   1, 10),
    ("Descale kettle / coffee maker",   None,                                 90,   0, 15),

    # ── Semi-annual ────────────────────────────────────────────────────────
    ("Clean gutters",                   None,                                180,   1, 60),
    ("Wash windows inside & out",       "Full exterior + interior pass",     180,   0, 60),
    ("Deep clean tile grout",           None,                                180,   0, 45),
    ("Check and restock first aid kit", None,                                180,   1, 10),
]


async def seed_chores(session: AsyncSession) -> None:
    try:
        count = (await session.execute(select(func.count()).select_from(Chore))).scalar_one()
    except Exception:
        return
    if count > 0:
        return
    for name, desc, freq, priority, est in _STARTER_CHORES:
        session.add(Chore(
            name=name,
            description=desc,
            frequency_days=freq,
            priority=priority,
            estimated_minutes=est,
            enabled=True,
        ))
    await session.commit()
    log.info("Seeded %d starter chores", len(_STARTER_CHORES))
