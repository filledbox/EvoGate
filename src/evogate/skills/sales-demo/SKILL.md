---
name: sales-analysis
description: Query the sales database for order counts and item revenue by region.
---

# Sales analysis

## Plan the query

Identify the requested measure, business entity and filters. Inspect the provided
schema before choosing join keys. Use `query_values` when a stored value is uncertain.

## Execute and check

1. For an order count, count the order entities. A join from `orders` to `items`
   can produce several rows per order. Use `COUNT(DISTINCT o.id)` for this join.
2. For item revenue, sum `i.amount` over item rows. Preserve the item grain.
3. Apply the requested region filter. Use `execute_sql` to check a proposed query
   when its join or aggregate needs verification.
4. Check the requested output shape and inspect execution errors. Correct the
   query using observed schema and values, then return the final read-only SQL.

## Scope

These steps describe the bundled sales database. Interpret new questions using
their explicit business meaning and evidence.
