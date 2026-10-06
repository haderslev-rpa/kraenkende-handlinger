"""Procesindgang: main.py ejer browser, login, API-klient og oprydning.

--queue fylder koeen. Uden --queue gennemgaas hele koeen, et item ad gangen.
--debug er ikke en dry-run: statusaendringer og mails udfoeres stadig.
"""

from __future__ import annotations

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
from playwright.async_api import Page
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import BrowserSession

import configuration
from behandel import MANUEL_STATE_PREFIX, behandel_page
from configuration import (
    QUEUE_ID,
    STATUS_CODE_COMPLETED,
    STATUS_CODE_MANUEL,
    STATUS_COMPLETED,
    STATUS_MANUEL,
)
from populate_queue import populate_queue
from q_insubiz.api.client import InsubizApiClient
from q_insubiz.api_client import create_api_client_from_context
from q_insubiz.functionality.launch import launch_insubiz
from q_insubiz.utils import normalize_positive_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for log_name in ("httpx", "automation_server_client", "debugpy"):
    logging.getLogger(log_name).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _hent_queue_id() -> int:
    """Returnerer det validerede tekniske koe-id fra configuration.py."""
    return normalize_positive_id(name="QUEUE_ID", value=QUEUE_ID)


def _valider_workqueue(workqueue: Workqueue) -> None:
    """Stopper foer koeaendringer, hvis ATS-koeen ikke matcher dubletopslaget."""
    if workqueue is None:
        raise ValueError("Automation Server returnerede ingen workqueue.")
    actual_id = normalize_positive_id(name="workqueue.id", value=workqueue.id)
    if actual_id != _hent_queue_id():
        raise RuntimeError(
            f"ATS-koe {actual_id} matcher ikke QUEUE_ID={QUEUE_ID}. "
            "Kontroller processens workqueue og lokal ATS-workqueue-opsatning."
        )


def _hent_headless(*, debug: bool, queue_mode: bool) -> bool:
    """Returnerer den valgte modes boolske browserindstilling."""
    if not isinstance(debug, bool) or not isinstance(queue_mode, bool):
        raise TypeError("debug og queue_mode skal vaere boolske.")
    if queue_mode:
        value = configuration.QUEUE_HEADLESS
    elif debug:
        value = configuration.DEBUG_HEADLESS
    else:
        value = configuration.HEADLESS
    if not isinstance(value, bool):
        raise TypeError("Headless-indstillingen skal vaere True eller False.")
    return value


# ------------------------------------------------------------
# BROWSER: FAELLES OPSTART, FEJLDOKUMENTATION OG OPRYDNING
# ------------------------------------------------------------
async def _tag_screenshot_ved_fejl(
    *,
    session: BrowserSession | None,
    error: BaseException,
) -> None:
    """Forsoeger fejlscreenshot uden at skjule den oprindelige fejl."""
    try:
        if session is None or session.context is None:
            return
        pages = [page for page in session.context.pages if not page.is_closed()]
        if pages:
            await session.screenshot(
                pages[-1],
                f"exception_{type(error).__name__}",
                always=True,
            )
    except Exception:
        logger.warning("Fejlscreenshot kunne ikke gemmes.", exc_info=True)


async def _luk_browser_session(
    *,
    session: BrowserSession | None,
    api_client: InsubizApiClient | None,
) -> None:
    """Frigiver klienten og lukker derefter browseren, ogsaa ved klientfejl."""
    try:
        if api_client is not None:
            try:
                await api_client.close()
            except Exception:
                logger.warning("API-klienten kunne ikke frigives.", exc_info=True)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                logger.warning("BrowserSession kunne ikke lukkes.", exc_info=True)
                # Bibliotekets close() kan stoppe efter en context-fejl.
                # Nyt kald lukker eventuelle resterende browserressourcer.
                try:
                    await session.close()
                except Exception:
                    logger.warning("Browseroprydning fejlede igen.", exc_info=True)


