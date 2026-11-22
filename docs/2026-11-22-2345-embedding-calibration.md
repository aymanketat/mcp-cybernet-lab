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

## Amendment of 22 November, made before any score was computed

Made after Ayman Ketat's check of the protocol as pushed (`311a9ad`; `ef5fb67` only corrected the
date) and requirements revision 3.3. No score had been computed and no result was committed. The
sections above are left as first written; where they differ from this section, this section holds.

- **The bound counts distinct decoy texts, not comparisons.** Comparisons that reuse the same texts
  are not independent trials. The 40,000 easy comparisons of fact records reuse 200 ordinary
  lines: none of them matching bounds the rate for new ordinary text at about 3/200 = 1.5 %
  (95 %), not 0.0075 %. The per-comparison figure describes these 200 lines only. The same holds
  for hard decoys, which are filled from 50 records of each kind: about 3/50 = 6 %. The sentence
  under "The measures" that gives 0.1 % for 3,000 comparisons and 0.0075 % for 40,000 is withdrawn.
  A claim at 0.1 % would need about 3,000 distinct decoy texts, which this pair set does not have.
- **Decoys are also reported per unit.** A unit is an ordinary line (easy) or a decoy record
  (hard). A unit matches if it exceeds the threshold against any source. The tables give the
  number of distinct units, the bound that follows, and how many units match at the threshold
  that 0.1 % of the comparisons exceed. (Above the highest decoy score no unit matches, by definition.)
- **"Recall at 0.1 %"** stays as defined, a threshold on comparisons. It is a description of this
  pair set, not a bound on the false-match rate of new text.
- **The held-out row** is reported at the easy-decoy thresholds of person records as well as of
  fact records: the translations of 18 November are of an employee's name and national ID.

## What is expected

- Hard decoys defeat every model: a sink template filled with another record differs from the
  positive only in the values, and a sentence embedding weighs wording more than values. Recall
  at zero hard-decoy matches is expected to be low for all four (below one half). This is a finding
  for the design of a semantic rule, not a criterion for a model.
- Against easy decoys every model is expected to separate most positives, with the three-fact and
  in-a-post sinks and the cross-language directions lowest.
- `minilm-paraphrase` refuses the longest texts (its limit is 128 tokens).

## Results

Added on 23 November 2026. The protocol was pushed at `311a9ad` (22 November, 23:44) and amended
at `bbc512d` (23:54), both before any score; the run was made on `c906d84`, which adds only a
download that resumes. Pair set `2f00861e5ad133c0` (the hash the run printed): 1,900 texts, 60,400
comparisons. Numbers only: no text of a pair or of a run is in the result files.

### The pipeline check

Each model against its reference implementation (sentence-transformers 6.3.0, torch 2.15.0 on the
CPU), twenty texts: the lowest cosine is 1.000000 for all four, the token counts are the same for
every text, and the reference's token limit is the limit used here (512, 256, 8192, 128). The
lab's module ran with onnxruntime 1.32.0, tokenizers 0.23.3 and numpy 2.5.5.

### The table

Machine: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz. One thread, one text per inference.

### Fact records against easy decoys (3400 positives, 40000 decoy comparisons, before texts refused as too long)

Distinct decoy lines: 200; none matching bounds the rate for new ones at about 1.5 % (95 %).

| Model | Recall at zero decoy matches | Recall at 0.1 % of comparisons | Decoy lines matching at that 0.1 % threshold | Median positive | Median decoy | Highest decoy | Lowest positive minus highest decoy | Decoy comparisons |
|---|---|---|---|---|---|---|---|---|
| e5-small | 88.0 % | 95.5 % | 8 of 200 | 0.910 | 0.787 | 0.870 | -0.043 | 40000 |
| labse | 100.0 % | 100.0 % | 10 of 200 | 0.724 | 0.075 | 0.316 | +0.027 | 40000 |
| bge-m3 | 99.9 % | 100.0 % | 9 of 200 | 0.757 | 0.358 | 0.545 | -0.019 | 40000 |
| minilm-paraphrase | 97.9 % | 99.6 % | 4 of 200 | 0.661 | 0.080 | 0.430 | -0.125 | 40000 |

### Fact records against hard decoys (3400 positives, 3400 decoy comparisons, before texts refused as too long)

Distinct decoy records: 50; none matching bounds the rate for new ones at about 6.0 % (95 %).

