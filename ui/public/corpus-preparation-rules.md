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
| One chunk | 6,000 characters | A longer chunk is split into pieces of at most 6,000 characters, at whitespace where possible, and every piece counts toward the 10,000-chunk limit |

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
depends on the collection's chunking strategy and its chunk settings. Count them with the
rule for the strategy. Each rule counts the worst case, so most files give fewer chunks than
it says, and a file whose count stays under the target in section 5 doesn't fail on it. Below,
"text" is the file's converted text in characters (section 3), and a record is one JSON
record or JSON Lines line once converted.

- **`fixed`**: one chunk for every `chunk_size` characters. With the default `chunk_size`,
  that is 8,000 chunks at about 8,000,000 characters, so the 7,000,000-character target is
  reached first.
- **`overlap`** (the default): one chunk for every `chunk_size − chunk_overlap` characters.
  With the defaults (1,000 − 200), one chunk per 800 characters: 8,000 chunks at about
  6,400,000 characters. The chunks repeat the overlap, so this strategy has a second count,
  the text across its chunks: the text plus `chunk_overlap` for every chunk, limited to
  10,000,000 characters (section 2).
- **`language`** splits at blank lines first (two line breaks in a row), then at line breaks,
  at a full stop followed by a space, at spaces, and last between any two characters. It
  keeps every piece shorter than `chunk_size` whole, so a chunk can be only a little over half
  full, and it splits a longer piece again at the next level, which can leave a short chunk
  on each side of it. Call a paragraph (the text between blank lines), a line, a sentence
  (the text of a line between full stops followed by a space) or a word (the text of a line
  between spaces) long when it has at least `chunk_size − 3` characters. Count one chunk for
  every `(chunk_size − chunk_overlap) ÷ 2` characters, plus two for every long paragraph,
  long line, long sentence and long word, plus one. Each level counts on its own: a long
  word in a long sentence, in a long line, in a long paragraph counts eight. With the
  defaults, one chunk per 400 characters, so a file with nothing long reaches 8,000 chunks at
  about 3,200,000 characters. Whatever the structure, the count is never more than one chunk
  for every `(chunk_size − chunk_overlap) ÷ 10` characters, plus one, and you can use that
  instead of counting long pieces: with the defaults, one chunk per 80 characters, 8,000
  chunks at about 640,000 characters. For JSON and JSON Lines, whose records are separated
  by a blank line, one of these counts may be smaller, and you can use it instead:
  - if no paragraph is long: one chunk per paragraph (one per blank line, plus one), but
    never fewer than one for every `chunk_size − chunk_overlap` characters;
  - if every paragraph is shorter than `chunk_size − chunk_overlap − 3`: one chunk for every
    `chunk_size − chunk_overlap − 3 − L` characters, plus one, where `L` is the longest
    paragraph's length.
- **`context_aware`** puts JSON and JSON Lines records, and the paragraphs, titles and
  tables of other files, into chunks whole: short ones are joined up to `chunk_size`, and a
  longer one is a chunk of its own, split into pieces if it is over 6,000 characters
  (section 2). So a chunk can be only a little over half full. Count one chunk for every
  `chunk_size ÷ 2` characters, plus two for every table, plus one for every 3,000
  characters, rounded up, of each record or paragraph longer than 6,000 characters. With
  the default `chunk_size`, one chunk per 500 characters: 8,000 chunks at about 4,000,000
  characters. For JSON and JSON Lines, one of these counts may be smaller, and you can use
  it instead:
  - one chunk per record, a record longer than 6,000 characters counting as one for every
    3,000 characters, rounded up; but never fewer than one for every `chunk_size` characters;
  - if every record is shorter than `chunk_size ÷ 2`: one chunk for every `chunk_size − L`
    characters, where `L` is the longest record's length.
- **`semantic`**: one chunk per run of sentences on the same topic, so the count depends on
  the text and isn't known before the import. A chunk over 6,000 characters is then cut
  into pieces (section 2), and the pieces can outnumber its sentences. Count one chunk per
  sentence (a sentence ends at a full stop followed by a space or a line break), plus one
  for every 3,000 characters of text, plus one. The strategy is limited to 100,000
  sentences per file as well.
- The app refuses a file early when the text alone is over 10,000 chunks at one chunk for
  every `chunk_size` characters (`fixed`, `context_aware`) or `chunk_size − chunk_overlap`
  characters (`language`). That's why the record and paragraph counts above never go below
  those.
- A larger `chunk_size` gives fewer chunks, at some cost to retrieval precision. A smaller
  `chunk_overlap` gives fewer chunks with `overlap` and `language`.

## 5. Safe targets and splitting

Aim below the limits, not at them:

| Per file | Target | Limit |
|---|---|---|
| Converted text | at most 7,000,000 characters (70 % of the limit) | 10,000,000 characters |
| Chunks, counted as in section 4 | at most 8,000 chunks (80 % of the limit) | 10,000 chunks |
| `overlap` only: text across the chunks, counted as in section 4 | at most 8,000,000 characters (80 % of the limit) | 10,000,000 characters |
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
