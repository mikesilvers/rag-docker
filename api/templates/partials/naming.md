```
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>[-<n>].tar.gz          (not encrypted)
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>[-<n>].tar.gz.age      (encrypted with a new key, the default, or a passphrase)
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>[-<n>].tar.gz.age.key  (the key file of a package encrypted with a key)
```

The collection name in the filename is lowercased and stripped of punctuation,
so it is lossy, and two different collections can produce the same label. `<id8>`
is the first eight hex characters of the manifest's own digest.

`-<n>` is added only when that name is already taken: two exports of the same
collection that start in the same second would otherwise get the same name, so
the later one is `-2` (then `-3`, and so on). Both packages and their key files
are kept; an export never replaces a file in `./exports`.

**`manifest.json` is authoritative.** Import reads the collection name from there
and never parses the filename. Renaming a package file changes nothing about what
it contains.
