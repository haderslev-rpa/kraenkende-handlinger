"""Samlet konfiguration til processen krænkende-handlinger.

Alle konfigurerbare procesinputs er samlet i denne fil.
main.py, populate_queue.py og behandel.py importerer værdierne herfra.
Der læses ikke fra .env eller miljøvariabler.
"""

from __future__ import annotations

from datetime import UTC, datetime

# ------------------------------------------------------------
# AUTOMATION SERVER
# ------------------------------------------------------------

QUEUE_ID: int = 14


# ------------------------------------------------------------
# BROWSER
# ------------------------------------------------------------

HEADLESS: bool = True
DEBUG_HEADLESS: bool = False
QUEUE_HEADLESS: bool = True

# Bevarer originalens adfaerd: --queue sletter alle NEW-items foer fyldning.
# Saet False for at bevare eksisterende NEW-items.
CLEAR_NEW_ITEMS_BEFORE_POPULATE: bool = True


# ------------------------------------------------------------
# MAIL
# ------------------------------------------------------------

MAILAFSENDER = "dirxfb@haderslev.dk"
MAILMODTAGER = "forsikring@haderslev.dk"
MAIL_EMNE_PREFIX = "Skade nr"

MAILTEKST_COMPENSATION = (
    "Vurderes efter arbejdsskadeloven? har ikke den forventede "
    "værdi af krav 2\n"
    "Robot vil ikke længere behandle dette skade nr"
)

MAILTEKST_UARBEJDSDYGTIGHED = (
    "Forventet fravær er ikke som forventet.\n"
    "Robot vil ikke længere behandle dette skade nr"
)

MAILTEKST_BEGGE_KRAV = (
    "Vurderes efter arbejdsskadeloven? har ikke den forventede "
    "værdi af krav 2\n"
    "Forventet fravær er ikke som forventet.\n"
    "Robot vil ikke længere behandle dette skade nr"
)


# ------------------------------------------------------------
# WORK ITEM-STATUS
# ------------------------------------------------------------

STATUS_COMPLETED = "Completed"
STATUS_CODE_COMPLETED = "Færdig"
STATUS_MANUEL = "Manuel"
STATUS_CODE_MANUEL = "Manuel"


# ------------------------------------------------------------
# FORRETNINGSREGLER
# ------------------------------------------------------------

FORVENTET_COMPENSATION_ID = 0
FORVENTET_ACCIDENT_DURATION_TEXT = "Uarbejdsdygtighed mindre end 1 dag"

AFSLUTTET_STATUS = "Afsluttet"
KRAENKENDE_HANDLING_UNDERTYPE = "Krænkende handling"


# ------------------------------------------------------------
# POPULATE QUEUE, INSUBIZ-FILTRE
# ------------------------------------------------------------

CURRENT_YEAR = datetime.now(UTC).year

CUSTOMER_ID: int | None = None
CUSTOMER_SEGMENTATION_1 = -1
CUSTOMER_SEGMENTATION_2 = -1
CLAIM_GROUP_ID = 0
STATUS_ID = -2

CREATED_YEAR_FROM = 0
CREATED_YEAR_TO = CURRENT_YEAR
INCIDENT_YEAR_FROM = CURRENT_YEAR - 1
INCIDENT_YEAR_TO = CURRENT_YEAR
SHOW_TREE_DATA = False

QUEUE_LOOKBACK_START = "2025-07-01T00:00:00Z"


# ------------------------------------------------------------
# INSUBIZ-EKSPORTKOLONNER
# ------------------------------------------------------------

SKADER_LISTE_COLUMNS = [
    "Id",
    "IncidentNumberInternal",
    "IncidentSubType",
    "IncidentStatus",
]


# ------------------------------------------------------------
# INSUBIZ-FELTALIASER
# ------------------------------------------------------------

SKADE_ID_FELTER = (
    "Skade id",
    "Skade-id",
    "Skade_id",
    "Id",
    "IncidentId",
)

SKADE_NR_FELTER = (
    "Skadenr.",
    "Skadenr",
    "Skade nr.",
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


# ------------------------------------------------------------
# BOX-FELTNAVNE
# ------------------------------------------------------------

BOX_SKADE_ID = "Skade_id"
BOX_SKADE_NR = "Skade_nr"
BOX_UNDERTYPE = "Undertype"
BOX_STATUS = "Status"


__all__ = [
    "AFSLUTTET_STATUS",
    "BOX_SKADE_ID",
    "BOX_SKADE_NR",
    "BOX_STATUS",
    "BOX_UNDERTYPE",
    "CLAIM_GROUP_ID",
    "CREATED_YEAR_FROM",
    "CREATED_YEAR_TO",
    "CURRENT_YEAR",
    "CUSTOMER_ID",
    "CUSTOMER_SEGMENTATION_1",
    "CUSTOMER_SEGMENTATION_2",
    "FORVENTET_ACCIDENT_DURATION_TEXT",
    "FORVENTET_COMPENSATION_ID",
    "HEADLESS",
    "DEBUG_HEADLESS",
    "QUEUE_HEADLESS",
    "CLEAR_NEW_ITEMS_BEFORE_POPULATE",
    "INCIDENT_YEAR_FROM",
    "INCIDENT_YEAR_TO",
    "KRAENKENDE_HANDLING_UNDERTYPE",
    "MAILAFSENDER",
    "MAILMODTAGER",
    "MAIL_EMNE_PREFIX",
    "MAILTEKST_BEGGE_KRAV",
    "MAILTEKST_COMPENSATION",
    "MAILTEKST_UARBEJDSDYGTIGHED",
    "QUEUE_ID",
    "QUEUE_LOOKBACK_START",
    "SHOW_TREE_DATA",
    "SKADE_ID_FELTER",
    "SKADE_NR_FELTER",
    "SKADER_LISTE_COLUMNS",
    "STATUS_CODE_COMPLETED",
    "STATUS_CODE_MANUEL",
    "STATUS_COMPLETED",
    "STATUS_FELTER",
    "STATUS_ID",
    "STATUS_MANUEL",
    "UNDERTYPE_FELTER",
]