| Model | Recall at zero decoy matches | Recall at 0.1 % of comparisons | Decoy records matching at that 0.1 % threshold | Median positive | Median decoy | Highest decoy | Lowest positive minus highest decoy | Decoy comparisons |
|---|---|---|---|---|---|---|---|---|
| e5-small | 31.3 % | 34.0 % | 3 of 50 | 0.910 | 0.858 | 0.942 | -0.115 | 3400 |
| labse | 36.8 % | 37.6 % | 3 of 50 | 0.724 | 0.515 | 0.775 | -0.432 | 3400 |
| bge-m3 | 27.4 % | 34.4 % | 1 of 50 | 0.757 | 0.611 | 0.825 | -0.298 | 3400 |
| minilm-paraphrase | 5.8 % | 5.9 % | 2 of 50 | 0.661 | 0.522 | 0.878 | -0.573 | 3400 |

### Person records against easy decoys (600 positives, 10000 decoy comparisons, before texts refused as too long)

Distinct decoy lines: 200; none matching bounds the rate for new ones at about 1.5 % (95 %).

| Model | Recall at zero decoy matches | Recall at 0.1 % of comparisons | Decoy lines matching at that 0.1 % threshold | Median positive | Median decoy | Highest decoy | Lowest positive minus highest decoy | Decoy comparisons |
|---|---|---|---|---|---|---|---|---|
| e5-small | 81.5 % | 85.3 % | 2 of 200 | 0.908 | 0.793 | 0.860 | -0.045 | 10000 |
| labse | 100.0 % | 100.0 % | 2 of 200 | 0.696 | 0.042 | 0.307 | +0.072 | 10000 |
| bge-m3 | 100.0 % | 100.0 % | 3 of 200 | 0.771 | 0.374 | 0.513 | +0.035 | 10000 |
| minilm-paraphrase | 92.0 % | 98.8 % | 2 of 200 | 0.747 | 0.046 | 0.440 | -0.107 | 10000 |

### Person records against hard decoys (600 positives, 3000 decoy comparisons, before texts refused as too long)

Distinct decoy records: 50; none matching bounds the rate for new ones at about 6.0 % (95 %).

| Model | Recall at zero decoy matches | Recall at 0.1 % of comparisons | Decoy records matching at that 0.1 % threshold | Median positive | Median decoy | Highest decoy | Lowest positive minus highest decoy | Decoy comparisons |
|---|---|---|---|---|---|---|---|---|
| e5-small | 28.2 % | 29.8 % | 3 of 50 | 0.908 | 0.869 | 0.945 | -0.130 | 3000 |
| labse | 21.0 % | 25.7 % | 2 of 50 | 0.696 | 0.531 | 0.769 | -0.390 | 3000 |
| bge-m3 | 40.0 % | 44.0 % | 3 of 50 | 0.771 | 0.610 | 0.842 | -0.293 | 3000 |
| minilm-paraphrase | 7.2 % | 9.2 % | 3 of 50 | 0.747 | 0.611 | 0.877 | -0.544 | 3000 |

### Fact records: recall at zero easy-decoy matches, by direction, by what the sink did, by digits

| Positives of | n | e5-small | labse | bge-m3 | minilm-paraphrase |
|---|---|---|---|---|---|
| digits: arabic-indic | 1000 | 81.7 % | 100.0 % | 100.0 % | 98.9 % |
| digits: ascii | 2400 | 90.6 % | 100.0 % | 99.9 % | 97.5 % |
| direction: ar to ar | 1000 | 100.0 % | 100.0 % | 100.0 % | 100.0 % |
| direction: ar to en | 700 | 78.6 % | 100.0 % | 99.7 % | 93.0 % |
| direction: en to ar | 1000 | 74.1 % | 100.0 % | 100.0 % | 97.8 % |
| direction: en to en | 700 | 100.0 % | 100.0 % | 100.0 % | 100.0 % |
| family: close | 600 | 95.8 % | 100.0 % | 100.0 % | 100.0 % |
| family: close-translit | 200 | 95.0 % | 100.0 % | 100.0 % | 100.0 % |
| family: in-a-post | 600 | 85.8 % | 100.0 % | 100.0 % | 100.0 % |
| family: note | 600 | 94.2 % | 100.0 % | 100.0 % | 100.0 % |
| family: note-translit | 200 | 100.0 % | 100.0 % | 100.0 % | 100.0 % |
| family: reordered | 600 | 92.8 % | 100.0 % | 100.0 % | 100.0 % |
| family: three-facts | 600 | 64.8 % | 100.0 % | 99.7 % | 88.2 % |

### Person records: recall at zero easy-decoy matches, by direction, by what the sink did, by digits

| Positives of | n | e5-small | labse | bge-m3 | minilm-paraphrase |
|---|---|---|---|---|---|
| digits: arabic-indic | 200 | 100.0 % | 100.0 % | 100.0 % | 91.5 % |
| digits: ascii | 400 | 72.2 % | 100.0 % | 100.0 % | 92.2 % |
| direction: ar to ar | 400 | 100.0 % | 100.0 % | 100.0 % | 90.2 % |
| direction: ar to en | 200 | 44.5 % | 100.0 % | 100.0 % | 95.5 % |
| family: fields | 150 | 90.7 % | 100.0 % | 100.0 % | 100.0 % |
| family: in-a-post | 150 | 66.7 % | 100.0 % | 100.0 % | 68.7 % |
| family: sentence | 150 | 75.3 % | 100.0 % | 100.0 % | 99.3 % |
| family: short | 150 | 93.3 % | 100.0 % | 100.0 % | 100.0 % |

