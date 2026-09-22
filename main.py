from __future__ import annotations

"""
Hovedindgang til processen.

Queue-mode
----------

Kør med:

    uv run python main.py --queue

Process-mode
------------

Kør med:

    uv run python main.py

Manuel behandling
-----------------

behandel_page() kan markere et item til manuel behandling ved at sætte:

    data["box"]["Manuel behandling"] = True

Når dette felt er True, overskriver main.py ikke status med Completed.
Itemet opdateres i stedet med status "Manuel" og den state samt årsag,
som behandel_page() allerede har skrevet til work item-data.
"""

import asyncio
import logging
import os
import sys
from pprint import pprint
from typing import Any

from automation_server_client import (
    AutomationServer,
    WorkItemError,
    WorkItemStatus,
    Workqueue,
)
from behandel import behandel_page
from populate_queue import populate_queue
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import BrowserSession
from q_insubiz.utils import normalize_positive_id


# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s [%(levelname)s] "
        "%(name)s: %(message)s"
    ),
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("automation_server_client").setLevel(logging.WARNING)
logging.getLogger("debugpy").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# WORK ITEM-STATUS
# ------------------------------------------------------------

STATUS_COMPLETED = "Completed"
STATUS_CODE_COMPLETED = "Færdig"

STATUS_MANUEL = "Manuel"
STATUS_CODE_MANUEL = "Manuel"

BOX_MANUEL_BEHANDLING = "Manuel behandling"
BOX_MANUEL_AARSAG = "Manuel årsag"


# ------------------------------------------------------------
# QUEUE-ID
# ------------------------------------------------------------

def _hent_queue_id(
    *,
    workqueue: Workqueue,
) -> int:
    """
    Henter queue-id fra miljøet eller workqueue-objektet.

    Først anvendes miljøvariablen QUEUE_ID. Hvis den ikke findes,
    undersøges almindelige attributter på workqueue-objektet.
    """
    environment_value = os.getenv("QUEUE_ID")

    if environment_value is not None and environment_value.strip():
        return normalize_positive_id(
            name="QUEUE_ID",
            value=environment_value,
        )

    for attribute_name in (
        "queue_id",
        "id",
        "workqueue_id",
    ):
        value = getattr(
            workqueue,
            attribute_name,
            None,
        )

        if value is None:
            continue

        try:
            return normalize_positive_id(
                name=f"workqueue.{attribute_name}",
                value=value,
            )
        except (TypeError, ValueError):
            continue

    raise RuntimeError(
        "Queue-id kunne ikke findes. Angiv miljøvariablen "
        "QUEUE_ID eller anvend et workqueue-objekt med queue_id, "
        "id eller workqueue_id."
    )


# ------------------------------------------------------------
# WORK ITEM-HJÆLPERE
# ------------------------------------------------------------

