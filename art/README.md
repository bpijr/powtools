# Local game assets

Place files from your own game dump here when running the tools locally. Extracted archives and the
hash registry are ignored by Git and should not be assumed to be part of a public source release.

Expected character layout:

```text
art/
  hashid.bin
  characters/
    fighter.dict
    fighter.data
    fighter.debug
```

Some tests require named local fixtures such as `bearhugger`, `glassjoe`, `littlemac`, `donkeykong`
or `referee`. See the test's command-line arguments before running it.
