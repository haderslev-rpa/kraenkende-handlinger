from __future__ import annotations

"""
Fylder Automation Server-køen med relevante skader fra Insubiz.

Work item-strukturen følger processtandarden. Domænedata placeres kun i
box, herunder både det tekniske skade-id og det læsevenlige skadenummer:

    {
        "box": {
            "Skade_id": 2488985,
            "Skade_nr": "2026-001234",
            "Undertype": "Krænkende handling",
            "Status": "Ny"
        },
        "defer": null,
        "state": [],
        "status": {}
    }

Skade_id anvendes som work item-reference. Skade_nr er skadens interne,
læsevenlige nummer fra IncidentNumberInternal.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from q_haderslev_vbo.automation_server.ats_is_item_in_queue import (
    is_item_in_queue,
)
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_insubiz.api_client import create_api_client
from q_insubiz.functionality.skader import SKADER_LISTE
from q_insubiz.utils import normalize_positive_id


logger = logging.getLogger(__name__)


# --------------------------------------------------
# Standardværdier
# --------------------------------------------------

CURRENT_YEAR = datetime.now(timezone.utc).year
QUEUE_LOOKBACK_START = "2025-07-01T00:00:00Z"

AFSLUTTET_STATUS = "Afsluttet"
KRAENKENDE_HANDLING_UNDERTYPE = "Krænkende handling"


# --------------------------------------------------
# Insubiz-kolonner
# --------------------------------------------------

SKADER_LISTE_COLUMNS = [
    "Id",
    "IncidentNumberInternal",
    "IncidentSubType",
    "IncidentStatus",
]


# --------------------------------------------------
# Feltaliaser
# --------------------------------------------------

SKADE_ID_FELTER = (
    "Skade id",
    "Skade-id",
    "Skade_id",
    "Id",
    "IncidentId",
)

SKADE_NR_FELTER = (
    "Skade nr",
    "Skade-nr",
    "Skade_nr",
    "Skadenummer",
    "Internt skadenummer",
    "IncidentNumberInternal",
    "Incident number internal",
)

UNDERTYPE_FELTER = (
    "Undertype",
    "IncidentSubType",
    "Incident subtype",
)

STATUS_FELTER = (
    "Status",
    "IncidentStatus",
    "Incident status",
)


# --------------------------------------------------
# Feltstandarder i box
# --------------------------------------------------

BOX_SKADE_ID = "Skade_id"
BOX_SKADE_NR = "Skade_nr"
BOX_UNDERTYPE = "Undertype"
BOX_STATUS = "Status"


# --------------------------------------------------
# Normalisering
# --------------------------------------------------

def _normaliser_feltnavn(value: str) -> str:
    """Normaliserer et kolonnenavn til robust sammenligning."""
    if not isinstance(value, str):
        raise TypeError(
            "Feltnavnet skal være tekst. "
            f"Modtog: {type(value).__name__}."
        )

    normalized_value = value.strip().casefold()

    for character in ("_", "-", ".", ":"):
        normalized_value = normalized_value.replace(character, " ")

    return " ".join(normalized_value.split())


def _normaliser_tekst(value: str) -> str:
    """Normaliserer tekst til sammenligning."""
    return " ".join(str(value).strip().split()).casefold()


def _tekster_er_ens(value: str, expected: str) -> bool:
    """Sammenligner tekst uden betydning af bogstavstørrelse og mellemrum."""
    return _normaliser_tekst(value) == _normaliser_tekst(expected)


def _normaliser_paakraevet_tekst(*, name: str, value: str) -> str:
    """Validerer og normaliserer en obligatorisk tekstværdi."""
    if not isinstance(name, str):
        raise TypeError("name skal være tekst.")

    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("name må ikke være tom.")

    if not isinstance(value, str):
        raise TypeError(
            f"{normalized_name} skal være tekst. "
            f"Modtog: {type(value).__name__}."
        )

    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"{normalized_name} må ikke være tom.")

    return normalized_value


def _utc_timestamp() -> str:
    """Returnerer aktuelt UTC-tidspunkt i ATS-kompatibelt ISO-format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------
# Feltopslag
# --------------------------------------------------

