PRAGMA foreign_keys = ON;

BEGIN;

INSERT INTO
    users (
        name,
        email,
        password_hash,
        phone
    )
VALUES (
        'Rahim Ahmed',
        'rahim@gmail.com',
        'hash_rahim',
        '01711111111'
    ),
    (
        'Karim Hasan',
        'karim@gmail.com',
        'hash_karim',
        '01822222222'
    ),
    (
        'Nusrat Jahan',
        'nusrat@gmail.com',
        'hash_nusrat',
        '01933333333'
    ),
    (
        'Sakib Khan',
        'sakib@gmail.com',
        'hash_sakib',
        '01644444444'
    );

INSERT INTO
    admins (
        name,
        email,
        password_hash,
        role
    )
VALUES (
        'Admin User',
        'admin@example.com',
        'admin_hash',
        'admin'
    ),
    (
        'Manager',
        'manager@example.com',
        'manager_hash',
        'manager'
    );

INSERT INTO
    categories (name, description)
VALUES (
        'Electronics',
        'Electronic devices and accessories'
    ),
    (
        'Clothing',
        'Men''s and women''s clothing'
    ),
    (
        'Books',
        'Books and educational materials'
    ),
    (
        'Home & Kitchen',
        'Home and kitchen products'
    ),
    (
        'Sports',
        'Sports and fitness products'
    );

INSERT INTO
    products (
        category_id,
        name,
        description,
        price,
        discount_price,
        stock,
        sku,
        image_url
    )
VALUES (
        1,
        'Wireless Headphones',
        'Bluetooth wireless headphones with noise cancellation',
        3500,
        2999,
        50,
        'ELEC-001',
        'headphones.jpg'
    ),
    (
        1,
        'Mechanical Keyboard',
        'RGB mechanical keyboard',
        4500,
        3999,
        30,
        'ELEC-002',
        'keyboard.jpg'
    ),
    (
        1,
        'Wireless Mouse',
        'Ergonomic wireless mouse',
        1500,
        1200,
        80,
        'ELEC-003',
        'mouse.jpg'
    ),
    (
        2,
        'Cotton T-Shirt',
        'Premium cotton t-shirt',
        800,
        699,
        100,
        'CLO-001',
        'tshirt.jpg'
    ),
    (
        2,
        'Denim Jeans',
        'Classic blue denim jeans',
        2200,
        1999,
        40,
        'CLO-002',
        'jeans.jpg'
    ),
    (
        3,
        'Python Programming Book',
        'Beginner to advanced Python programming',
        1200,
        999,
        25,
        'BOOK-001',
        'python.jpg'
    ),
    (
        3,
        'Database Systems',
        'Introduction to database management systems',
        1500,
        1300,
        20,
        'BOOK-002',
        'database.jpg'
    ),
    (
        4,
        'Coffee Maker',
        'Automatic coffee maker',
        5500,
        4999,
        15,
        'HOME-001',
        'coffee-maker.jpg'
    ),
    (
        5,
        'Yoga Mat',
        'Non-slip exercise yoga mat',
        1200,
        999,
        60,
        'SPORT-001',
        'yoga-mat.jpg'
    );

INSERT INTO
    product_images (
        product_id,
        image_url,
        sort_order
    )
VALUES (1, 'headphones-front.jpg', 1),
    (1, 'headphones-side.jpg', 2),
    (2, 'keyboard-front.jpg', 1),
    (4, 'tshirt-front.jpg', 1),
    (5, 'jeans-front.jpg', 1);

INSERT INTO
    addresses (
        user_id,
        full_name,
        phone,
        address_line,
        city,
        postal_code,
        country,
        is_default
    )
VALUES (
        1,
        'Rahim Ahmed',
        '01711111111',
        '12 Main Road',
        'Dhaka',
        '1207',
        'Bangladesh',
        1
    ),
    (
        2,
        'Karim Hasan',
        '01822222222',
        '25 College Road',
        'Khulna',
        '9100',
        'Bangladesh',
        1
    ),
    (
        3,
        'Nusrat Jahan',
        '01933333333',
        'Barishal Road',
        'Barishal',
        '9100',
        'Bangladesh',
        1
    ),
    (
        4,
        'Sakib Khan',
        '01644444444',
        '10 University Road',
        'Chittagong',
        '4000',
        'Bangladesh',
        1
    );

