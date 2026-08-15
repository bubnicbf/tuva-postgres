-- db/terminology/ms_drg_weights_los.sql
-- Terminology: MS-DRG weights and LOS by fiscal year
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".ms_drg_weights_los (
  ms_drg                 varchar NOT NULL,  -- MS-DRG code
  fiscal_year            varchar NOT NULL,  -- FY as published
  final_post_acute_drg   varchar,
  final_special_pay_drg  varchar,
  mdc                    varchar,           -- MDC code (as published)
  type                   varchar,           -- DRG type (as published)
  ms_drg_title           varchar,
  drg_weight_raw         varchar,
  drg_weight             varchar,
  geometric_mean_los     varchar,
  arithmetic_mean_los    varchar,
  CONSTRAINT ms_drg_weights_los_pk PRIMARY KEY (ms_drg, fiscal_year)
);

-- Helpful lookups
CREATE INDEX IF NOT EXISTS ms_drg_wlos_fy_idx   ON :"terminology_schema".ms_drg_weights_los (fiscal_year);
CREATE INDEX IF NOT EXISTS ms_drg_wlos_mdc_idx  ON :"terminology_schema".ms_drg_weights_los (mdc);

-- Docs
COMMENT ON TABLE  :"terminology_schema".ms_drg_weights_los IS
  'MS-DRG weights/length-of-stay values by fiscal year (as published).';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.ms_drg                IS 'The Medicare Severity DRG code.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.fiscal_year           IS 'Fiscal year for the DRG weight and LOS values.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.final_post_acute_drg  IS 'Flag if final post-acute DRG for the year.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.final_special_pay_drg IS 'Flag if final special pay DRG for the year.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.mdc                   IS 'MDC code associated with the MS-DRG.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.type                  IS 'Published DRG type.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.ms_drg_title          IS 'MS-DRG title.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.drg_weight_raw        IS 'Raw DRG weight (as published).';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.drg_weight            IS 'Adjusted DRG weight (as published).';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.geometric_mean_los    IS 'Geometric mean LOS.';
COMMENT ON COLUMN :"terminology_schema".ms_drg_weights_los.arithmetic_mean_los   IS 'Arithmetic mean LOS.';
