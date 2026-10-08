"""Startpunkt for kraenkende-handlinger.

main.py ejer:
- Automation Server og workqueue.
- BrowserSession, Playwright-side og Insubiz-login.
- API-klienten på samme browsercontext.
- Diagnostik, fejlscreenshots og oprydning.
- Afslutning af work items.

--queue fylder køen.
Uden --queue behandles køens items.
--debug er ikke en dry-run: mails og statusændringer udføres stadig.

Procesindstillinger læses fra configuration.py.
Diagnostikken ændrer ikke miljø eller browserinstallation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import socket
import sys
from importlib.metadata import distribution
from pathlib import Path
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
from q_insubiz.api.client import InsubizApiClient
from q_insubiz.api_client import create_api_client_from_context
from q_insubiz.functionality.launch import launch_insubiz
from q_insubiz.utils import normalize_positive_id

import configuration
from behandel import MANUEL_STATE_PREFIX, behandel_page
from populate_queue import populate_queue


# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s [%(levelname)s] "
        "%(name)s [pid=%(process)d]: %(message)s"
    ),
    stream=sys.stderr,
    force=True,
)

for logger_name in (
    "httpx",
    "automation_server_client",
    "playwright",
    "debugpy",
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# KOMMANDOLINJE OG KONFIGURATION
# ------------------------------------------------------------

def _har_argument(argument: str) -> bool:
    """Returnerer True, hvis argumentet er angivet."""
    return argument in sys.argv[1:]


def _get_debug_mode() -> bool:
    """Returnerer True ved --debug."""
    return _har_argument("--debug")


def _get_queue_mode() -> bool:
    """Returnerer True ved --queue."""
    return _har_argument("--queue")


def _hent_queue_id() -> int:
    """Returnerer det validerede kø-id fra configuration.py."""
    return normalize_positive_id(
        name="QUEUE_ID",
        value=configuration.QUEUE_ID,
    )


def _hent_headless(*, debug: bool, queue_mode: bool) -> bool:
    """Returnerer browserindstillingen for den valgte kørsel."""
    if not isinstance(debug, bool):
        raise TypeError("debug skal være True eller False.")

    if not isinstance(queue_mode, bool):
        raise TypeError("queue_mode skal være True eller False.")

    if queue_mode:
        headless = configuration.QUEUE_HEADLESS
    elif debug:
        headless = configuration.DEBUG_HEADLESS
    else:
        headless = configuration.HEADLESS

    if not isinstance(headless, bool):
        raise TypeError(
            "Headless-indstillingen skal være True eller False."
        )

    return headless


# ------------------------------------------------------------
# TEKNISK DIAGNOSTIK
# ------------------------------------------------------------

def _log_koerselsmiljoe() -> None:
    """Logger teknisk miljø uden credentials eller sagsdata."""
    try:
        logger.info(
            "MILJØ | server=%s | pid=%s",
            socket.gethostname(),
            os.getpid(),
        )
        logger.info(
            "MILJØ | python=%s | version=%s | platform=%s",
            sys.executable,
            sys.version.split()[0],
            sys.platform,
        )
        logger.info(
            "MILJØ | arbejdsmappe=%s | main=%s",
            Path.cwd(),
            Path(__file__).resolve(),
        )
        logger.info(
            "MILJØ | BrowserSession-kode=%s",
            inspect.getfile(BrowserSession),
        )

        # Kun disse konkrete miljøvariabler logges.
        # ATS_TOKEN og øvrige credentials aflæses ikke.
        for navn in (
            "PLAYWRIGHT_BROWSERS_PATH",
            "PLAYWRIGHT_SKIP_BROWSER_GC",
        ):
            logger.info(
                "MILJØ | %s=%r",
                navn,
                os.environ.get(navn, "[ikke sat]"),
            )

        for pakkenavn in (
            "playwright",
            "q-haderslev-vbo",
            "q-insubiz",
            "q-outlook-api",
            "automation-server-client",
        ):
            try:
                pakke = distribution(pakkenavn)
                commit = "[ikke tilgængelig]"
                metadata = pakke.read_text("direct_url.json")

                if metadata:
                    oplysninger = json.loads(metadata)
                    commit = (
                        oplysninger.get("vcs_info", {})
                        .get("commit_id", commit)
                    )

                # Repository-URL logges ikke.
                logger.info(
                    "PAKKE | navn=%s | version=%s | commit=%s",
                    pakkenavn,
                    pakke.version,
                    commit,
                )
            except Exception as error:
                logger.warning(
                    "PAKKE | metadata kunne ikke læses for %s "
                    "| fejltype=%s",
                    pakkenavn,
                    type(error).__name__,
                )

        # Fingeraftryk gør det muligt at sammenligne de konkrete
        # main.py- og uv.lock-filer mellem ATS-kørsler.
        projektmappe = Path(__file__).resolve().parent

        for fil in (
            Path(__file__).resolve(),
            projektmappe / "uv.lock",
        ):
            try:
                if fil.is_file():
                    fingeraftryk = hashlib.sha256(
                        fil.read_bytes()
                    ).hexdigest()

                    logger.info(
                        "KODE | fil=%s | sha256=%s",
                        fil.name,
                        fingeraftryk,
                    )
                else:
                    logger.warning(
                        "KODE | fil=%s | ikke fundet",
                        fil.name,
                    )
            except OSError as error:
                logger.warning(
                    "KODE | fil=%s | kunne ikke aflæses "
                    "| fejltype=%s",
                    fil.name,
                    type(error).__name__,
                )

    except Exception as error:
        # Diagnostik må ikke forhindre den normale proceskørsel.
        logger.warning(
            "Miljødiagnostik kunne ikke afsluttes | fejltype=%s",
            type(error).__name__,
        )


def _log_browserfil_ved_fejl(error: BaseException) -> None:
    """Undersøger den konkrete filsti fra Playwrights fejl."""
    try:
        match = re.search(
            r"Executable doesn't exist at ([^\r\n]+)",
            str(error),
        )

        if match is None:
            return

        browserfil = Path(match.group(1).strip())

        logger.error(
            "BROWSERFIL | sti=%s | findes_nu=%s "
            "| er_fil_nu=%s | eksekverbar_nu=%s",
            browserfil,
            browserfil.exists(),
            browserfil.is_file(),
            os.access(browserfil, os.X_OK),
        )

        for mappe in (
            browserfil.parent,
            browserfil.parent.parent,
            browserfil.parent.parent.parent,
        ):
            try:
                logger.error(
                    "BROWSERMAPPE | sti=%s | findes_nu=%s",
                    mappe,
                    mappe.exists(),
                )

                if mappe.is_dir():
                    logger.error(
                        "BROWSERMAPPE | sti=%s | indhold=%s",
                        mappe,
                        sorted(
                            element.name
                            for element in mappe.iterdir()
                        ),
                    )
            except OSError as mappefejl:
                logger.warning(
                    "BROWSERMAPPE | sti=%s "
                    "| kunne ikke aflæses | fejltype=%s",
                    mappe,
                    type(mappefejl).__name__,
                )

        logger.error(
            "FORKLARING | Playwright rapporterede en manglende "
            "browserfil. Diagnostikken viser filsystemets tilstand "
            "efter fejlen. Den beviser ikke, hvorfor filen mangler."
        )

    except Exception as diagnostikfejl:
        logger.warning(
            "Browserfil-diagnostik fejlede | fejltype=%s",
            type(diagnostikfejl).__name__,
        )


# ------------------------------------------------------------
# AUTOMATION SERVER
# ------------------------------------------------------------

def _opret_automation_server() -> AutomationServer:
    """Initialiserer og returnerer Automation Server-klienten."""
    logger.info("ATS | initialisering starter")

    try:
        automation_server = AutomationServer.from_environment()
    except Exception as error:
        raise RuntimeError(
            "Automation Server kunne ikke initialiseres."
        ) from error

    if automation_server is None:
        raise RuntimeError(
            "AutomationServer.from_environment() returnerede None."
        )

    logger.info("ATS | initialisering OK")
    return automation_server


def _opret_workqueue(
    *,
    automation_server: AutomationServer,
    queue_id: int,
) -> Workqueue:
    """Konfigurerer kø-id og returnerer den validerede workqueue."""
    logger.info("ATS | henter workqueue | queue_id=%s", queue_id)

    automation_server.workqueue_id = queue_id

    configured_queue_id = normalize_positive_id(
        name="AutomationServer.workqueue_id",
        value=automation_server.workqueue_id,
    )

    if configured_queue_id != queue_id:
        raise RuntimeError(
            "Automation Server blev konfigureret med forkert kø-id."
        )

    try:
        workqueue = automation_server.workqueue()
    except Exception as error:
        raise RuntimeError(
            f"Workqueue kunne ikke oprettes. Queue-id: {queue_id}."
        ) from error

    _valider_workqueue(workqueue)

    logger.info("ATS | workqueue OK | queue_id=%s", queue_id)
    return workqueue


def _valider_workqueue(workqueue: Workqueue) -> None:
    """Kontrollerer køen mod processens konfigurerede kø-id."""
    if workqueue is None:
        raise ValueError("Automation Server returnerede ingen workqueue.")

    actual_id = normalize_positive_id(
        name="workqueue.id",
        value=workqueue.id,
    )
    expected_id = _hent_queue_id()

    if actual_id != expected_id:
        raise RuntimeError(
            f"ATS-kø {actual_id} matcher ikke QUEUE_ID={expected_id}."
        )


# ------------------------------------------------------------
# SCREENSHOT OG OPRYDNING
# ------------------------------------------------------------

async def _tag_screenshot_ved_fejl(
    *,
    session: BrowserSession | None,
    error: BaseException,
) -> None:
    """Forsøger screenshot uden at skjule den oprindelige fejl."""
    try:
        if session is None or session.context is None:
            logger.warning(
                "SCREENSHOT | kan ikke tages: ingen browsercontext"
            )
            return

        aktive_sider = [
            page
            for page in session.context.pages
            if not page.is_closed()
        ]

        if not aktive_sider:
            logger.warning(
                "SCREENSHOT | kan ikke tages: ingen aktiv side"
            )
            return

        logger.info("SCREENSHOT | forsøger fejlscreenshot")

        await session.screenshot(
            aktive_sider[-1],
            f"exception_{type(error).__name__}",
            always=True,
        )

    except Exception:
        logger.warning(
            "SCREENSHOT | fejlscreenshot kunne ikke gemmes",
            exc_info=True,
        )


async def _luk_browser_session(
    *,
    session: BrowserSession | None,
    api_client: InsubizApiClient | None,
) -> None:
    """Lukker API-klienten og derefter BrowserSession."""
    try:
        if api_client is not None:
            try:
                logger.info("OPRYDNING | lukker API-klient")
                await api_client.close()
                logger.info("OPRYDNING | API-klient lukket")
            except Exception:
                logger.warning(
                    "OPRYDNING | API-klienten kunne ikke lukkes",
                    exc_info=True,
                )
    finally:
        if session is not None:
            try:
                logger.info("OPRYDNING | lukker BrowserSession")
                await session.close()
                logger.info("OPRYDNING | BrowserSession lukket")
            except Exception:
                logger.warning(
                    "OPRYDNING | BrowserSession kunne ikke lukkes",
                    exc_info=True,
                )


# ------------------------------------------------------------
# BROWSER OG INSUBIZ
# ------------------------------------------------------------

async def _opret_browser_session(
    *,
    headless: bool,
    debug: bool,
) -> tuple[BrowserSession, Page, InsubizApiClient]:
    """Returnerer session, indlogget side og delt API-klient."""
    if not isinstance(headless, bool):
        raise TypeError("headless skal være True eller False.")

    if not isinstance(debug, bool):
        raise TypeError("debug skal være True eller False.")

    session: BrowserSession | None = None
    api_client: InsubizApiClient | None = None
    trin = "oprettelse af BrowserSession"

    try:
        logger.info(
            "BROWSER | trin=%s | starter "
            "| headless=%s | debug=%s",
            trin,
            headless,
            debug,
        )

        session = BrowserSession(
            headless=headless,
            debug=debug,
        )

        trin = "browserstart"
        logger.info("BROWSER | trin=%s | starter", trin)

        await session.start()

        logger.info("BROWSER | trin=%s | OK", trin)

        trin = "oprettelse af side og browsercontext"
        logger.info("BROWSER | trin=%s | starter", trin)

        page = await session.new_page()

        if page.is_closed():
            raise RuntimeError("Den oprettede Playwright-side er lukket.")

        if session.context is None:
            raise RuntimeError("BrowserSession mangler browsercontext.")

        if page.context is not session.context:
            raise RuntimeError(
                "Siden tilhører ikke BrowserSessions context."
            )

        if session.recorder is None:
            raise RuntimeError("BrowserSession mangler recorder.")

        logger.info("BROWSER | trin=%s | OK", trin)

        trin = "Insubiz-login"
        logger.info("BROWSER | trin=%s | starter", trin)

        await launch_insubiz(
            page=page,
            recorder=session.recorder,
        )

        logger.info("BROWSER | trin=%s | OK", trin)

        trin = "oprettelse af delt API-klient"
        logger.info("BROWSER | trin=%s | starter", trin)

        api_client = create_api_client_from_context(
            context=session.context,
            page=page,
        )

        logger.info("BROWSER | trin=%s | OK", trin)
        logger.info("BROWSER | session, login og API-klient er klar")

        return session, page, api_client

    except BaseException as error:
        logger.exception(
            "BROWSER | FEJL | trin=%s | fejltype=%s",
            trin,
            type(error).__name__,
        )

        _log_browserfil_ved_fejl(error)

        await _tag_screenshot_ved_fejl(
            session=session,
            error=error,
        )
        await _luk_browser_session(
            session=session,
            api_client=api_client,
        )
        raise


# ------------------------------------------------------------
# QUEUE-MODE
# ------------------------------------------------------------

async def _run_queue_mode(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Logger ind, rydder NEW-items og fylder køen."""
    _valider_workqueue(workqueue)

    clear_new_items = configuration.CLEAR_NEW_ITEMS_BEFORE_POPULATE

    if not isinstance(clear_new_items, bool):
        raise TypeError(
            "CLEAR_NEW_ITEMS_BEFORE_POPULATE skal være True eller False."
        )

    headless = _hent_headless(
        debug=debug,
        queue_mode=True,
    )

    logger.info(
        "QUEUE | starter | queue_id=%s "
        "| headless=%s | debug=%s | ryd_NEW=%s",
        _hent_queue_id(),
        headless,
        debug,
        clear_new_items,
    )

    session: BrowserSession | None = None
    api_client: InsubizApiClient | None = None
    trin = "browser og login"

    try:
        session, _page, api_client = await _opret_browser_session(
            headless=headless,
            debug=debug,
        )

        if clear_new_items:
            trin = "rydning af NEW-items"
            logger.info("QUEUE | trin=%s | starter", trin)

            workqueue.clear_workqueue(WorkItemStatus.NEW)

            logger.info("QUEUE | trin=%s | OK", trin)
        else:
            logger.info("QUEUE | eksisterende NEW-items beholdes")

        trin = "hentning, filtrering og oprettelse af kø-items"
        logger.info("QUEUE | trin=%s | starter", trin)

        await populate_queue(
            workqueue=workqueue,
            queue_id=_hent_queue_id(),
            api_client=api_client,
        )

        logger.info("QUEUE | trin=%s | OK", trin)

    except BaseException as error:
        logger.exception(
            "QUEUE | FEJL | trin=%s | fejltype=%s",
            trin,
            type(error).__name__,
        )

        # Browseropstart tager selv screenshot og rydder op ved fejl.
        # Her håndteres fejl efter en gennemført browseropstart.
        if session is not None:
            await _tag_screenshot_ved_fejl(
                session=session,
                error=error,
            )
        raise

    finally:
        await _luk_browser_session(
            session=session,
            api_client=api_client,
        )

    logger.info("QUEUE | afsluttet uden fejl")