def _find_feltnavn(
    *,
    skade: dict[str, Any],
    feltnavne: Iterable[str],
) -> str | None:
    """Finder det faktiske feltnavn via en samling aliaser."""
    if not isinstance(skade, dict):
        raise TypeError(
            "skade skal være en dictionary. "
            f"Modtog: {type(skade).__name__}."
        )

    if isinstance(feltnavne, (str, bytes)):
        raise TypeError("feltnavne skal være en samling af tekstværdier.")

    normaliserede_felter = {
        _normaliser_feltnavn(faktisk_feltnavn): faktisk_feltnavn
        for faktisk_feltnavn in skade
        if isinstance(faktisk_feltnavn, str)
    }

    for feltnavn in feltnavne:
        if not isinstance(feltnavn, str):
            raise TypeError("Alle værdier i feltnavne skal være tekst.")

        faktisk_feltnavn = normaliserede_felter.get(
            _normaliser_feltnavn(feltnavn)
        )
        if faktisk_feltnavn is not None:
            return faktisk_feltnavn

    return None


def _hent_feltvaerdi(
    *,
    skade: dict[str, Any],
    feltnavne: Iterable[str],
    felttype: str,
    paakraevet: bool = True,
    standardvaerdi: Any = None,
) -> Any:
    """Henter en værdi via en samling mulige feltnavne."""
    if not isinstance(felttype, str):
        raise TypeError("felttype skal være tekst.")

    normalized_felttype = felttype.strip()
    if not normalized_felttype:
        raise ValueError("felttype må ikke være tom.")

    aliaser = tuple(feltnavne)
    if not aliaser:
        raise ValueError("feltnavne må ikke være tom.")

    faktisk_feltnavn = _find_feltnavn(
        skade=skade,
        feltnavne=aliaser,
    )

    if faktisk_feltnavn is None:
        if not paakraevet:
            return standardvaerdi
        raise RuntimeError(
            "Skadelisten mangler et forventet felt. "
            f"Felttype: {normalized_felttype!r}. "
            f"Forventede et af: {list(aliaser)!r}. "
            f"Tilgængelige felter: {list(skade.keys())!r}."
        )

    value = skade.get(faktisk_feltnavn)
    if value is None:
        if not paakraevet:
            return standardvaerdi
        raise RuntimeError(
            "Skadelisten indeholder en tom værdi. "
            f"Felttype: {normalized_felttype!r}. "
            f"Felt: {faktisk_feltnavn!r}."
        )

    return value


def _hent_tekstvaerdi(
    *,
    skade: dict[str, Any],
    feltnavne: Iterable[str],
    felttype: str,
    paakraevet: bool = True,
    standardvaerdi: str = "",
) -> str:
    """Henter en feltværdi og returnerer den som tekst."""
    value = _hent_feltvaerdi(
        skade=skade,
        feltnavne=feltnavne,
        felttype=felttype,
        paakraevet=paakraevet,
        standardvaerdi=standardvaerdi,
    )

    if value is None:
        if paakraevet:
            raise RuntimeError(
                "Skadelisten indeholder en tom tekstværdi. "
                f"Felttype: {felttype!r}."
            )
        return standardvaerdi

    if isinstance(value, dict):
        nested_value = (
            value.get("text")
            or value.get("name")
            or value.get("value")
            or value.get("id")
        )
        if nested_value is None:
            if paakraevet:
                raise RuntimeError(
                    "Skadelistens felt er et objekt uden text, name, "
                    f"value eller id. Felttype: {felttype!r}. "
                    f"Værdi: {value!r}."
                )
            return standardvaerdi
        value = nested_value

    normalized_value = str(value).strip()
    if not normalized_value:
        if paakraevet:
            raise RuntimeError(
                "Skadelisten indeholder en tom tekstværdi. "
                f"Felttype: {felttype!r}."
            )
        return standardvaerdi

    return normalized_value


def _hent_skade_id(*, skade: dict[str, Any]) -> int:
    """Henter og normaliserer skadens interne id."""
    raw_skade_id = _hent_feltvaerdi(
        skade=skade,
        feltnavne=SKADE_ID_FELTER,
        felttype="skade-id",
    )

    try:
        return normalize_positive_id(name="skade_id", value=raw_skade_id)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Skadelistens skade-id er ugyldigt. "
            f"Modtog: {raw_skade_id!r}."
        ) from error


def _hent_skade_nr(*, skade: dict[str, Any]) -> str:
    """Henter skadens læsevenlige nummer som tekst."""
    return _hent_tekstvaerdi(
        skade=skade,
        feltnavne=SKADE_NR_FELTER,
        felttype="skadenummer",
    )


# --------------------------------------------------
# Work item-struktur
# --------------------------------------------------

def _opret_data_json() -> dict[str, Any]:
    """Opretter standardstrukturen. Domænedata placeres kun i box."""
    return {
        "box": {},
        "defer": None,
        "state": [],
        "status": {},
    }


