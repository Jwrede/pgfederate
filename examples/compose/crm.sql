CREATE TABLE regions (id int PRIMARY KEY, name text NOT NULL);
CREATE TABLE customers (
    id int PRIMARY KEY,
    name text NOT NULL,
    region_id int REFERENCES regions (id),
    signed_up timestamptz NOT NULL
);
INSERT INTO regions VALUES (1, 'EMEA'), (2, 'Americas');
INSERT INTO customers VALUES
    (1, 'Acme', 1, '2026-01-10 09:00:00+00'),
    (2, 'Globex', 2, '2026-02-14 10:30:00+00'),
    (3, 'Initech', 1, '2026-03-03 16:45:00+00');
