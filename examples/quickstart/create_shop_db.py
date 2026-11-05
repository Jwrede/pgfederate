"""Create the small SQLite database the quick start reads."""

import sqlite3

db = sqlite3.connect("shop.db")
db.executescript(
    """
    DROP TABLE IF EXISTS customers;
    CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT);
    INSERT INTO customers VALUES (1, 'Acme', 'DE'), (2, 'Globex', 'US'), (3, 'Initech', 'DE');
    """
)
db.commit()
