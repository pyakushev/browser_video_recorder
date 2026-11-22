CREATE TABLE IF NOT EXISTS companies (
    id                        SERIAL PRIMARY KEY,
    company_name              TEXT NOT NULL UNIQUE,
    language                  TEXT,
    country                   TEXT,
    postcode                  TEXT,
    city                      TEXT,
    street                    TEXT,
    house_no                  TEXT,
    web                       TEXT,                             -- company home page URL
    status                    INTEGER,                          -- NULL = not yet processed
    impressum_url             TEXT,                             -- resolved Impressum/Imprint URL
    legal_name                TEXT,                             -- extracted company name
    postal_address            TEXT,                             -- extracted postal address
    handelsregister           TEXT,                             -- trade registry number (HRB/HRA)
    vat_number                TEXT,                             -- VAT id (USt-IdNr., DE...)
    phone                     TEXT,
    email                     TEXT,
    responsible_person        TEXT,                             -- e.g. Geschäftsführer
    raw_json                  JSONB,                            -- full structured DeepSeek response
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS companies_status_idx ON companies (status);
