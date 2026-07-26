# Background music — licensing

`licenses.json` in this directory is an **allowlist**. A track is eligible for
random BGM selection only if its entry carries a non-empty `license` field.
`app/services/video.py::get_bgm_file` enforces this.

The manifest is opt-in: if `licenses.json` is absent, BGM selection behaves as
it did before and every `.mp3` here is fair game. Its presence turns
enforcement on.

## Why every shipped track is currently disabled

The 29 `outputNNN.mp3` files came from upstream MoneyPrinterTurbo, whose README
states:

> The current project includes some default music from YouTube videos. If there
> are copyright issues, please delete.

That is not a license. For a channel pursuing monetization the risk is
asymmetric: a single claimed track can demonetize a video retroactively, and an
automated pipeline has no way to notice it happened. So they are all listed
with `"license": ""` and will not be used — renders proceed with no background
music until cleared tracks are added.

## Adding a track you can actually use

1. Drop the `.mp3` in this directory.
2. Add an entry to `licenses.json`:

```json
"my-track.mp3": {
  "source": "Artist — Track Title",
  "license": "CC0-1.0",
  "url": "https://example.com/where-you-got-it"
}
```

Any non-empty `license` makes it eligible. Keep `url` pointing at something
that would actually substantiate the claim if challenged.

Reliable sources: YouTube Audio Library (check the per-track attribution
requirement), Free Music Archive CC0/CC-BY, Pixabay Music, Incompetech
(CC-BY, attribution required in the video description).
