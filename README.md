# Soundboard Packs

Downloadable sound packs for the Soundboard iOS app, served by GitHub Pages at
<https://bschn100.github.io/soundboard-packs/>.

The app reads [`index.json`](index.json) to list the available packs. Each entry points to a
pack manifest at `packs/<id>/sounds/pack.json`, which lists the pack's `.m4a` files along with
their sizes and SHA-256 hashes.

## Adding a pack

1. Create a folder `packs/<pack-id>/`. The id must be lowercase letters, digits and hyphens
   (e.g. `farm-animals`), and it should never change after you publish the pack.
2. Put the source audio files in that folder. Any format `afconvert` can read works
   (`.wav`, `.aiff`, `.caf`, `.mp3`, `.m4a`, `.flac`). Each sound must be **60 seconds or shorter**.
3. Add `packs/<pack-id>/pack.yml`:

   ```yaml
   name: Farm Animals
   description: Moos, oinks and clucks.
   palette:
     name: Meadow
     colors:            # quote these: an unquoted # starts a YAML comment
       - "#6BBF59"
       - "#F2C14E"
       - "#F78154"
   sounds:              # optional
     - file: cow.wav
       name: Moo        # optional, defaults to the file name ("cow.wav" -> "Cow")
       color: 1         # optional palette index, defaults to cycling through the palette
   ```

   Sounds listed under `sounds:` come first, in the order given. Any other audio files in
   the folder follow in alphabetical order with default names and colors. Strings must fit
   on one line. The parser handles a simple subset of YAML: no flow `[...]`/`{...}` syntax
   and no `|`/`>` blocks.
4. Run the build (macOS only, no packages to install):

   ```sh
   python3 build.py
   ```

   This encodes each sound to AAC at `packs/<pack-id>/sounds/<name>.m4a`, writes
   `pack.json`, and regenerates `index.json`. If any sound is too long or a file is invalid,
   the build stops and changes no manifests.
5. Commit everything, including the generated `sounds/`, `pack.json`, `index.json` and
   `.build-cache.json`, then push. GitHub Pages publishes the result.

## Versioning

You never edit `version` by hand. Whenever a pack's `pack.json` would change (a sound is
added, removed, re-recorded or renamed, a color is reassigned, or the name, description or
palette changes), `build.py` bumps that pack's version by one. The app can compare versions
to offer updates.

`.build-cache.json` records which source file produced each `.m4a`. Unchanged sounds aren't
re-encoded, so their bytes and hashes stay the same and nobody gets a spurious update. Keep
this file committed.

## Layout

```
index.json                    generated: list of all packs
.nojekyll                     serve files as-is (no Jekyll processing)
build.py                      the build script
packs/<id>/pack.yml           you write this
packs/<id>/*.wav, ...         your source audio
packs/<id>/sounds/*.m4a       generated AAC files
packs/<id>/sounds/pack.json   generated manifest (file names are bare, e.g. "kick.m4a")
```

## Licensing

Only add sounds you have the right to redistribute, and include a `LICENSE.txt` in the
pack folder. The `arcade-starter` sounds were synthesized for this repo and are CC0.