# ------------------------------------------------------------
# WORK ITEM-AFSLUTNING
# ------------------------------------------------------------

def _hent_manuel_state(*, data: dict[str, Any]) -> str | None:
    """Returnerer registreret manuel state eller None."""
    states = data.get("state", [])

    if states is None:
        return None

    if not isinstance(states, list):
        raise WorkItemError("Work item-feltet state skal være en liste.")

    prefix = MANUEL_STATE_PREFIX.strip().casefold()

    if not prefix:
        raise RuntimeError("MANUEL_STATE_PREFIX må ikke være tom.")

    for existing_state in reversed(states):
        state_text = str(existing_state).strip()

        # Bevarer understøttelse af tidsstempel foran state-teksten.
        if prefix in state_text.casefold():
            return state_text

    return None


def _faerdiggoer_item(*, item: Any, data: Any) -> None:
    """Gemmer processtatus og afslutter kø-itemet."""
    if not isinstance(data, dict):
        raise WorkItemError("Work item data skal være en dictionary.")

    manuel_state = _hent_manuel_state(data=data)

    if manuel_state is not None:
        update_item_data(
            data,
            item=item,
            status=configuration.STATUS_MANUEL,
            status_code=configuration.STATUS_CODE_MANUEL,
        )
        completion_message = configuration.STATUS_MANUEL
    else:
        update_item_data(
            data,
            item=item,
            status=configuration.STATUS_COMPLETED,
            status_code=configuration.STATUS_CODE_COMPLETED,
            state=configuration.STATUS_COMPLETED,
        )
        completion_message = configuration.STATUS_COMPLETED

    logger.info(
        "ITEM | gemmer data | reference=%s | processtatus=%s",
        item.reference,
        completion_message,
    )
    item.update(data)

    logger.info(
        "ITEM | kalder complete() | reference=%s",
        item.reference,
    )
    item.complete(completion_message)

    logger.info(
        "ITEM | complete() returnerede uden fejl "
        "| reference=%s | processtatus=%s",
        item.reference,
        completion_message,
    )


