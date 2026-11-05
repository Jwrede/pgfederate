CREATE TABLE products (id INT PRIMARY KEY, title VARCHAR(100) NOT NULL, price DECIMAL(10, 2) NOT NULL);
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL,
    ordered_at DATETIME NOT NULL
);
INSERT INTO products VALUES (1, 'Widget', 9.50), (2, 'Gadget', 25.00);
INSERT INTO orders VALUES
    (1, 1, 1, 3, '2026-03-01 08:00:00'),
    (2, 1, 2, 1, '2026-03-15 12:00:00'),
    (3, 2, 2, 2, '2026-04-02 09:30:00'),
    (4, 3, 1, 1, '2026-04-20 17:15:00');
