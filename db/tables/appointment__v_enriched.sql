-- db/tables/appointment__v_enriched.sql (patch)
CREATE OR REPLACE VIEW :"schema".v_appointment_enriched AS
SELECT
  a.*,
  -- Appointment type descriptions from new terminology table
  tat.description AS source_type_description_term,
  nat.description AS normalized_type_description_term,

  -- Status (code-only set): expose booleans for presence
  (st.code  IS NOT NULL) AS source_status_known,
  (nst.code IS NOT NULL) AS normalized_status_known,

  rct.display  AS source_reason_code_type_display,
  nrct.display AS normalized_reason_code_type_display,

  scr.description  AS source_cancellation_reason_description_term,
  ncr.description  AS normalized_cancellation_reason_description_term
FROM :"schema".appointment a
LEFT JOIN :"terminology_schema".appointment_type tat
       ON a.source_appointment_type_code = tat.code
LEFT JOIN :"terminology_schema".appointment_type nat
       ON a.normalized_appointment_type_code = nat.code
LEFT JOIN :"terminology_schema".appointment_status st
       ON a.source_status = st.code
LEFT JOIN :"terminology_schema".appointment_status nst
       ON a.normalized_status = nst.code
LEFT JOIN :"terminology_schema".appointment_reason_code_type rct
       ON a.source_reason_code_type = rct.code
LEFT JOIN :"terminology_schema".appointment_reason_code_type nrct
       ON a.normalized_reason_code_type = nrct.code
LEFT JOIN :"terminology_schema".appointment_cancellation_reason scr
       ON a.source_cancellation_reason_code = scr.code
LEFT JOIN :"terminology_schema".appointment_cancellation_reason ncr
       ON a.normalized_cancellation_reason_code = ncr.code;