def _hent_box(
    *,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Returnerer work itemets box som dictionary."""
    box = data.get("box")

    if box is None:
        box = {}
        data["box"] = box

    if not isinstance(box, dict):
        raise WorkItemError(
            "Work item-feltet box skal være en dictionary. "
            f"Modtog: {type(box).__name__}."
        )

    return box


def _er_manuel_behandling(
    *,
    data: dict[str, Any],
) -> bool:
    """Returnerer True, når behandel_page har valgt manuel behandling."""
    box = _hent_box(data=data)
    value = box.get(
        BOX_MANUEL_BEHANDLING,
        False,
    )

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().casefold() in {
            "true",
            "ja",
            "1",
            "manuel",
        }

    return bool(value)


def _hent_manuel_aarsag(
    *,
    data: dict[str, Any],
) -> str:
    """Henter årsagen til manuel behandling fra box."""
    box = _hent_box(data=data)
    value = box.get(BOX_MANUEL_AARSAG)

    if value is None:
        return "Manuel behandling"

    normalized_value = str(value).strip()
    return normalized_value or "Manuel behandling"


def _gem_completed_item(
    *,
    item: Any,
    data: dict[str, Any],
) -> None:
    """Gemmer et normalt færdigbehandlet item."""
    update_item_data(
        data,
        item=item,
        status=STATUS_COMPLETED,
        status_code=STATUS_CODE_COMPLETED,
        state=STATUS_COMPLETED,
    )

    item.update(data)
    item.complete(STATUS_COMPLETED)


def _gem_manuelt_item(
    *,
    item: Any,
    data: dict[str, Any],
) -> None:
    """
    Gemmer et item med status Manuel.

    behandel_page() har allerede skrevet den konkrete 1.0-state og
    årsagen til box. Main sikrer her, at status ikke overskrives med
    Completed.
    """
    manuel_aarsag = _hent_manuel_aarsag(
        data=data,
    )

    update_item_data(
        data,
        item=item,
        status=STATUS_MANUEL,
        status_code=STATUS_CODE_MANUEL,
    )

    item.update(data)
    item.complete(STATUS_MANUEL)

    logger.info(
        "Work item er sendt til manuel behandling. "
        "Reference: %s. Årsag: %s.",
        item.reference,
        manuel_aarsag,
    )


# ------------------------------------------------------------
# PROCESS-MODE, WORKER
# ------------------------------------------------------------

async def process_workqueue(
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Behandler køens items ét ad gangen."""
    if not isinstance(debug, bool):
        raise TypeError(
            "debug skal være True eller False."
        )

    logger.info(
        "Process workqueue mode started "
        "(debug=%s)",
        debug,
    )

    headless = (
        os.getenv(
            "HEADLESS",
            "true",
        ).lower()
        == "true"
    )

    session = BrowserSession(
        headless=headless,
        debug=debug,
    )

    await session.start()
    page = await session.new_page()

    try:
        for item in workqueue:
            with item:
                data = item.data

                if not isinstance(data, dict):
                    raise WorkItemError(
                        "Work item data skal være en dictionary. "
                        f"Modtog: {type(data).__name__}."
                    )

                try:
                    print(
                        "==================================== "
                        "NEXT ITEM "
                        "===================================="
                    )
                    pprint(data)

                    await behandel_page(
                        item=item,
                        session=session,
                        page=page,
                    )

                    if _er_manuel_behandling(
                        data=data,
                    ):
                        _gem_manuelt_item(
                            item=item,
                            data=data,
                        )
                    else:
                        _gem_completed_item(
                            item=item,
                            data=data,
                        )

                except WorkItemError as error:
                    logger.error(
                        "WorkItemError for item %s: %s",
                        item.reference,
                        error,
                    )

                    item.fail(
                        str(error)
                    )

                    await session.close()

                    session = BrowserSession(
                        headless=headless,
                        debug=debug,
                    )

                    await session.start()
                    page = await session.new_page()

                except Exception as error:
                    logger.exception(
                        "Uventet fejl"
                    )

                    try:
                        if (
                            session.context
                            and session.context.pages
                        ):
                            page = session.context.pages[-1]

                            await session.screenshot(
                                page,
                                (
                                    "hard_exception_"
                                    f"{type(error).__name__}"
                                ),
                                always=True,
                            )
                    except Exception:
                        logger.warning(
                            "Kunne ikke tage screenshot "
                            "ved hard error"
                        )

                    await session.close()
                    raise
    finally:
        await session.close()


# ------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------

if __name__ == "__main__":
    DEBUG = "--debug" in sys.argv
    QUEUE_MODE = "--queue" in sys.argv

    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    if QUEUE_MODE:
        workqueue.clear_workqueue(
            WorkItemStatus.NEW
        )

        asyncio.run(
            populate_queue(
                workqueue=workqueue,
                queue_id=_hent_queue_id(
                    workqueue=workqueue,
                ),
            )
        )

        sys.exit(0)

    asyncio.run(
        process_workqueue(
            workqueue,
            debug=DEBUG,
        )
    )
