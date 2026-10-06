# The eyes' ship reference

`core/eyes_reference.py` lets the Suit read the public ship-fingerprint site
(https://suitmk2-eyes.pages.dev): which ships it covers, which tables exist, and the tables themselves when
something asks for one. `tests/test_eyes_reference.py` holds it to its rules.

**It does not recognise ships.** Read the next two sections before building on it.

## What the eyes are today (core/eyes.py)

Every 2, 3 or 10 seconds (the "presence" setting), and only while Star Citizen is the window in front:

1. One frame of the primary screen is grabbed and shrunk, in memory.
2. A 64-bit difference hash says whether the picture changed. If not, nothing more happens.
3. A nearest-neighbour over 32x18 thumbnails picks one of twelve scenes (menu, hangar, on_foot, cockpit,
   quantum, combat, landing, mining, trading, map, dead, other), or says it cannot tell.
4. If it cannot tell, and the pilot switched the glance on, and the game is leaving room, a local vision
   model (gemma3:4b through Ollama on this PC) answers with a scene and up to twelve words of what a
   companion would notice. Each answer also teaches step 3.
5. On request from the combat watch, a short burst of frames is checked for muzzle flashes.

What comes out is `eyes.state()`: the scene, how long, the "notable" words, how old they are. No ship name.
The ship the pilot is in comes from the game log (`core/event_parser.py`, the channel change on boarding).

There is no image-embedding model anywhere in the Suit. `models/` holds one model, YAMNet, for sound.
`requirements.txt` has no torch, no transformers and no onnxruntime; numpy and onnxruntime arrive only as
dependencies of the speech packages.

## Why recognition is not connected

A fingerprint table can only be compared with a fingerprint made by the same model and the same
preprocessing. The published tables were made with CLIP ViT-H/14 (laion2B), DINOv2-base and DINOv2-small:
the whole frame squashed to 224x224 bicubic, the model's own mean and deviation, one L2-normalised vector.
The Suit cannot make any of those vectors, so there is nothing to look up. A thumbnail, a difference hash or
a sentence from the glance model is not a fingerprint and cannot be matched against these tables.

Separately, the site's own measurement (report.md there) says the method does not work well enough:

- run as planned it gave no answer on 97.1% of exterior and 98.2% of interior test frames;
- forced to answer, it gets the ship family more often than the exact ship, interiors more often than
  exteriors, and many frames together more often than one;
- forced, it names one of the ten ships for every frame of a ship it has never seen;
- nothing was tested on a real game screen; all footage is review video at 480p or lower.

So `RECOGNITION_CONNECTED` is `False`, no function in the module names a ship, and a test fails if either
changes without this section being rewritten.

### What it would take

1. A fingerprint model the Suit can run. DINOv2-small is the only realistic one (the report measured 44 to
   70 ms per frame on CPU; CLIP ViT-H/14 took 1.5 to 2 s). It would have to ship or be downloaded as an ONNX
   file of exactly the published revision (about 22 million weights, roughly 90 MB), and run through
   onnxruntime on the CPU as the sound classifier does. onnxruntime would have to become a stated
   requirement.
2. Proof that the Suit's copy makes the same vectors: fingerprint frames that are already in a published
   embedding file and compare. Without that proof a table lookup is noise that looks like an answer. The
   frames are not published, so this can only be done where the frames are.
3. Evidence on real game screens. There is none. The pilot's own captures (`core/training_shots.py`, opt-in)
   are the footage that would count.
4. An answer for "none of these". The tables have none, and thresholds did not separate unknown ships.
5. Then, at most: ship family, or cockpit or not, over many frames, with "cannot tell" as the usual answer,
   only agreeing with or adding to what the game log says, off by default.

## What the module does

| Thing | Where |
|---|---|
| The address | `BASE_URL` in the module. The settings key `eyes_reference_url` replaces it; `""` means offline. |
| The index the toolbox ships with | `data/eyes_reference_index.json` (version 1, ten ships, 22 kB) |
| The pilot's copy | `~/.sctoolbox/suitmk2/eyes_reference/`: `index.json`, `have.json`, `files/<site path>` |

- **At start-up** (`start_in_background`, called by `core/companion_service.py` when the eyes are on): one
  GET for `index.json`, about 22 kB, on its own thread, at most once a day. Nothing else. With
  `"eyes_reference_url": ""` no request is ever made. If the companions are disabled the service does not
  start, so nothing is asked.
- **A table** is fetched the first time `table_file()`, `table_rows()` or `load_table()` asks for it:
  one request per file, written to `<name>.part`, renamed only when size and sha256 match the index. After
  that it is read from disk: no request, on this run or any later one. `plan(table)` says what a table would
  cost before fetching it. The five float16 tables are 7.7, 16.0, 6.0, 20.0 and 12.0 MB; all five with their
  row labels are 65.6 MB.
- **Any other published file**: `file(path)` fetches the batch's file list once (321 kB for batch 1),
  checks it against the sha256 the index holds, and uses the list's sha256 for the file.
- **Requests** are GETs under the one address, https only, with the agent `SC-Toolbox-SuitEyes/1`. The host
  answers 403 to Python's default agent. Nothing about the pilot, the PC or the game is sent.
- **The host answers a missing address with its front page and status 200.** A file is therefore accepted
  on its checksum and on nothing else.
- **Failure** is a `ReferenceProblem` carrying a sentence. `startup()` and `start_in_background()` never raise.
- numpy is needed only by `load_table()`. Everything else is standard library.

Try it without the game: `python core/eyes_reference.py --status`, `--refresh`, `--pull dinov2_s_all`
(add `--home <folder>` to use a folder other than the pilot's).

## The index, and how it grows

The site's `index.json` (format 1) is what the module reads. In short:

- `version` goes up by one for each batch of ships added.
- `ships`: key, name, family, `since` (the version it arrived in), frames per view, rows per table, videos.
- `tables`: id, `since`, model, `space`, rows, width, rows per ship, and each file with path, bytes, sha256.
  `space` is the exact recipe (model, output, crop, centring). Tables in one space can be stacked into a
  bigger reference; tables in different spaces must never be mixed.
- `batches`: each batch's own file list, pinned by sha256. Batch 1's list is the site's `manifest.json`.
- `embeddings`, `models`, `history`, `limits`.

Adding ships only appends: new files under new names, a file list per batch, and the next `index.json`.
A published data file is never rewritten or renamed, so a table fetched once is never fetched again, and
this version of the Suit can read a later index: it ignores keys it does not know and still finds
everything it knew. `Index.new_since(version)` and `ReferenceStore.whats_new()` say what arrived.
The full description is `index.md` on the site.

## Converting more ships

The pipeline lives outside the toolbox, in the working folder the first batch was made in (its scripts are
published under `scripts/` on the site). This is the order, and what each step does to work already done.

| Step | Script | Adds to what is there? |
|---|---|---|
| 1. List the new videos: one row each in `meta/manifest.tsv` (id, ship key, G or T, uploader) | by hand | yes |
| 2. Download | `download.py` | yes; skips videos already there |
| 3. Cut frames | `extract.py` | yes; skips videos that have an `index.json` |
| 4. Contact sheets, then time ranges in `labels/segments.txt` (and `labels/crops.tsv` for overlays) | `sheet.py`, by hand | yes |
| 5. Fingerprints | `embed.py <model>`, `embed2.py <model> full` and `cc` | yes; skips videos already embedded |
| 6. Frame labels | `build_table.py` | **rewrites** `meta/frames_table.jsonl` whole. Old rows come out the same if their inputs did not change. For the site, keep only the new videos' rows as the batch's own label file. |
| 7. Fingerprint tables | `write_table.py` | **no: it rebuilds all five tables under the same names** from every gallery row, and counts rows per ship from a fixed list of ten |
| 8. Index | `make_index.py append`, then `check` | yes; refuses anything that is not an addition |

The smallest change that makes step 7 append (not made yet, about fifteen lines in `write_table.py`):

- take a batch id and a list of video ids, and keep only the rows whose video is in the list;
- write to `fingerprint_table/<batch id>/<name>.*` instead of `fingerprint_table/<name>.*`;
- count rows per ship from the rows, not from the fixed list;
- for the centred table, load the published `v2_clip_pm_all.gallery_mean.npy` instead of recomputing the
  mean. `score2.py` computes it from every gallery row, so it would move when rows are added and the old
  table would silently stop being comparable with the new one;
- leave out the capped table (`v1_clip_h_cap200`); it only records how the first run was done.

New ship keys also have to be added at the END of `TEN` and to `FAMILY` in `score.py` before any scoring;
predictions store positions in that list.

Then, for batch `b0002`:

1. Put only the new files in a stage folder, laid out as on the site: `emb/<variant>/<video id>.npy`,
   `meta/frame_index/<video id>.json`, `fingerprint_table/b0002/...`, and under `batches/b0002/` the
   batch's frame labels, source credits and label ranges. No file or folder name may carry a maker's or the
   game's name; `make_index.py` refuses one that does.
2. Write `batch.json`: the batch id and date, the new or extended ships (key, name, family, frames,
   videos), the new tables (id, model, space, rows, width, rows per ship, files), the embedding variants.
3. `python make_index.py append --previous <index.json> --batch batch.json --stage <stage>
   --published <the published folder> --out <stage>` writes `index.json`, `index_history/v0002.json` and
   `batches/b0002/manifest.json`. `python make_index.py check --previous ... --new ...` must print
   "only appends".
4. Upload. The host publishes a whole folder at a time, so the folder that goes up is everything already
   published plus the stage folder. Uploading the stage folder alone would replace the site with it.
5. Suits in the field pick up the new index within a day and report the new ships once. Copy the new
   `index.json` over `data/eyes_reference_index.json` for the next toolbox release, so a Suit that has never
   been online knows them too.

More footage per ship is what the report says is missing most (two gallery videos per ship today), and the
pilot's own screen captures are the footage that would remove the reviewer-video problem.
