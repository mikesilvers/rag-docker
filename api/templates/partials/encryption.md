## Encryption

A package is encrypted only when it was exported with a passphrase. Its name
then ends in `.tar.gz.age`: it is an [age](https://age-encryption.org) file that
only that passphrase opens, whether through import here or with `age -d`. The
passphrase can't be recovered, and without it the package can't be opened. Send
it by a different channel than the file.

A package whose name ends in `.tar.gz` is not encrypted. Nothing in it is
protected: `sources/` holds the original documents byte for byte, and
`chunks.jsonl` holds their text. If the corpus contains confidential or personal
material, the package does too. Move and store it accordingly.

Encryption protects the file while it is moved and stored. Once it is decrypted
or imported, its contents are plaintext again, on the machine that opened it.
