"""All LLM prompt templates for the agent."""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are AutoText2SQL — an intelligent database metadata search assistant.
Your role is to help users find relevant tables, columns, and relationships \
in a set of relational databases by answering questions in natural language.

Rules:
- Always respond based on the retrieved database objects provided to you.
- When generating SQL, produce only read-only SELECT statements.
- Never generate DML, DDL, or any statement that could modify data.
- Format your response clearly with: explanation, relevant tables, suggested SQL.
- Be concise and factual. Do not invent table or column names.
"""

QUERY_ANALYZER_PROMPT = """\
Analyze the following user query about a database.
Extract:
1. intent: what the user wants to find (one sentence)
2. entities: list of business terms, table/column name hints mentioned

Respond in JSON:
{{"intent": "<string>", "entities": ["<term1>", "<term2>"]}}

User query: {query}
"""

RERANK_PROMPT = """\
You are a database metadata expert. Given a user query and a list of database objects, \
score each object's relevance to the query on a scale from 0 to 10.

User query: {query}

Objects:
{objects_json}

Return ONLY a JSON array: [{{"index": 0, "score": 8.5}}, ...]
"""

RESPONSE_GENERATOR_PROMPT = """\
Based on the retrieved database objects below, answer the user's question.

User question: {query}

Retrieved database objects:
{schema_context}

Provide:
1. A clear explanation of where the relevant data lives (DB → schema → table → columns).
2. How the tables relate to each other (via foreign keys if applicable).

Be concise and helpful.
"""

SQL_GENERATOR_PROMPT = """\
Generate a read-only SQL SELECT query that answers the following user question.

User question: {query}

Relevant database objects:
{schema_context}

Rules:
- Return only a valid SQL SELECT statement, no explanation.
- Use only the tables and columns listed above.
- PostgreSQL table references must use schema.table, not db.schema.table.
- If joining tables, use the foreign key relationships provided.
- Do not use subqueries unless necessary.
- If the user asks for an aggregate over the latest/top-N rows, first select those rows in a subquery,
  then aggregate in the outer query. Example pattern:
  SELECT SUM(x) FROM (SELECT x FROM schema.table ORDER BY created_at DESC LIMIT 10) AS t
- Always include a LIMIT clause (max 100 rows).

SQL:
"""

SQL_REWRITE_PROMPT = """\
The following SQL query needs to be rewritten to comply with read-only policy.
Only SELECT and WITH (CTE) statements are allowed.

Original SQL:
{sql}

Rewrite as a valid read-only SELECT. Return only the SQL, no explanation.
"""
