from __future__ import annotations

"""
Behandling af ét work item med en Insubiz-skade.

Skaden afsluttes kun, når begge krav er opfyldt:

1. easy.compensation.id er 0.
2. accidentDuration.text er
   "Uarbejdsdygtighed mindre end 1 dag".

Hvis begge krav er opfyldt:

- Skaden sættes til status AFSLUTTET i Insubiz.
- State "1.0 Skade afsluttet" registreres.

Hvis et eller begge krav ikke er opfyldt:

- Skaden ændres ikke i Insubiz.
- Der sendes en mail om manuel behandling.
- Work itemets status sættes til "Manuel".
- Der registreres én 1.0-state med årsagen.
- Der tilføjes ingen ekstra felter i box.

Mailafsender og mailmodtager hentes fra .env via:

    mailafsender
    mailmodtager
"""

import logging
import os
from typing import Any

from automation_server_client import WorkItemError
from dotenv import load_dotenv
from playwright.async_api import Page
from q_outlook_api.functionality.mail_api import send_mail

from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import BrowserSession
from q_insubiz.api_client import create_api_client
from q_insubiz.functionality.skader import (
    hent_skade_via_id,
    opdater_skade_status_fra_seneste_data,
)
from q_insubiz.models import SkadeStatus


load_dotenv()

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# FORRETNINGSREGLER
# ------------------------------------------------------------

FORVENTET_COMPENSATION_ID = 0

FORVENTET_ACCIDENT_DURATION_TEXT = (
    "Uarbejdsdygtighed mindre end 1 dag"
)


# ------------------------------------------------------------
# MILJØVARIABLER
# ------------------------------------------------------------

ENV_MAILAFSENDER = "mailafsender"
ENV_MAILMODTAGER = "mailmodtager"


# ------------------------------------------------------------
# WORK ITEM-FELTER
# ------------------------------------------------------------

SKADE_ID_BOX_FELTER = (
    "Skade_id",
    "skade_id",
    "Skade id",
    "Skade-id",
)

SKADE_NR_BOX_FELTER = (
    "Skade_nr",
    "skade_nr",
    "Skade nr",
    "Skade-nr",
)


# ------------------------------------------------------------
# STATES
# ------------------------------------------------------------

class States:
    """Afsluttende states for behandlingen."""

    AFSLUT_SKADE = "1.0 Skade afsluttet"

    MANUEL_COMPENSATION = (
        "1.0 Manuel - Vurderes efter arbejdsskadeloven = Ja"
    )

    MANUEL_UARBEJDSDYGTIGHED = (
        "1.0 Manuel - Uarbejdsdygtighed er ikke mindre end 1 dag"
    )

    MANUEL_BEGGE_KRAV = (
        "1.0 Manuel - Begge krav er ikke opfyldt"
    )


AFSLUTTENDE_STATES = (
    States.AFSLUT_SKADE,
    States.MANUEL_COMPENSATION,
    States.MANUEL_UARBEJDSDYGTIGHED,
    States.MANUEL_BEGGE_KRAV,
)


# ------------------------------------------------------------
# PUBLIC BEHANDLINGSFUNKTION
# ------------------------------------------------------------

