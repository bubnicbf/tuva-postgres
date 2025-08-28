-- Appointment type codes (normalized or source catalog identifiers).
CREATE TABLE IF NOT EXISTS :"terminology_schema".appointment_type_code (
  code    varchar PRIMARY KEY,   -- e.g., 'NEW_PATIENT','FOLLOW_UP','WELLNESS','TELEHEALTH','PROCEDURE','IMMUNIZATION','LAB'
  display varchar NOT NULL,
  system  varchar                -- optional URI/URN
);
