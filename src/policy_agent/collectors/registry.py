from __future__ import annotations

from .bc_orders import BCOrdersCollector
from .federal_regulatory_plans import FederalRegulatoryPlansCollector
from .bc_eao import BCEAOMilestonesCollector
from .iaac import IAACMilestonesCollector
from .house_reports import HouseCommitteeReportsCollector
from .bcuc_anticipated_pdf import BCUCAnticipatedPDFCollector
from .bc_laws import BCLawsRegulationsCollector
from .bc_legislature import BCLegislatureCalendarCollector
from .bcuc import BCUCAnticipatedFilingsCollector, BCUCDeadlinesCollector, BCUCProceedingsCollector
from .consultations import FederalConsultationsCollector, GovTogetherCollector
from .html_date_list import HTMLDateListCollector
from .json_feed import JSONFeedCollector
from .page_watch import PageWatchCollector
from .parliament import HouseCommitteeCollector
from .rss import RSSCollector
from .statcan import StatCanScheduleCollector

COLLECTORS = {
    "bc_orders": BCOrdersCollector,
    "federal_regulatory_plans": FederalRegulatoryPlansCollector,
    "bc_eao_milestones": BCEAOMilestonesCollector,
    "iaac_milestones": IAACMilestonesCollector,
    "house_committee_reports": HouseCommitteeReportsCollector,
    "bcuc_anticipated_pdf": BCUCAnticipatedPDFCollector,
    "rss": RSSCollector,
    "json_feed": JSONFeedCollector,
    "statcan_schedule": StatCanScheduleCollector,
    "bcuc_deadlines": BCUCDeadlinesCollector,
    "bcuc_anticipated": BCUCAnticipatedFilingsCollector,
    "bcuc_proceedings": BCUCProceedingsCollector,
    "federal_consultations": FederalConsultationsCollector,
    "govtogether": GovTogetherCollector,
    "bc_laws_regulations": BCLawsRegulationsCollector,
    "bc_legislature_calendar": BCLegislatureCalendarCollector,
    "house_committees": HouseCommitteeCollector,
    "page_watch": PageWatchCollector,
    "html_date_list": HTMLDateListCollector,
}


def get_collector(name: str):
    try:
        return COLLECTORS[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown collector: {name}") from exc
