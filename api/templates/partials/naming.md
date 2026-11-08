```
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>.tar.gz
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>.tar.gz.age    (exported with a passphrase)
```

The collection name in the filename is lowercased and stripped of punctuation,
so it is lossy, and two different collections can produce the same label. `<id8>`
is the first eight hex characters of the manifest's own digest.

**`manifest.json` is authoritative.** Import reads the collection name from there
and never parses the filename. Renaming a package file changes nothing about what
it contains.
