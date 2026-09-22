from __future__ import annotations

"""Hovedindgang til processen.

Alle konfigurerbare inputs importeres fra config.py.
Main.py læser derfor ikke værdier fra .env eller os.getenv.
"""

import asyncio
import logging
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
from config import (
    HEADLESS,
    MANUEL_STATE_PREFIX,
    QUEUE_ID,
    STATUS_CODE_COMPLETED,
    STATUS_CODE_MANUEL,
    STATUS_COMPLETED,
    STATUS_MANUEL,
)
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
# KONFIGURATIONSKONTROL
# ------------------------------------------------------------


def _hent_queue_id() -> int:
    """Validerer og returnerer queue-id fra config.py."""
    try:
        return normalize_positive_id(
            name="QUEUE_ID",
            value=QUEUE_ID,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "QUEUE_ID i config.py skal være et positivt heltal. "
            f"Modtog: {QUEUE_ID!r}."
        ) from error


def _hent_headless() -> bool:
    """Validerer og returnerer HEADLESS fra config.py."""
    if not isinstance(HEADLESS, bool):
        raise RuntimeError(
            "HEADLESS i config.py skal være True eller False. "
            f"Modtog: {HEADLESS!r}."
        )

    return HEADLESS


# ------------------------------------------------------------
# WORK ITEM-HJÆLPERE
# ------------------------------------------------------------


def _hent_states(
    *,
    data: dict[str, Any],
) -> list[Any]:
    """Returnerer work itemets validerede state-liste."""
    states = data.get("state", [])

    if states is None:
        return []

    if not isinstance(states, list):
        raise WorkItemError(
            "Work item-feltet state skal være en liste. "
            f"Modtog: {type(states).__name__}."
        )

    return states


def _hent_manuel_state(
    *,
    data: dict[str, Any],
) -> str | None:
    """Finder den manuelle 1.0-state, som behandel.py har registreret."""
    normalized_prefix = MANUEL_STATE_PREFIX.strip().casefold()

    if not normalized_prefix:
        raise RuntimeError(
            "MANUEL_STATE_PREFIX i config.py må ikke være tom."
        )

    for existing_state in reversed(
        _hent_states(data=data)
    ):
        state_text = str(existing_state).strip()

        if state_text.casefold().startswith(
            normalized_prefix
        ):
            return state_text

    return None


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
    )

    item.update(data)
    item.complete(STATUS_COMPLETED)

    logger.info(
        "Work item blev afsluttet. Reference: %s.",
        item.reference,
    )


def _gem_manuelt_item(
    *,
    item: Any,
    data: dict[str, Any],
    manuel_state: str,
) -> None:
    """Gemmer et item med status Manuel uden at overskrive 1.0-state."""
    update_item_data(
        data,
        item=item,
        status=STATUS_MANUEL,
        status_code=STATUS_CODE_MANUEL,
    )

    item.update(data)
    item.complete(STATUS_MANUEL)

    logger.info(
        "Work item blev sendt til manuel behandling. "
        "Reference: %s. State: %s.",
        item.reference,
        manuel_state,
    )


# ------------------------------------------------------------
# PROCESS-MODE
# ------------------------------------------------------------


async def process_workqueue(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Behandler køens items ét ad gangen."""
    if not isinstance(debug, bool):
        raise TypeError(
            "debug skal være True eller False."
        )

    headless = _hent_headless()

    logger.info(
        "Process workqueue mode startet. "
        "Debug: %s. Headless: %s.",
        debug,
        headless,
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

                    manuel_state = _hent_manuel_state(
                        data=data,
                    )

                    if manuel_state is not None:
                        _gem_manuelt_item(
                            item=item,
                            data=data,
                            manuel_state=manuel_state,
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

                    item.fail(str(error))

                    await session.close()

                    session = BrowserSession(
                        headless=headless,
                        debug=debug,
                    )

                    await session.start()
                    page = await session.new_page()

                except Exception as error:
                    logger.exception(
                        "Uventet fejl for item %s.",
                        item.reference,
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
                            "ved hard error."
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
                queue_id=_hent_queue_id(),
            )
        )

        sys.exit(0)

    asyncio.run(
        process_workqueue(
            workqueue=workqueue,
            debug=DEBUG,
        )
    )
