"""Parameterized Cypher statements for the locked Week 4 Memgraph ontology.

Three non-negotiable rules enforced across every statement in this module:
1. 100% Parameterized Cypher: never interpolate strings (`f"..."` or `.format()`).
2. `MERGE`, never `CREATE`: every node and relationship write uses `MERGE` on
   its primary key followed by `ON CREATE SET` / `SET`.
3. Explicit `valid_to IS NULL` filtering on every read/traversal query.
"""

from __future__ import annotations

MERGE_ARTICLE_SKELETON_CYPHER = """
MERGE (p:Publication {slug: $pub_slug})
ON CREATE SET p.name = $pub_name,
              p.homepage = $pub_homepage
SET p.name = $pub_name,
    p.homepage = $pub_homepage
MERGE (au:Author {slug: $author_slug})
ON CREATE SET au.name = $author_name
SET au.name = $author_name
MERGE (art:Article {canonical_id: $canonical_id})
ON CREATE SET art.article_id = $article_id,
              art.url = $url,
              art.title = $title,
              art.published_at = $published_at,
              art.content_hash = $content_hash
SET art.article_id = $article_id,
    art.url = $url,
    art.title = $title,
    art.published_at = $published_at,
    art.content_hash = $content_hash
MERGE (art)-[:PUBLISHED_IN]->(p)
MERGE (art)-[:WRITTEN_BY]->(au)
RETURN art.canonical_id AS canonical_id
"""

SUPERSEDE_ARTICLE_CLAIMS_CYPHER = """
MATCH (art:Article {canonical_id: $canonical_id})-[:ASSERTS]->(c:Claim)
WHERE c.valid_to IS NULL AND NOT c.claim_id IN $active_claim_ids
SET c.valid_to = $now
RETURN count(c) AS superseded_count
"""

MERGE_CLAIMS_BATCH_CYPHER = """
MATCH (art:Article {canonical_id: $canonical_id})
UNWIND $claims AS claim
MERGE (c:Claim {claim_id: claim.claim_id})
ON CREATE SET c.valid_from = claim.valid_from
SET c.article_id = claim.article_id,
    c.text = claim.text,
    c.quote_span = claim.quote_span,
    c.claim_type = claim.claim_type,
    c.confidence = claim.confidence,
    c.valid_to = claim.valid_to
MERGE (art)-[:ASSERTS]->(c)
FOREACH (topic IN claim.topics |
    MERGE (t:Topic {slug: topic.slug})
    ON CREATE SET t.name = topic.name
    SET t.name = topic.name
    MERGE (c)-[:ABOUT]->(t)
)
RETURN count(c) AS synced_claims
"""

MERGE_SUPERSEDES_EDGES_CYPHER = """
UNWIND $supersedes_pairs AS pair
MATCH (new_c:Claim {claim_id: pair.new_claim_id})
MATCH (old_c:Claim {claim_id: pair.old_claim_id})
MERGE (new_c)-[r:SUPERSEDES]->(old_c)
ON CREATE SET r.at = pair.at
SET r.at = pair.at
RETURN count(r) AS supersedes_edges
"""

SEARCH_CLAIMS_BY_TOPICS_CYPHER = """
MATCH (t:Topic)<-[:ABOUT]-(c:Claim)<-[:ASSERTS]-(art:Article)-[:PUBLISHED_IN]->(pub:Publication)
WHERE c.valid_to IS NULL
  AND (
    t.slug IN $topic_slugs
    OR any(term IN $search_terms WHERE toLower(t.name) CONTAINS term OR t.slug CONTAINS term)
  )
OPTIONAL MATCH (art)-[:WRITTEN_BY]->(au:Author)
RETURN c.claim_id AS claim_id,
       c.article_id AS article_id,
       c.text AS claim_text,
       c.quote_span AS quote_span,
       c.claim_type AS claim_type,
       c.confidence AS confidence,
       art.title AS title,
       art.url AS url,
       art.published_at AS published_at,
       au.name AS author,
       pub.slug AS publication_slug,
       collect(DISTINCT t.slug) AS matched_topic_slugs,
       count(DISTINCT t) AS matched_topics
ORDER BY matched_topics DESC, confidence DESC, published_at DESC, claim_id ASC
LIMIT $limit
"""

GET_CLAIM_PROVENANCE_CYPHER = """
MATCH (art:Article)-[:ASSERTS]->(c:Claim {claim_id: $claim_id})
WHERE c.valid_to IS NULL OR $include_superseded = true
OPTIONAL MATCH (art)-[:PUBLISHED_IN]->(pub:Publication)
OPTIONAL MATCH (art)-[:WRITTEN_BY]->(au:Author)
OPTIONAL MATCH (c)-[:ABOUT]->(t:Topic)
OPTIONAL MATCH (c)-[:SUPERSEDES]->(old_c:Claim)
RETURN c.claim_id AS claim_id,
       c.article_id AS article_id,
       c.text AS claim_text,
       c.quote_span AS quote_span,
       c.claim_type AS claim_type,
       c.confidence AS confidence,
       c.valid_from AS valid_from,
       c.valid_to AS valid_to,
       old_c.claim_id AS supersedes_id,
       art.canonical_id AS canonical_id,
       art.title AS title,
       art.url AS url,
       art.published_at AS published_at,
       art.content_hash AS content_hash,
       au.name AS author,
       pub.slug AS publication_slug,
       pub.name AS publication_name,
       pub.homepage AS publication_homepage,
       collect(DISTINCT {slug: t.slug, name: t.name}) AS topics
"""

COUNT_ACTIVE_CLAIMS_AND_TOPICS_CYPHER = """
OPTIONAL MATCH (c:Claim)
WHERE c.valid_to IS NULL
WITH count(DISTINCT c) AS active_claims
OPTIONAL MATCH (t:Topic)<-[:ABOUT]-(ac:Claim)
WHERE ac.valid_to IS NULL
RETURN active_claims, count(DISTINCT t) AS active_topics
"""

CROSS_PUBLICATION_DISAGREEMENTS_CYPHER = """
MATCH (a1:Author)<-[:WRITTEN_BY]-(art1:Article)-[:ASSERTS]->(c1:Claim)-[:ABOUT]->(t:Topic)
MATCH (t)<-[:ABOUT]-(c2:Claim)<-[:ASSERTS]-(art2:Article)-[:WRITTEN_BY]->(a2:Author)
WHERE c1.valid_to IS NULL
  AND c2.valid_to IS NULL
  AND a1.slug <> a2.slug
RETURN a1.slug AS author_1,
       art1.canonical_id AS article_1,
       c1.claim_id AS claim_1_id,
       c1.text AS claim_1_text,
       t.slug AS shared_topic,
       c2.claim_id AS claim_2_id,
       c2.text AS claim_2_text,
       art2.canonical_id AS article_2,
       a2.slug AS author_2
LIMIT $limit
"""
