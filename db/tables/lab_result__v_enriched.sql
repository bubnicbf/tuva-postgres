-- db/tables/lab_result__v_enriched.sql
-- Adds friendly labels and dictionary descriptions for order/component codes.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_lab_result_enriched AS
SELECT
  lr.*,
  ot.display   AS source_order_type_display,
  nt.display   AS normalized_order_type_display,
  od.description AS source_order_description_term,
  nd.description AS normalized_order_description_term,
  cd.description AS source_component_description_term,
  cnd.description AS normalized_component_description_term
FROM :"schema".lab_result lr
LEFT JOIN :"terminology_schema".lab_order_code_type ot
       ON lr.source_order_type = ot.code
LEFT JOIN :"terminology_schema".lab_order_code_type nt
       ON lr.normalized_order_type = nt.code
LEFT JOIN :"terminology_schema".lab_order_code od
       ON lr.source_order_type = od.code_type
      AND lr.source_order_code = od.code
LEFT JOIN :"terminology_schema".lab_order_code nd
       ON lr.normalized_order_type = nd.code_type
      AND lr.normalized_order_code = nd.code
LEFT JOIN :"terminology_schema".lab_component_code cd
       ON lr.source_component_code = cd.code
LEFT JOIN :"terminology_schema".lab_component_code cnd
       ON lr.normalized_component_code = cnd.code;
