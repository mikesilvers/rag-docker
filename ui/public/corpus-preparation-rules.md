# Preparing files for rag-docker ingest

How to prepare files so rag-docker ingests them in one pass. Every limit below is the
app's own, as shipped. The upload and ZIP limits are settings an administrator can change
for an installation; the others are fixed in the app.

## 1. How the app reads an upload

- **One file is one document.** Each file is parsed, converted to text, split into chunks,
  embedded and stored. The per-file limits below apply to each file on its own, including
  each file inside a ZIP.
- **A file fails whole.** A file that breaks a limit, or can't be read, stores nothing. The
  other files in the job carry on: the job ends `partial` when some files failed, and
  `failed` when all of them did. If storing a file's chunks fails, what that attempt stored
  is removed and an earlier version under the same name stays as it was; only if that
  clean-up fails too does the job's error say accepted chunks may remain.
- **The same name replaces.** Uploading a file under a name the collection already holds
  replaces that name's earlier chunks. A file uploaded directly is named by its file name; a
  file inside a ZIP by its path inside the archive (`a/report.pdf` and `b/report.pdf` are two
  documents). Give every part of a split file its own name, for example
  `bills-part-a.jsonl.gz`, `bills-part-b.jsonl.gz`.
- **Supported types:** PDF (`.pdf`), Word (`.docx`), plain text (`.txt`), Markdown (`.md`),
  CSV (`.csv`), JSON (`.json`, and `.json.gz` inside a ZIP only), JSON Lines (`.jsonl`,
  `.jsonl.gz`) and ZIP (`.zip`). Other files inside a ZIP are skipped and listed, as are
  macOS `__MACOSX/` folders and `._*` files.

## 2. Limits

### Per upload and per ZIP

| Limit | Value | When it's exceeded |
|---|---|---|
| One upload, all its files together | 512 MB | The upload is refused before it reaches the app |
| All the ZIPs in one upload, once expanded (the output of gzipped members included) | 2 GiB | The upload is refused: `ARCHIVE_TOO_LARGE` |
| Entries in one ZIP, folders included | 5,000 entries | The upload is refused: `ARCHIVE_TOO_MANY_MEMBERS` |
| Expansion of one ZIP member of 1 MiB or more | 200 times its compressed size | The upload is refused: `ARCHIVE_RATIO_TOO_HIGH` |

A refused upload imports nothing. These four are the shipped values; an administrator can
change them.

### Per file

A file over any of these fails, and the other files in the job carry on.

| Limit | Value | Applies to |
|---|---|---|
| **Text** | **10,000,000 characters** | Every file and every chunking strategy. JSON and JSON Lines are measured after conversion to text (section 3). |
| **Chunks** | **10,000 chunks** | Every chunking strategy (section 4) |
| Records | 1,000,000 records | A JSON file's records; a JSON Lines file's lines, blank lines included |
| One `.json` file, or one `.json.gz` once decompressed | 30,000,000 bytes | JSON is read whole; anything bigger should be JSON Lines |
| One `.jsonl.gz` once decompressed | 512 MB | `.jsonl.gz` |
| Sentences | 100,000 sentences | The `semantic` strategy only |
| Text across the windows, the overlap counted each time | 10,000,000 characters | The `overlap` strategy only |

### Per line and per chunk

| Limit | Value | What happens |
|---|---|---|
| One JSON Lines line, not counting its line ending | 10,000,000 bytes | The file fails at that line |
| One chunk | 6,000 characters | A longer chunk is cut, at whitespace where possible; nothing fails |

### Chunk settings

| Setting | Allowed | Default |
|---|---|---|
| `chunk_size` (characters) | 50 to 6,000 | default 1,000 |
| `chunk_overlap` (characters) | smaller than `chunk_size` for `overlap` and `language` | default 200 |
| `min_chunk_size` (characters) | 0 to 6,000 | default 100 |

## 3. JSON and JSON Lines: the text is bigger than the file

The app doesn't embed raw JSON. It turns every value into a readable line,
`key.path: value`:

```
{"bill": {"number": "HR 1", "sponsors": ["A", "B"]}, "actions": [{"date": "2025-01-03"}]}
```

