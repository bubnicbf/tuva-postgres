-- Example indexes
CREATE INDEX IF NOT EXISTS enc_patient_idx ON tuva.encounter (patient_id);
CREATE INDEX IF NOT EXISTS claim_payer_idx ON tuva.claim (payer_id);
CREATE INDEX IF NOT EXISTS line_claim_idx ON tuva.claim_line (claim_id);

-- Optional: check constraints you know hold for seed data
ALTER TABLE tuva.claim_line
  ADD CONSTRAINT nonneg_line_amount CHECK (charge_amt >= 0 AND paid_amt >= 0);

-- Example derived views (nice for analysts)
CREATE OR REPLACE VIEW tuva.v_claim_summary AS
SELECT
  c.claim_id,
  c.encounter_id,
  c.payer_id,
  coalesce(sum(cl.charge_amt),0) AS sum_charge,
  coalesce(sum(cl.paid_amt),0)   AS sum_paid
FROM tuva.claim c
LEFT JOIN tuva.claim_line cl USING (claim_id)
GROUP BY 1,2,3;
