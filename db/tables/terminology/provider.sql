-- db/tables/provider.sql
-- Uses psql var :"schema" (passed in by the wrapper) for portability.

CREATE TABLE IF NOT EXISTS :"schema".provider (
    npi                                               varchar PRIMARY KEY,
    entity_type_code                                  varchar,
    entity_type_description                           varchar,
    primary_taxonomy_code                             varchar,
    primary_specialty_description                     varchar,
    provider_first_name                               varchar,
    provider_last_name                                varchar,
    provider_credential                               varchar,
    provider_organization_name                        varchar,
    provider_other_organization_name                  varchar,
    provider_other_organization_name_type_code        varchar,
    provider_other_organization_name_type_description varchar,
    parent_organization_name                          varchar,
    practice_address_line_1                           varchar,
    practice_address_line_2                           varchar,
    practice_city                                     varchar,
    practice_state                                    varchar,
    practice_zip_code                                 varchar,
    mailing_telephone_number                          varchar,
    location_telephone_number                         varchar,
    official_telephone_number                         varchar,
    last_updated                                      date,
    deactivation_date                                 date,
    deactivation_flag                                 varchar
);