INSERT INTO carts (user_id) VALUES (1), (2), (3), (4);

INSERT INTO
    cart_items (cart_id, product_id, quantity)
VALUES (1, 1, 1),
    (1, 3, 2),
    (2, 4, 3),
    (2, 6, 1),
    (3, 2, 1),
    (4, 9, 2);

INSERT INTO
    orders (
        user_id,
        address_id,
        order_number,
        subtotal,
        discount,
        shipping_cost,
        total_amount,
        status
    )
VALUES (
        1,
        1,
        'ORD-1001',
        4700,
        200,
        100,
        4600,
        'delivered'
    ),
    (
        2,
        2,
        'ORD-1002',
        3100,
        100,
        80,
        3080,
        'shipped'
    ),
    (
        3,
        3,
        'ORD-1003',
        3999,
        0,
        100,
        4099,
        'processing'
    ),
    (
        4,
        4,
        'ORD-1004',
        1998,
        200,
        80,
        1878,
        'pending'
    );

INSERT INTO
    order_items (
        order_id,
        product_id,
        product_name,
        quantity,
        unit_price,
        total_price
    )
VALUES (
        1,
        1,
        'Wireless Headphones',
        1,
        2999,
        2999
    ),
    (
        1,
        3,
        'Wireless Mouse',
        1,
        1200,
        1200
    ),
    (
        2,
        4,
        'Cotton T-Shirt',
        2,
        699,
        1398
    ),
    (
        2,
        6,
        'Python Programming Book',
        1,
        999,
        999
    ),
    (
        3,
        2,
        'Mechanical Keyboard',
        1,
        3999,
        3999
    ),
    (
        4,
        9,
        'Yoga Mat',
        2,
        999,
        1998
    );

INSERT INTO
    payments (
        order_id,
        payment_method,
        transaction_id,
        amount,
        status,
        paid_at
    )
VALUES (
        1,
        'bkash',
        'TXN001',
        4600,
        'paid',
        '2026-09-10 10:30:00'
    ),
    (
        2,
        'card',
        'TXN002',
        3080,
        'paid',
        '2026-09-11 14:20:00'
    ),
    (
        3,
        'cash_on_delivery',
        NULL,
        4099,
        'pending',
        NULL
    ),
    (
        4,
        'bkash',
        'TXN004',
        1878,
        'paid',
        '2026-09-13 16:00:00'
    );

INSERT INTO
    reviews (
        user_id,
        product_id,
        rating,
        comment
    )
VALUES (
        1,
        1,
        5,
        'Excellent headphones. Very good sound.'
    ),
    (
        2,
        4,
        4,
        'Good quality t-shirt.'
    ),
    (
        3,
        2,
        5,
        'The keyboard feels great.'
    ),
    (
        4,
        9,
        4,
        'Good yoga mat for the price.'
    );

INSERT INTO
    wishlists (user_id, product_id)
VALUES (1, 2),
    (1, 8),
    (2, 1),
    (3, 5),
    (4, 6);

INSERT INTO
    coupons (
        code,
        discount_type,
        discount_value,
        minimum_order_amount,
        max_uses,
        expires_at
    )
VALUES (
        'WELCOME10',
        'percentage',
        10,
        1000,
        1000,
        '2026-12-31'
    ),
    (
        'SAVE200',
        'fixed',
        200,
        2000,
        500,
        '2026-12-31'
    ),
    (
        'NEWUSER',
        'percentage',
        15,
        1500,
        200,
        '2026-11-30'
    );

INSERT INTO
    order_coupons (
        order_id,
        coupon_id,
        discount_amount
    )
VALUES (1, 2, 200),
    (4, 2, 200);

INSERT INTO
    inventory_logs (
        product_id,
        quantity_change,
        reason
    )
VALUES (1, -1, 'Order ORD-1001'),
    (3, -1, 'Order ORD-1001'),
    (4, -2, 'Order ORD-1002'),
    (6, -1, 'Order ORD-1002'),
    (2, -1, 'Order ORD-1003'),
    (9, -2, 'Order ORD-1004'),
    (1, 20, 'New stock received'),
    (4, 50, 'New stock received');

COMMIT;