becomes

```
bill.number: HR 1
bill.sponsors: A, B
actions[0].date: 2025-01-03
```

- Keys keep the file's order. Nested keys become dotted paths; an array of plain values
  becomes one line (`sponsors: A, B`); an array of objects or arrays gets indexed paths
  (`actions[0].date`). Strings lose their quotes; numbers, `true`, `false` and `null` stay
  as written. Empty objects and arrays write nothing.
- A JSON file whose top level is an array of objects or arrays gives one record per item.
  In a JSON Lines file, each line is one record. Records are separated by a blank line, and
  the text limit counts all of it, the blank lines included.
- **Every line repeats its full key path**, so deep nesting and arrays of objects make the
  text grow (`actions[3].committee.name: ...` on every line).
- **Gzip doesn't help with the limit.** The limit is on the converted text, after
  decompression. A small `.jsonl.gz` can convert to far more than 10,000,000 characters.
- A JSON Lines file fails at the line that crosses the limit, and the error names it:
  "converts to more than 10,000,000 characters of text by line N, the per-file limit for
  every chunking strategy; split the file".

To keep the text small:

- Leave out fields nobody will search on: internal IDs, hashes, URLs, duplicate copies of
  the same text.
- Flatten deep structures, and use short, meaningful key names.
- Put the searchable prose in one field, with only the metadata a reader needs.

These fail a JSON Lines file whole, at the first bad line:

- A blank line followed by another record. Blank lines after the last record are fine.
- A line that isn't valid JSON (`NaN` and `Infinity` are not valid JSON), or text that
  isn't UTF-8.
- A line longer than 10,000,000 bytes.

A `.json` file is read whole, so one over 30,000,000 bytes fails before it is read: convert
it to JSON Lines, one record per line.

## 4. Chunk count

The 10,000-chunk limit can be reached before the text limit. How many chunks a file gives
depends on the collection's chunking strategy and its chunk settings:

- **`overlap`** (the default) **and `language`**: about one chunk for every
  `chunk_size − chunk_overlap` characters. With the defaults (1,000 − 200), a file reaches
  10,000 chunks at **about 8,000,000 characters**, below the 10,000,000-character limit.
- **`fixed` and `context_aware`**: about one chunk for every `chunk_size` characters. With
  the default `chunk_size`, the 10,000,000-character limit is reached first.
- **`semantic`**: one chunk per run of sentences on the same topic, so the count depends on
  the text; it is limited to 100,000 sentences per file as well.
- `context_aware` splits JSON and JSON Lines only between records, and joins short records
  into one chunk up to `chunk_size`; a record longer than `chunk_size` is one chunk, cut at
  6,000 characters. `language` prefers to split at the blank line between records.
- A larger `chunk_size` gives fewer chunks, at some cost to retrieval precision. A smaller
  `chunk_overlap` gives fewer chunks with `overlap` and `language`.

## 5. Safe targets and splitting

Aim below the limits, not at them:

| Per file | Target | Limit |
|---|---|---|
| Converted text | at most 7,000,000 characters (70 % of the limit) | 10,000,000 characters |
| Chunks, estimated as in section 4 | at most 8,000 chunks (80 % of the limit) | 10,000 chunks |
| Records or lines | under the limit | 1,000,000 records |

When a file is over a target, split it:

- **JSON Lines:** split at line boundaries, never inside a line, into parts that each stay
  under the targets. Every part is a valid JSON Lines file of its own.
- **A large `.json` array:** convert it to JSON Lines, one record per line, then split that.
- **Other documents:** split by section or chapter.
- Give every part its own name (section 1), and group the parts into ZIPs that stay under
  the upload and ZIP limits (section 2).

## 6. Checklist before you upload

1. Choose the collection's chunking strategy and chunk settings first: the chunk estimate
   depends on them.
2. Trim the fields you don't need (section 3).
3. Split every file over the targets (section 5).
4. Give every file a distinct name.
5. Keep each upload under 512 MB, and its ZIPs under 2 GiB once expanded and 5,000 entries
   each.
6. After the job ends, check whether it is `partial` or `failed`, read the errors, fix the
   files named and upload them again under the same names.