def _opret_box_data(
    *,
    skade_id: int,
    skade_nr: str,
    undertype: str,
    status: str,
) -> dict[str, Any]:
    """Opretter domænedata til work itemets box."""
    return {
        BOX_STATUS: _normaliser_paakraevet_tekst(
            name="status",
            value=status,
        ),
        BOX_SKADE_ID: normalize_positive_id(
            name="skade_id",
            value=skade_id,
        ),
        BOX_SKADE_NR: _normaliser_paakraevet_tekst(
            name="skade_nr",
            value=skade_nr,
        ),
        BOX_UNDERTYPE: _normaliser_paakraevet_tekst(
            name="undertype",
            value=undertype,
        ),
    }


def _hent_box(*, data_json: dict[str, Any]) -> dict[str, Any]:
    """Returnerer den validerede box fra work item-data."""
    if not isinstance(data_json, dict):
        raise TypeError("data_json skal være en dictionary.")

    box = data_json.get("box")
    if not isinstance(box, dict):
        raise RuntimeError("data_json mangler en gyldig box.")

    return box


def _fjern_domaenefelter_fra_roden(*, data_json: dict[str, Any]) -> None:
    """Fjerner ældre domænefelter fra roden af work item-data."""
    root_fields = (
        "skade_id",
        "Skade_id",
        "Skade id",
        "Skade-id",
        "skade_nr",
        "Skade_nr",
        "Skade nr",
        "Skade-nr",
        "IncidentNumberInternal",
        "IncidentSubType",
        "IncidentStatus",
        "Undertype",
        "Status",
    )

    for field_name in root_fields:
        data_json.pop(field_name, None)


def _kontroller_data_json(*, data_json: dict[str, Any]) -> None:
    """Kontrollerer at domænedata kun findes i box."""
    box = _hent_box(data_json=data_json)

    forventede_box_felter = (
        BOX_SKADE_ID,
        BOX_SKADE_NR,
        BOX_UNDERTYPE,
        BOX_STATUS,
    )
    manglende_box_felter = [
        field_name
        for field_name in forventede_box_felter
        if field_name not in box
    ]

    if manglende_box_felter:
        raise RuntimeError(
            "Work itemets box mangler forventede felter. "
            f"Manglende: {manglende_box_felter!r}. "
            f"Tilgængelige: {list(box.keys())!r}."
        )

    normalize_positive_id(
        name="box.Skade_id",
        value=box[BOX_SKADE_ID],
    )
    _normaliser_paakraevet_tekst(
        name="box.Skade_nr",
        value=box[BOX_SKADE_NR],
    )

    forbidden_root_fields = (
        "skade_id",
        "Skade_id",
        "Skade id",
        "Skade-id",
        "skade_nr",
        "Skade_nr",
        "Skade nr",
        "Skade-nr",
        "IncidentNumberInternal",
    )
    for forbidden_field in forbidden_root_fields:
        if forbidden_field in data_json:
            raise RuntimeError(
                "Skade-id og skadenummer må kun findes i box. "
                f"Feltet {forbidden_field!r} findes på roden."
            )


def _hent_item_reference(*, data_json: dict[str, Any]) -> str:
    """Henter work item-reference fra box.Skade_id."""
    _kontroller_data_json(data_json=data_json)
    box = _hent_box(data_json=data_json)
    skade_id = normalize_positive_id(
        name="box.Skade_id",
        value=box[BOX_SKADE_ID],
    )
    return str(skade_id)


# --------------------------------------------------
# Fyld kø
# --------------------------------------------------

