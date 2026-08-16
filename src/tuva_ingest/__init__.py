"""tuva_ingest: extract-load connector for the maintained Tuva workflow.

Pipeline (see README.md "Architecture"):

  API manifest -> authenticated artifact downloads -> immutable raw
  snapshot directory -> checksum/completeness validation -> operational
  migrations -> raw-schema load (eligibility, medical_claim,
  pharmacy_claim) -> dbt (staging -> Tuva Input Layer) -> the pinned
  ``tuva-health/the_tuva_project`` package (release 0.18.0), which owns
  and builds Tuva's core/terminology/output data model.

This package never creates, writes to, or reproduces Tuva-managed core,
terminology, or output schemas -- it only ever writes to the configured
raw warehouse schema and its own operational control schema. Mapping raw
data into the Tuva Input Layer, and everything downstream of that, is a
dbt concern (see models/), not a Python concern.
"""

__version__ = "0.1.0"
