# Embedding models: pinned candidates, the pair set, the measures, and the calibration (work package 3, item 3b)

22 November 2026, branch `wp3-embedder`, on top of `wp3-extension-points`. Work package 3 of the
FlowGuard lab requirements (revision 3.2, in Ayman Ketat's thesis records), item 3b: the part that
calibrates candidate embedding models on English-Arabic pairs. It produces a table for a decision
that is not taken here. No model is chosen, and no model goes into a testbed.

This note is written in two steps. Everything down to "Results" was committed and pushed before any
score was computed: the candidates, the pair set, the measures and what is expected. The results
are added afterwards, and what was written before is not changed.

Not in this change yet: the embedder sidecar of a testbed, the `embed` function in a guard's
context, `embeddings.jsonl`, and the embedder in `mcl check-isolation`.

## The candidates

One module, `mcp_cybernet_lab/embedding.py`, will serve the sidecar, this calibration and a replay
on the host. It needs numpy, onnxruntime and tokenizers (`pip install -e .[embed]`), so it is not
one of the modules copied into the lab's standard-library images.

| Name | Repository | Revision | Files used | Pipeline | Token limit | Dimensions |
|---|---|---|---|---|---|---|
| `e5-small` | `intfloat/multilingual-e5-small` | `614241f6` | `onnx/model.onnx`, `tokenizer.json` | "query: " before every text, mean over tokens, unit length | 512 | 384 |
| `labse` | `sentence-transformers/LaBSE` | `836121a0` | `onnx/model.onnx`, `tokenizer.json`, `2_Dense/model.safetensors` | first token, the Dense layer with tanh, unit length | 256 | 768 |
| `bge-m3` | `BAAI/bge-m3` | `5617a9f6` | `onnx/model.onnx`, `onnx/model.onnx_data`, `tokenizer.json` | first token, unit length | 8192 | 1024 |
| `minilm-paraphrase` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | `e8f8c211` | `onnx/model.onnx`, `tokenizer.json` | mean over tokens, unit length | 128 | 384 |

- **Pinned** by the full revision and the SHA-256 of every file used (`embedding.MODELS`). `fetch`
  downloads at that revision and keeps nothing whose hash differs; `Embedder` verifies every hash
  again before it loads a file, and never uses the network.
- **Pipelines** are read from each repository's own configuration at the pinned revision
  (`modules.json`, `1_Pooling/config.json`, `2_Dense/config.json`, `sentence_bert_config.json`).
  The token limits are the reference's `max_seq_length`. The reference of `minilm-paraphrase` does
  not normalize; unit length is added here and changes no cosine.
- **A text over the limit is refused** (`TooLong`), not cut. The limit sets how long a window a
  guard can compare.
- **Reproducible on one machine:** one thread, one text per inference, no padding, no truncation.

## The pipeline check (before any score)

`calibrate_embedders.py check` compares, for twenty texts of the pair set in both languages, the
vector of this module with the vector of the model's reference implementation
(sentence-transformers, in its own environment, `reference_vectors.py`): the lowest cosine must be
at least 0.999, and both must count the same number of tokens. A model that fails is not
calibrated until its pipeline is right.

## The pair set

`tests/layer1/embedpairs.py`, `pair_set()`, seed 20261122 (the seed of the Layer-1 suite: these
records are planted in no testbed). 1,900 texts, 60,400 comparisons.

Every comparison is a source text against a sink payload: a span a private server returned against
what an agent sends to a public server. The whole payload is one text; cutting a payload into
windows is a guard's design and is not done here, so the "in a post" templates measure what a
longer post around the record costs.

- **Records.** 50 fact records from `seeding.fact_record` (reference, company, amount, date, city;
  50 different companies) and 50 of the lab's Arabic person records (name and national ID).
- **Sources.** A fact record as an English sentence, as English fields, and as an Arabic sentence
  (ASCII digits and Arabic-Indic digits). A person record exactly as the lab plants it.
- **Sinks.** Arabic and English: a close translation, a reordered paraphrase, a short note, three
  facts only, the record inside a longer post; in English also with the company name
  transliterated; for a person a sentence, a short form, fields, and inside a post. Arabic sinks
  come with both digit scripts. The Arabic was read and corrected by Ayman Ketat.

| Class | What the payload is | Fact records | Person records |
|---|---|---|---|
| positive | the record of the source, through a sink template | 3,400 | 600 |
| hard decoy | the same sink template filled with another record | 3,400 | 3,000 |
| easy decoy | one of 200 ordinary office lines (100 per language) that carry no record | 40,000 | 10,000 |

Each source meets 200 different ordinary lines, so the easy comparisons are not repeats of a few.
A person positive has five hard decoys (five other people), a fact positive one.

## The measures

Score: the cosine of the two vectors. For each model, for fact and person records separately, and
against easy and hard decoys separately:

- **Recall at zero decoy matches:** the share of positives that score above every decoy.
- **Recall at 0.1 %:** the share of positives above the threshold that at most 0.1 % of the decoys exceed.
- **The score gap:** median positive minus median decoy, and lowest positive minus highest decoy.
- **The bound:** with n decoy comparisons and none matching, the rate is below about 3/n (95 %):
  0.1 % for 3,000, 0.0075 % for 40,000.
- Recall against easy decoys also **by language direction** (English or Arabic source to English or
  Arabic sink), **by what the sink did**, and **by digit script**.
- **Cost and limits:** milliseconds per text (median and 95th percentile, one thread, the machine
  named), the token limit, the texts refused as too long, the size of the pinned files. The image
  size is not measured here: there is no embedder image yet.
- **The same bytes** for the same texts from two fresh processes.
- **Held out, numbers only:** the real translations stored in the runs of 18 November (the lab's
  translator calls the model): each text against its translation, as the share scoring above the
  two thresholds of fact records against easy decoys. They are not used to set anything, since
  guards will later be replayed on those runs.

## What is expected

- Hard decoys defeat every model: a sink template filled with another record differs from the
  positive only in the values, and a sentence embedding weighs wording more than values. Recall
  at zero hard-decoy matches is expected to be low for all four (below one half). This is a finding
  for the design of a semantic rule, not a criterion for a model.
- Against easy decoys every model is expected to separate most positives, with the three-fact and
  in-a-post sinks and the cross-language directions lowest.
- `minilm-paraphrase` refuses the longest texts (its limit is 128 tokens).

## Results

(Added after this note was first committed and pushed.)
