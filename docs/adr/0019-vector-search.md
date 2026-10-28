# ADR-0019: Vector search in Neo4j, hybrid Ask, local embeddings

**Status:** Accepted (2026-10-22)

## Context
Neo4j (ADR-0017) holds graqle's vector index `cogni_chunk_embedding_index` (1024 dims, sized for Amazon Titan)
over 15,532 chunks, none embedded: graqle reported it `usable: false`, and graq's question embedder (all-MiniLM,
384 dims) would not match it anyway. Our own `embedding` role (Ollama nomic-embed-text) was used by nothing.
Ask retrieved evidence with keyword search only (SQLite FTS5, BM25). In the 2026-10-05 model evaluation the
question "What is the EU doing on AI regulation?" got 12 items and none about the EU: terms under 3 characters
("EU") are dropped, so the search ran on "regulation" alone, although the corpus holds the EU AI Act Newsletter and
Commission items. Ollama is heavy on the Mac only because of the 5 GB extraction model; embeddings need far less.

## Decision
1. **Embeddings in process.** The `embedding` role is `fastembed` / `BAAI/bge-small-en-v1.5`: ONNX Runtime, no
   server, no PyTorch, 67 MB, MIT, 384 dims, model files in `data/models/`. A new router provider kind
   (`fastembed`) keeps the rule that every model call goes through `llm.py`. BGE is asymmetric: questions get the
   model's query instruction (`params.query_prefix` in models.yaml), stored passages do not.
2. **What is embedded.** One chunk per news item (the DOCUMENT node with the item id) and one per entity:
   6,565 texts. Section and document-path chunks repeat the item text and get no vector, so one story is never
   returned three times (a vector index only holds nodes that have the indexed property).
3. **Cache, then project.** Vectors are cached in `data/vectors.sqlite` by hash(model + text); Neo4j stays a
   rebuildable copy (ADR-0017). `signal-radar graph embed` embeds only missing texts, within `--max-chunks` /
   `--max-minutes`, keeps finished batches on Ctrl+C, then writes vectors with `db.create.setNodeVectorProperty`
   and a `vec_key` per chunk so stale vectors are rewritten. It is never run from a git hook. The daily job runs it
   after the sync (`config/graph.yaml` `embed:`, 10-minute cap); a failure is a warning, not a failed run.
4. **Our own index.** `signalradar_chunk_vec` on `(:Chunk).vec`, cosine, dimension from the model (recreated if the
   model's dimension changes). graqle's 1024-dim index is left untouched. The restricted user has the `index`
   privilege on `signalradar` only.
5. **Search = vectors + one graph hop (GraphRAG).** `signal-radar search` and the MCP tool `semantic_search` embed
   the question, query the index (approximate k-NN), and add context from the graph: the entities an item mentions,
   or the recent items that mention an entity. Without Neo4j the same search runs exactly over the cache (numpy).
6. **Hybrid Ask.** Keyword and vector search each rank items; reciprocal rank fusion (1 / (60 + rank)) merges the
   rankings, because BM25 and cosine scores are on different scales. Relevance and source coverage only break ties.

## Evidence (2026-10-22)
- First run: 6,565 texts in 191 s on CPU (about 34 per second); index ONLINE at 100%. A re-run embeds and writes
  nothing. A full `graph sync` keeps all vectors (graqle's chunk upsert sets text/type/index only).
- Index quality: Neo4j's index (HNSW with binary quantization and rescoring, the 2026 default) returned 19 to 20
  of the exact top 20 on three test questions.
- Retrieval quality: the 8 Ask questions of the model evaluation, top 12 items per method, judged blind in-session
  (139 items, methods hidden): **keyword only 54/96 = 56% relevant, hybrid 69/96 = 72%**, hybrid never worse on any
  question. EU regulation 0/12 -> 6/12; OpenAI safety incidents 8/12 -> 12/12; model prices 4/12 -> 6/12.
- Vectors alone are not enough: on "AI in banking or insurance" keyword search found the insurance startups and
  vector search drifted to general AI essays. That is why Ask fuses both.

## Consequences
- $0 to run; the daily embed handles about 100 new texts in seconds. One more dependency set (`.[vectors]`).
- graq's own reasoning still cannot use vectors (its embedder and index differ). Optional later: install
  sentence-transformers and point the generated graqle.yaml at bge-small-en-v1.5 with a 384-dim index.
- The same vectors can merge one story told by several sources (TODO T26) and score items against the lens.
- Weak questions that remain (Meta enterprise moves 5/12, banking 6/12) lack relevant items in the corpus: a source
  gap (T24), not a search gap.
- The relevance judgements were made by one grader (Claude, blind to method); the sheet and key are in
  `data/model_eval/retrieval_*`.
