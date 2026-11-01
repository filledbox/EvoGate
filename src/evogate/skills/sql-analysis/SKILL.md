---
name: sql-analysis
description: Plan, execute and verify read-only SQLite analysis using the connected database.
---

# SQL analysis

## Understand the request

Identify the requested business entities, measures, filters, grouping, ordering
and output columns. Use the question and explicit business context to resolve
meaning. Consult the schema for tables, columns and join keys.

## Inspect and plan

Use `query_values` to inspect uncertain stored categories. Use `execute_sql` for
targeted checks of join cardinality, missing values and aggregation behavior.
Choose the result grain before joining tables. Check whether a join multiplies
the entities being counted or the values being summed. Select aggregation and
deduplication according to that grain and the requested measure.

## Execute and verify

Generate one read-only SQLite query. Check relevant filters, date boundaries,
NULL behavior, ties and requested ordering. Execute the query when a check is
needed, inspect errors and results, and repair using observed schema and values.
Successful execution establishes executability. Semantic correctness also
requires agreement with the question and its business definitions.

Return the final SQL through the agent's output interface. Apply database-specific
knowledge only within its documented scope.