async def behandel_page(
    item: Any,
    session: BrowserSession,
    page: Page,
) -> None:
    """Kontrollerer én skade og afslutter eller markerer den manuel."""
    _valider_session_og_side(
        session=session,
        page=page,
    )

    data = item.data

    if not isinstance(data, dict):
        raise WorkItemError(
            "Work item data skal være en dictionary. "
            f"Modtog: {type(data).__name__}."
        )

    box = _hent_box(data=data)

    skade_id = _hent_skade_id_fra_box(
        box=box,
    )

    skade_nr = _hent_skade_nr_fra_box(
        box=box,
    )

    if _har_afsluttende_state(data=data):
        print()
        print("=" * 80)
        print(f"Skade-id: {skade_id}")
        print("SKIP: Itemet har allerede en afsluttende 1.0-state")
        print("=" * 80)
        print()
        return

    mailafsender, mailmodtager = _hent_mailkonfiguration()

    print()
    print("=" * 80)
    print("KONTROL AF SKADE")
    print("=" * 80)
    print(f"Skade-id: {skade_id}")
    print(f"Skade-nr.: {skade_nr}")

    api_client = create_api_client()

    try:
        skade = await hent_skade_via_id(
            api_client=api_client,
            skade_id=skade_id,
        )

        compensation_id = _hent_compensation_id(
            skade=skade,
        )

        accident_duration_text = (
            _hent_accident_duration_text(
                skade=skade,
            )
        )

        compensation_opfyldt = (
            compensation_id
            == FORVENTET_COMPENSATION_ID
        )

        accident_duration_opfyldt = (
            _normaliser_tekst(
                accident_duration_text
            )
            == _normaliser_tekst(
                FORVENTET_ACCIDENT_DURATION_TEXT
            )
        )

        _print_kontrolresultat(
            compensation_id=compensation_id,
            compensation_opfyldt=(
                compensation_opfyldt
            ),
            accident_duration_text=(
                accident_duration_text
            ),
            accident_duration_opfyldt=(
                accident_duration_opfyldt
            ),
        )

        if not (
            compensation_opfyldt
            and accident_duration_opfyldt
        ):
            manuel_state = _bestem_manuel_state(
                compensation_opfyldt=(
                    compensation_opfyldt
                ),
                accident_duration_opfyldt=(
                    accident_duration_opfyldt
                ),
            )

            _send_mail_om_manuel_behandling(
                mailafsender=mailafsender,
                mailmodtager=mailmodtager,
                skade_id=skade_id,
                skade_nr=skade_nr,
                state=manuel_state,
            )

            update_item_data(
                data,
                item=item,
                status="Manuel",
                status_code="Manuel",
                state=manuel_state,
            )

            print(
                f"BESLUTNING: Skade {skade_id} "
                "sendes til MANUEL behandling"
            )
            print(f"State: {manuel_state}")
            print("Status: Manuel")
            print("Mail: Sendt")
            print("=" * 80)
            print()
            return

        print(
            f"BESLUTNING: Skade {skade_id} "
            "afsluttes"
        )
        print("Årsag: Begge krav er opfyldt.")
        print("=" * 80)
        print()

        await opdater_skade_status_fra_seneste_data(
            api_client=api_client,
            skade_id=skade_id,
            status=SkadeStatus.AFSLUTTET,
        )

        opdateret_skade = await hent_skade_via_id(
            api_client=api_client,
            skade_id=skade_id,
        )

        status_id = _hent_status_id(
            skade=opdateret_skade,
        )

        forventet_status_id = int(
            SkadeStatus.AFSLUTTET
        )

        if status_id != forventet_status_id:
            raise WorkItemError(
                "Statusændringen blev sendt, men skaden "
                "har ikke status AFSLUTTET. "
                f"Skade-id: {skade_id}. "
                f"Forventede status-id: "
                f"{forventet_status_id}. "
                f"Modtog: {status_id!r}."
            )

        update_item_data(
            data,
            item=item,
            state=States.AFSLUT_SKADE,
        )

        print(
            "Statuskontrol: OPFYLDT, "
            f"status-id er {status_id}"
        )
        print(
            f"RESULTAT: Skade {skade_id} "
            "er afsluttet"
        )
        print("=" * 80)
        print()

        logger.info(
            "Skaden er afsluttet og state er sat. "
            "Skade-id: %s.",
            skade_id,
        )

    finally:
        await api_client.close()


# ------------------------------------------------------------
# MAIL
# ------------------------------------------------------------

def _send_mail_om_manuel_behandling(
    *,
    mailafsender: str,
    mailmodtager: str,
    skade_id: int,
    skade_nr: str,
    state: str,
) -> None:
    """Sender mail om manuel behandling via q_outlook_api."""
    mail = {
        "subject": (
            "Manuel behandling af skade "
            f"{skade_nr}"
        ),
        "body": (
            "Følgende skade kræver manuel behandling.\n\n"
            f"Skade-id: {skade_id}\n"
            f"Skade-nr.: {skade_nr}\n"
            f"Årsag: {state}"
        ),
        "to": [mailmodtager],
        "cc": [],
        "bcc": [],
    }

    send_mail(
        mailafsender,
        mail,
    )

    logger.info(
        "Mail om manuel behandling blev sendt. "
        "Skade-id: %s. Skade-nr.: %s.",
        skade_id,
        skade_nr,
    )


# ------------------------------------------------------------
# MANUEL BEHANDLING
# ------------------------------------------------------------

def _bestem_manuel_state(
    *,
    compensation_opfyldt: bool,
    accident_duration_opfyldt: bool,
) -> str:
    """Returnerer den ene state, der forklarer manuel behandling."""
    if (
        not compensation_opfyldt
        and not accident_duration_opfyldt
    ):
        return States.MANUEL_BEGGE_KRAV

    if not compensation_opfyldt:
        return States.MANUEL_COMPENSATION

    return States.MANUEL_UARBEJDSDYGTIGHED


# ------------------------------------------------------------
# MILJØVARIABLER
# ------------------------------------------------------------

def _hent_miljoevaerdi(
    *,
    name: str,
) -> str:
    """Henter en obligatorisk værdi fra miljøet eller .env."""
    value = os.getenv(name)

    if value is None:
        raise WorkItemError(
            "Den obligatoriske miljøvariabel "
            f"{name!r} mangler."
        )

    normalized_value = value.strip()

    if not normalized_value:
        raise WorkItemError(
            "Den obligatoriske miljøvariabel "
            f"{name!r} er tom."
        )

    return normalized_value