async def _opret_browser_session(
    *,
    debug: bool,
    queue_mode: bool,
) -> tuple[BrowserSession, Page, InsubizApiClient]:
    """Returnerer session, autentificeret side og klient paa samme context."""
    session = BrowserSession(
        headless=_hent_headless(debug=debug, queue_mode=queue_mode),
        debug=debug,
    )
    api_client: InsubizApiClient | None = None
    try:
        await session.start()
        page = await session.new_page()
        if session.context is None or page.is_closed():
            raise RuntimeError("BrowserSession mangler en aktiv side og context.")
        if page.context is not session.context:
            raise RuntimeError("Siden tilhoerer ikke BrowserSessions context.")
        if session.recorder is None:
            raise RuntimeError("BrowserSession mangler sin recorder.")
        await launch_insubiz(page=page, recorder=session.recorder)
        api_client = create_api_client_from_context(
            context=session.context,
            page=page,
        )
        logger.info("Insubiz-login OK. UI og API deler browsercontext.")
        return session, page, api_client
    except BaseException as error:
        await _tag_screenshot_ved_fejl(session=session, error=error)
        await _luk_browser_session(session=session, api_client=api_client)
        raise


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
        raise RuntimeError("MANUEL_STATE_PREFIX i behandel.py må ikke være tom.")

    for existing_state in reversed(_hent_states(data=data)):
        state_text = str(existing_state).strip()

        if state_text.casefold().startswith(normalized_prefix):
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
        "Work item blev sendt til manuel behandling. Reference: %s. State: %s.",
        item.reference,
        manuel_state,
    )


# ------------------------------------------------------------
# PROCESS-MODE: HELE KOEEN, ET ITEM AD GANGEN
# ------------------------------------------------------------
async def process_workqueue(*, workqueue: Workqueue, debug: bool) -> None:
    """Behandler hele koeen. Soft fejl genstarter login foer naeste item."""
    _valider_workqueue(workqueue)
    _hent_headless(debug=debug, queue_mode=False)
    session: BrowserSession | None = None
    api_client: InsubizApiClient | None = None
    page: Page | None = None
    try:
        for item in workqueue:
            # Start foer with item. Loginfejl stopper processen, ikke forretningsreglen.
            if session is None:
                session, page, api_client = await _opret_browser_session(
                    debug=debug,
                    queue_mode=False,
                )
            with item:
                try:
                    data = item.data
                    if not isinstance(data, dict):
                        raise WorkItemError("Work item data skal vaere en dictionary.")
                    print("=" * 35 + " NEXT ITEM " + "=" * 35)
                    pprint(data)
                    await behandel_page(
                        item=item,
                        session=session,
                        page=page,
                        api_client=api_client,
                    )
                    manuel_state = _hent_manuel_state(data=data)
                    if manuel_state is not None:
                        _gem_manuelt_item(
                            item=item,
                            data=data,
                            manuel_state=manuel_state,
                        )
                    else:
                        _gem_completed_item(item=item, data=data)
                except WorkItemError as error:
                    logger.error("WorkItemError for %s: %s", item.reference, error)
                    await _tag_screenshot_ved_fejl(session=session, error=error)
                    item.fail(str(error))
                    await _luk_browser_session(session=session, api_client=api_client)
                    session = None
                    page = None
                    api_client = None
                except Exception as error:
                    logger.exception("Uventet fejl for item %s.", item.reference)
                    await _tag_screenshot_ved_fejl(session=session, error=error)
                    raise
    finally:
        await _luk_browser_session(session=session, api_client=api_client)


# ------------------------------------------------------------
# QUEUE-MODE: DELT BROWSER OG KLIENT
# ------------------------------------------------------------
async def _koer_queue_mode(*, workqueue: Workqueue, debug: bool) -> None:
    """Fylder koeen; rydning af NEW-items styres eksplicit af configuration.py."""
    _valider_workqueue(workqueue)
    if not isinstance(configuration.CLEAR_NEW_ITEMS_BEFORE_POPULATE, bool):
        raise TypeError("CLEAR_NEW_ITEMS_BEFORE_POPULATE skal vaere boolsk.")
    session, _page, api_client = await _opret_browser_session(
        debug=debug,
        queue_mode=True,
    )
    try:
        if configuration.CLEAR_NEW_ITEMS_BEFORE_POPULATE:
            logger.warning("Sletter alle NEW-items i koe %s foer fyldning.", QUEUE_ID)
            workqueue.clear_workqueue(WorkItemStatus.NEW)
        await populate_queue(
            workqueue=workqueue,
            queue_id=_hent_queue_id(),
            api_client=api_client,
        )
    except BaseException as error:
        await _tag_screenshot_ved_fejl(session=session, error=error)
        raise
    finally:
        await _luk_browser_session(session=session, api_client=api_client)


def main() -> None:
    """Initialiserer ATS og vaelger producer eller worker. Returnerer None."""
    debug = "--debug" in sys.argv
    queue_mode = "--queue" in sys.argv
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()
    if queue_mode:
        asyncio.run(_koer_queue_mode(workqueue=workqueue, debug=debug))
    else:
        asyncio.run(process_workqueue(workqueue=workqueue, debug=debug))


if __name__ == "__main__":
    main()
