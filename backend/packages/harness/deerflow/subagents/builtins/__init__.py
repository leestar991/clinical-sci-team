"""Built-in subagent configurations."""

from .bash_agent import BASH_AGENT_CONFIG
from .bioinformatics import BIOINFORMATICS_CONFIG
from .chemistry import CHEMISTRY_CONFIG
from .clinical_ops import CLINICAL_OPS_CONFIG
from .cmo_gpl import CMO_GPL_CONFIG
from .data_extractor import DATA_EXTRACTOR_CONFIG
from .data_management import DATA_MANAGEMENT_CONFIG
from .drug_registration import DRUG_REGISTRATION_CONFIG
from .general_purpose import GENERAL_PURPOSE_CONFIG
from .gpm import GPM_CONFIG
from .literature_analyzer import LITERATURE_ANALYZER_CONFIG
from .ov_retriever import OV_RETRIEVER_CONFIG
from .parkinson_clinical import PARKINSON_CLINICAL_CONFIG
from .pharmacology import PHARMACOLOGY_CONFIG
from .quality_control import QUALITY_CONTROL_CONFIG
from .report_writer import REPORT_WRITER_CONFIG
from .report_writing import REPORT_WRITING_CONFIG
from .toxicology import TOXICOLOGY_CONFIG
from .trial_design import TRIAL_DESIGN_CONFIG
from .trial_statistics import TRIAL_STATISTICS_CONFIG

__all__ = [
    "GENERAL_PURPOSE_CONFIG",
    "BASH_AGENT_CONFIG",
    "LITERATURE_ANALYZER_CONFIG",
    "DATA_EXTRACTOR_CONFIG",
    "REPORT_WRITER_CONFIG",
    "OV_RETRIEVER_CONFIG",
    # Clinical development team
    "CMO_GPL_CONFIG",
    "GPM_CONFIG",
    "PARKINSON_CLINICAL_CONFIG",
    "TRIAL_DESIGN_CONFIG",
    "TRIAL_STATISTICS_CONFIG",
    "DATA_MANAGEMENT_CONFIG",
    "DRUG_REGISTRATION_CONFIG",
    "PHARMACOLOGY_CONFIG",
    "TOXICOLOGY_CONFIG",
    "CHEMISTRY_CONFIG",
    "BIOINFORMATICS_CONFIG",
    "CLINICAL_OPS_CONFIG",
    "QUALITY_CONTROL_CONFIG",
    "REPORT_WRITING_CONFIG",
]

# Registry of built-in subagents
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
    "literature-analyzer": LITERATURE_ANALYZER_CONFIG,
    "data-extractor": DATA_EXTRACTOR_CONFIG,
    "report-writer": REPORT_WRITER_CONFIG,
    "ov-retriever": OV_RETRIEVER_CONFIG,
    # Virtual Clinical Development Team
    "cmo-gpl": CMO_GPL_CONFIG,
    "gpm": GPM_CONFIG,
    "parkinson-clinical": PARKINSON_CLINICAL_CONFIG,
    "trial-design": TRIAL_DESIGN_CONFIG,
    "trial-statistics": TRIAL_STATISTICS_CONFIG,
    "data-management": DATA_MANAGEMENT_CONFIG,
    "drug-registration": DRUG_REGISTRATION_CONFIG,
    "pharmacology": PHARMACOLOGY_CONFIG,
    "toxicology": TOXICOLOGY_CONFIG,
    "chemistry": CHEMISTRY_CONFIG,
    "bioinformatics": BIOINFORMATICS_CONFIG,
    "clinical-ops": CLINICAL_OPS_CONFIG,
    "quality-control": QUALITY_CONTROL_CONFIG,
    "report-writing": REPORT_WRITING_CONFIG,
}