def _hent_mailkonfiguration() -> tuple[str, str]:
    """Henter mailafsender og mailmodtager fra .env."""
    return (
        _hent_miljoevaerdi(
            name=ENV_MAILAFSENDER,
        ),
        _hent_miljoevaerdi(
            name=ENV_MAILMODTAGER,
        ),
    )


# ------------------------------------------------------------
# TERMINALUDSKRIFT
# ------------------------------------------------------------

def _print_kontrolresultat(
    *,
    compensation_id: int | None,
    compensation_opfyldt: bool,
    accident_duration_text: str,
    accident_duration_opfyldt: bool,
) -> None:
    """Udskriver resultatet af de to krav."""
    print("-" * 80)
    print("KRAV 1: EASY compensation")
    print(
        "Forventet: easy.compensation.id = "
        f"{FORVENTET_COMPENSATION_ID}"
    )
    print(
        "Faktisk:   easy.compensation.id = "
        f"{compensation_id!r}"
    )
    print(
        "Resultat:  "
        + (
            "OPFYLDT"
            if compensation_opfyldt
            else "IKKE OPFYLDT"
        )
    )

    print("-" * 80)
    print("KRAV 2: Uarbejdsdygtighed")
    print(
        "Forventet: "
        f"{FORVENTET_ACCIDENT_DURATION_TEXT!r}"
    )
    print(
        "Faktisk:   "
        f"{accident_duration_text!r}"
    )
    print(
        "Resultat:  "
        + (
            "OPFYLDT"
            if accident_duration_opfyldt
            else "IKKE OPFYLDT"
        )
    )
    print("-" * 80)


# ------------------------------------------------------------
# STATE
# ------------------------------------------------------------

def _har_afsluttende_state(
    *,
    data: dict[str, Any],
) -> bool:
    """Kontrollerer om itemet allerede har en afsluttende state."""
    states = data.get(
        "state",
        [],
    )

    if states is None:
        return False

    if not isinstance(states, list):
        raise WorkItemError(
            "Work item-feltet state skal være en liste. "
            f"Modtog: {type(states).__name__}."
        )

    return any(
        expected_state in str(existing_state)
        for existing_state in states
        for expected_state in AFSLUTTENDE_STATES
    )


# ------------------------------------------------------------
# WORK ITEM-DATA
# ------------------------------------------------------------

