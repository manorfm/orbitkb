# Sample order Kotlin fixture

A source-analysis fixture for one `POST /venues/{restaurantId}/spots/{tableId}/checks/{billId}/items` route. It models controller → command → use case → repository, catalog Feign calls and a typed Redis event. Security declares this route authenticated. The application enables Rabbit listeners, but this slice has no Rabbit publisher or consumer.

The files are intentionally a bounded excerpt of a larger service; they are parsed by OrbitKB tests and are not a deployable application.

Run `pytest -q tests/test_sample_order_kotlin.py` to inspect the current coverage.
The analyzer reaches both catalog Feign routes and recognizes the MongoDB
document and Spring security rule. The endpoint still has unresolved flow
boundaries around extension methods, domain branches and Redis publication;
the request DTO is extracted, but the inferred response type and business
description are not complete enough for zero-call docs.