### Cost, limits and the held-out translations

| Model | Dimensions | Token limit | Texts refused as too long | ms per text, median (95th percentile) | Pinned files | Same bytes in two processes |
|---|---|---|---|---|---|---|
| e5-small | 384 | 512 | 0 of 1900 | 24 (48) | 487 MB | yes |
| labse | 768 | 256 | 0 of 1900 | 117 (246) | 1894 MB | yes |
| bge-m3 | 1024 | 8192 | 0 of 1900 | 298 (612) | 2285 MB | yes |
| minilm-paraphrase | 384 | 128 | 0 of 1900 | 23 (47) | 479 MB | yes |

### Held out: the real translations of stored runs (a text against its translation)

Share scoring above the easy-decoy thresholds of each kind of record. Not used to set anything.

| Model | Translations scored (refused as too long) | Above the fact threshold: zero matches | fact: 0.1 % | Above the person threshold: zero matches | person: 0.1 % | Lowest, median score |
|---|---|---|---|---|---|---|
| e5-small | 34 (0) | 97.1 % | 97.1 % | 97.1 % | 100.0 % | 0.853, 0.896 |
| labse | 34 (0) | 100.0 % | 100.0 % | 100.0 % | 100.0 % | 0.819, 0.884 |
| bge-m3 | 34 (0) | 100.0 % | 100.0 % | 100.0 % | 100.0 % | 0.590, 0.756 |
| minilm-paraphrase | 34 (0) | 100.0 % | 100.0 % | 100.0 % | 100.0 % | 0.775, 0.857 |

### How to read it

- **Thresholds come from the same decoys they are tested on.** "Recall at zero decoy matches" is
  the share of positives above the highest decoy score of this pair set. It says how well a model
  separates these texts; the rate for new text is bounded only as each table's first line says
  (about 1.5 % for ordinary lines, about 6 % for other records).
- **The 0.1 % threshold is not a 0.1 % rate.** At the threshold that 0.1 % of the comparisons
  exceed, between 2 and 10 of the 200 ordinary lines match against at least one source, that is 1 %
  to 5 % of the lines. This is the difference between counting comparisons and counting texts.
- **A model's scores are on its own scale.** e5-small gives ordinary lines a median of about 0.79
  against a fact record and positives 0.91; LaBSE gives 0.08 and 0.72. A threshold belongs to one model.

### Against what was expected

| Expected (written before any score) | Found |
|---|---|
| Hard decoys defeat every model: recall at zero hard-decoy matches below one half | Held: 5.8 % to 36.8 % for fact records, 7.2 % to 40.0 % for person records. The highest hard decoy scores above the median positive in every model. |
| Against easy decoys every model separates most positives; three-fact and in-a-post sinks and the cross-language directions are lowest | Held, unevenly. LaBSE and bge-m3 are at or within 0.1 point of 100 % in every row. e5-small and minilm-paraphrase lose where expected: e5-small 64.8 % on three facts, 74.1 % and 78.6 % across languages for fact records, 44.5 % from the Arabic person record to English; minilm-paraphrase 88.2 % on three facts and 68.7 % on a person inside a post. Within one language all four reach 100 % on fact records. |
| minilm-paraphrase refuses the longest texts | Not so: no text of the pair set exceeds 128 tokens, so no model refused any text. The limit was not tested by this pair set. |

### What the table does not show

- **No model is chosen here**, and none is in any testbed. The decision is Prof. Koubaa's.
- **Hard decoys are the finding for a semantic rule:** another company or another person in the
  same sentence scores as high as a paraphrase of the right one. Similarity of the whole text
  cannot tell which record a payload states; a rule that blocks on it alone would block a post
  about any other record of the same shape.
- **The pair set is built from templates.** Twelve fact sinks and eight person sinks, however many
  records fill them, are few wordings; a model's translation of a record may be worded otherwise.
  The held-out row is the only real wording here: 34 translations of a name and a national ID from
  the four runs of 18 November, short texts, all but one above every threshold in every model.
- **Cost** is per text on one thread of the machine named above; a guard embeds several windows
  per call. The image size is not measured: there is no embedder image yet.
- **Not built yet:** the embedder sidecar, the `embed` function in a guard's context,
  `embeddings.jsonl` with replay from it, and the embedder in `mcl check-isolation`.
