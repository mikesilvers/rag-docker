## Encryption

A package whose name ends in `.tar.gz.age` is encrypted. It is an
[age](https://age-encryption.org) file, opened in one of two ways:

- **With its own key** (the default since 1.3.1). Each export is encrypted to a
  new key made for that package alone, and the key is written to a separate key
  file, `<package>.key`. The key file opens the package, through import or with
  `age -d -i <package>.key <package>`, and nothing else. It can be downloaded
  from the server once, and it can't be recovered. Store it apart from the
  package: anyone who has both can read it.
- **With a passphrase**, chosen when it was exported. Import it with that
  passphrase, or open it with `age -d`. The passphrase can't be recovered either.
  Send it by a different channel than the file.

A package whose name ends in `.tar.gz` is not encrypted. It was exported with
encryption turned off on purpose (LAN mode doesn't allow that). Nothing in it is
protected: `sources/` holds the original documents byte for byte, and
`chunks.jsonl` holds their text. If the corpus contains confidential or personal
material, the package does too. Move and store it accordingly.

Encryption protects the file while it is moved and stored. Once it is decrypted
or imported, its contents are plaintext again, on the machine that opened it.