async def populate_queue(
    *,
    workqueue: Any,
    queue_id: int,
    customer_id: int | None = None,
    customer_segmentation_1: int = -1,
    customer_segmentation_2: int = -1,
    claim_group_id: int = 0,
    status_id: int = -2,
    created_year_from: int = 0,
    created_year_to: int = CURRENT_YEAR,
    incident_year_from: int = CURRENT_YEAR - 1,
    incident_year_to: int = CURRENT_YEAR,
    show_tree_data: bool = False,
) -> None:
    """
    Henter relevante skader fra Insubiz og opretter work items.

    Kun skader med undertypen Krænkende handling, som ikke har status
    Afsluttet, bliver tilføjet. Dubletter søges fra 1. juli 2025 til det
    aktuelle UTC-tidspunkt.
    """
    if workqueue is None:
        raise ValueError("workqueue må ikke være None.")

    normalized_queue_id = normalize_positive_id(
        name="queue_id",
        value=queue_id,
    )
    api_client = create_api_client()

    try:
        skader = await SKADER_LISTE(
            api_client=api_client,
            customer_id=customer_id,
            customer_segmentation_1=customer_segmentation_1,
            customer_segmentation_2=customer_segmentation_2,
            claim_group_id=claim_group_id,
            status_id=status_id,
            created_year_from=created_year_from,
            created_year_to=created_year_to,
            incident_year_from=incident_year_from,
            incident_year_to=incident_year_to,
            show_tree_data=show_tree_data,
            columns=SKADER_LISTE_COLUMNS,
        )

        if not isinstance(skader, list):
            raise RuntimeError(
                "SKADER_LISTE returnerede et uventet format. "
                f"Modtog: {type(skader).__name__}."
            )

        logger.info("Skadelisten blev hentet. Antal: %s.", len(skader))
        if skader:
            logger.info("Kolonner i skadelisten: %s.", list(skader[0].keys()))

        antal_tilfoejet = 0
        antal_dubletter = 0
        antal_filtreret = 0
        queue_lookup_end = _utc_timestamp()

        for row_number, skade in enumerate(skader, start=1):
            if not isinstance(skade, dict):
                raise RuntimeError(
                    "Skadelisten indeholder en ugyldig række. "
                    f"Række: {row_number}. Modtog: {type(skade).__name__}."
                )

            skade_id = _hent_skade_id(skade=skade)
            skade_nr = _hent_skade_nr(skade=skade)
            undertype = _hent_tekstvaerdi(
                skade=skade,
                feltnavne=UNDERTYPE_FELTER,
                felttype="undertype",
            )
            status = _hent_tekstvaerdi(
                skade=skade,
                feltnavne=STATUS_FELTER,
                felttype="status",
            )

            if _tekster_er_ens(status, AFSLUTTET_STATUS):
                antal_filtreret += 1
                logger.debug(
                    "Springer afsluttet skade over. Skade-id: %s.",
                    skade_id,
                )
                continue

            if not _tekster_er_ens(undertype, KRAENKENDE_HANDLING_UNDERTYPE):
                antal_filtreret += 1
                logger.debug(
                    "Springer skade med anden undertype over. "
                    "Skade-id: %s. Undertype: %s.",
                    skade_id,
                    undertype,
                )
                continue

            raw_item = _opret_box_data(
                skade_id=skade_id,
                skade_nr=skade_nr,
                undertype=undertype,
                status=status,
            )
            data_json = _opret_data_json()

            update_item_data(
                data_json,
                box_updates=raw_item,
                update=False,
            )
            _fjern_domaenefelter_fra_roden(data_json=data_json)
            item_reference = _hent_item_reference(data_json=data_json)

            if is_item_in_queue(
                queue_id=normalized_queue_id,
                item_reference=item_reference,
                new=True,
                in_progress=True,
                completed=True,
                pending_user_action=True,
                start_datetime=QUEUE_LOOKBACK_START,
                end_datetime=queue_lookup_end,
                updated_at=False,
            ):
                antal_dubletter += 1
                logger.info(
                    "Springer eksisterende item over. Række: %s. "
                    "Skade-id: %s. Skadenummer: %s. Reference: %s.",
                    row_number,
                    skade_id,
                    skade_nr,
                    item_reference,
                )
                print(
                    "Springer over: Item med reference "
                    f"{item_reference!r} findes allerede. "
                    f"Skadenummer: {skade_nr!r}."
                )
                continue

            workqueue.add_item(
                data=data_json,
                reference=item_reference,
            )
            antal_tilfoejet += 1

            logger.info(
                "Skade blev tilføjet. Række: %s. Skade-id: %s. "
                "Skadenummer: %s. Reference: %s. Undertype: %s. Status: %s.",
                row_number,
                skade_id,
                skade_nr,
                item_reference,
                undertype,
                status,
            )
            print(
                "Tilføjet til kø: "
                f"Reference {item_reference!r}, "
                f"skadenummer {skade_nr!r}, "
                f"undertype {undertype!r}, status {status!r}."
            )

        logger.info(
            "Køoprettelsen er afsluttet. Hentet: %s. Tilføjet: %s. "
            "Dubletter: %s. Filtreret: %s.",
            len(skader),
            antal_tilfoejet,
            antal_dubletter,
            antal_filtreret,
        )

        print()
        print("=" * 80)
        print("KØOPRETTELSE AFSLUTTET")
        print("=" * 80)
        print(f"Antal hentet: {len(skader)}")
        print(f"Antal tilføjet: {antal_tilfoejet}")
        print(f"Antal dubletter: {antal_dubletter}")
        print(f"Antal filtreret fra: {antal_filtreret}")
        print("=" * 80)
    finally:
        await api_client.close()


__all__ = [
    "populate_queue",
]