def _hent_box(
    *,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Returnerer work itemets box."""
    box = data.get("box")

    if not isinstance(box, dict):
        raise WorkItemError(
            "Work item-feltet box skal være en dictionary. "
            f"Modtog: {type(box).__name__}."
        )

    return box


def _hent_skade_id_fra_box(
    *,
    box: dict[str, Any],
) -> int:
    """Henter og validerer skade-id fra box."""
    faktisk_feltnavn = _find_box_feltnavn(
        box=box,
        feltnavne=SKADE_ID_BOX_FELTER,
    )

    if faktisk_feltnavn is None:
        raise WorkItemError(
            "Work itemets box mangler Skade_id. "
            f"Tilgængelige felter: "
            f"{list(box.keys())!r}."
        )

    skade_id = _normaliser_heltal(
        value=box[faktisk_feltnavn],
        field_name="box.Skade_id",
    )

    if skade_id <= 0:
        raise WorkItemError(
            "box.Skade_id skal være større end 0. "
            f"Modtog: {skade_id}."
        )

    return skade_id


def _hent_skade_nr_fra_box(
    *,
    box: dict[str, Any],
) -> str:
    """Henter skadens læsevenlige nummer fra box."""
    faktisk_feltnavn = _find_box_feltnavn(
        box=box,
        feltnavne=SKADE_NR_BOX_FELTER,
    )

    if faktisk_feltnavn is None:
        raise WorkItemError(
            "Work itemets box mangler Skade_nr. "
            f"Tilgængelige felter: "
            f"{list(box.keys())!r}."
        )

    skade_nr = str(
        box[faktisk_feltnavn]
    ).strip()

    if not skade_nr:
        raise WorkItemError(
            "Work itemets box indeholder et tomt Skade_nr."
        )

    return skade_nr


def _find_box_feltnavn(
    *,
    box: dict[str, Any],
    feltnavne: tuple[str, ...],
) -> str | None:
    """Finder et box-feltnavn via kendte aliaser."""
    normaliserede_felter = {
        _normaliser_feltnavn(field_name): field_name
        for field_name in box
        if isinstance(field_name, str)
    }

    for feltnavn in feltnavne:
        faktisk_feltnavn = normaliserede_felter.get(
            _normaliser_feltnavn(feltnavn)
        )

        if faktisk_feltnavn is not None:
            return faktisk_feltnavn

    return None


# ------------------------------------------------------------
# INSUBIZ-FELTER
# ------------------------------------------------------------

def _hent_compensation_id(
    *,
    skade: dict[str, Any],
) -> int | None:
    """Henter easy.compensation.id fra skaden."""
    direct_value = skade.get(
        "easy.compensation.id"
    )

    if direct_value is not None:
        return _normaliser_heltal(
            value=direct_value,
            field_name="easy.compensation.id",
        )

    easy = skade.get("easy")

    if easy is None:
        return None

    if not isinstance(easy, dict):
        raise WorkItemError(
            "Feltet easy skal være en dictionary. "
            f"Modtog: {type(easy).__name__}."
        )

    compensation = easy.get("compensation")

    if compensation is None:
        return None

    value = (
        compensation.get("id")
        if isinstance(compensation, dict)
        else compensation
    )

    if value is None:
        return None

    return _normaliser_heltal(
        value=value,
        field_name="easy.compensation.id",
    )


def _hent_accident_duration_text(
    *,
    skade: dict[str, Any],
) -> str:
    """Henter accidentDuration.text fra skaden."""
    direct_value = skade.get(
        "accidentDuration.text"
    )

    if direct_value is not None:
        return str(direct_value).strip()

    accident_duration = skade.get(
        "accidentDuration"
    )

    if accident_duration is None:
        personal_injury = skade.get(
            "personalInjury"
        )

        if personal_injury is not None:
            if not isinstance(
                personal_injury,
                dict,
            ):
                raise WorkItemError(
                    "Feltet personalInjury skal være "
                    "en dictionary."
                )

            accident_duration = (
                personal_injury.get(
                    "accidentDuration"
                )
            )

    if accident_duration is None:
        return ""

    if isinstance(accident_duration, str):
        return accident_duration.strip()

    if not isinstance(accident_duration, dict):
        raise WorkItemError(
            "Feltet accidentDuration skal være "
            "en dictionary eller tekst."
        )

    return str(
        accident_duration.get("text")
        or ""
    ).strip()


def _hent_status_id(
    *,
    skade: dict[str, Any],
) -> int | None:
    """Henter status-id fra et skadesvar."""
    direct_value = skade.get("status.id")

    if direct_value is not None:
        return _normaliser_heltal(
            value=direct_value,
            field_name="status.id",
        )

    status = skade.get("status")

    if status is None:
        status_id = skade.get("statusId")

        if status_id is None:
            return None

        return _normaliser_heltal(
            value=status_id,
            field_name="statusId",
        )

    value = (
        status.get("id")
        if isinstance(status, dict)
        else status
    )

    if value is None:
        return None

    return _normaliser_heltal(
        value=value,
        field_name="status.id",
    )


# ------------------------------------------------------------
# GENERELLE HJÆLPERE
# ------------------------------------------------------------

def _normaliser_heltal(
    *,
    value: Any,
    field_name: str,
) -> int:
    """Normaliserer en værdi til et heltal."""
    if isinstance(value, bool):
        raise WorkItemError(
            f"{field_name} må ikke være boolsk."
        )

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not value.is_integer():
            raise WorkItemError(
                f"{field_name} indeholder decimaler."
            )
        return int(value)

    if isinstance(value, str):
        normalized_value = value.strip()

        if normalized_value.endswith(".0"):
            normalized_value = normalized_value[:-2]

        if normalized_value.lstrip("-").isdigit():
            return int(normalized_value)

    raise WorkItemError(
        f"{field_name} havde et ugyldigt format. "
        f"Modtog: {value!r}."
    )


def _normaliser_tekst(
    value: str,
) -> str:
    """Normaliserer tekst til sammenligning."""
    return " ".join(
        str(value).strip().split()
    ).casefold()


def _normaliser_feltnavn(
    value: str,
) -> str:
    """Normaliserer et feltnavn til sammenligning."""
    normalized_value = value.strip().casefold()

    for character in (
        "_",
        "-",
        ".",
        ":",
    ):
        normalized_value = normalized_value.replace(
            character,
            " ",
        )

    return " ".join(
        normalized_value.split()
    )


def _valider_session_og_side(
    *,
    session: BrowserSession,
    page: Page,
) -> None:
    """Kontrollerer objekterne fra main.py."""
    if session is None:
        raise WorkItemError(
            "Browsersessionen mangler."
        )

    if page is None:
        raise WorkItemError(
            "Playwright-siden mangler."
        )

    if page.is_closed():
        raise WorkItemError(
            "Playwright-siden er lukket."
        )


__all__ = [
    "States",
    "behandel_page",
]