# ------------------------------------------------------------
# PROCESS-MODE
# ------------------------------------------------------------

async def process_workqueue(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Behandler køens items med en delt Insubiz-session."""
    _valider_workqueue(workqueue)

    headless = _hent_headless(
        debug=debug,
        queue_mode=False,
    )

    logger.info(
        "PROCESS | starter | queue_id=%s | headless=%s | debug=%s",
        _hent_queue_id(),
        headless,
        debug,
    )

    session: BrowserSession | None = None
    api_client: InsubizApiClient | None = None
    page: Page | None = None

    try:
        session, page, api_client = await _opret_browser_session(
            headless=headless,
            debug=debug,
        )

        for item in workqueue:
            # En soft fejl lukker sessionen. Næste item får nyt login.
            if session is None:
                logger.info(
                    "PROCESS | opretter ny session efter tidligere itemfejl"
                )

                session, page, api_client = await _opret_browser_session(
                    headless=headless,
                    debug=debug,
                )

            with item:
                trin = "behandling"

                try:
                    logger.info(
                        "ITEM | behandling starter | reference=%s",
                        item.reference,
                    )

                    await behandel_page(
                        item=item,
                        session=session,
                        page=page,
                        api_client=api_client,
                    )

                    logger.info(
                        "ITEM | behandling returnerede uden fejl "
                        "| reference=%s",
                        item.reference,
                    )

                    trin = "afslutning af work item"

                    _faerdiggoer_item(
                        item=item,
                        data=item.data,
                    )

                except WorkItemError as error:
                    logger.error(
                        "ITEM | soft fejl | trin=%s "
                        "| reference=%s | fejl=%s",
                        trin,
                        item.reference,
                        error,
                    )

                    await _tag_screenshot_ved_fejl(
                        session=session,
                        error=error,
                    )

                    item.fail(str(error))

                    await _luk_browser_session(
                        session=session,
                        api_client=api_client,
                    )

                    session = None
                    page = None
                    api_client = None

                except Exception as error:
                    logger.exception(
                        "ITEM | hard fejl | trin=%s "
                        "| reference=%s | fejltype=%s",
                        trin,
                        item.reference,
                        type(error).__name__,
                    )

                    await _tag_screenshot_ved_fejl(
                        session=session,
                        error=error,
                    )
                   