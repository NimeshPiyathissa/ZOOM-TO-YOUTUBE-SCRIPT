# Audio and CPU: findings and options (no production change)

Nothing here is applied. The active production audio pipeline, encoder settings and quality are untouched, and nothing
reduces quality automatically. Use `scripts/measure-cpu.sh` (read-only; reads `/proc`, prints no command lines) to get numbers
from the real stream before choosing any option.

## What is known

| Item | Source | Value |
|------|--------|-------|
| Instance | memory notes, c5a.xlarge | 4 vCPU, not burstable |
| Encoder, 1080p30 `veryfast` | earlier benchmark on this stack | ~0.75 core (static slide) to ~1.0 core (high motion) |
| Chrome rendering | `browser-source.sh` passes `--disable-gpu` | software rasterisation: every browser-source frame is painted on the CPU |
| Audio chain | `stream.sh` | web/Zoom sources: `-af aresample=async=1:first_pts=0`, then AAC `-ar 48000 -ac 2`; `direct` sources skip the filter |
| PulseAudio sink | `audio-setup.sh` | null sink at 48 kHz |

## Audio resampling: measured here, inconclusive and small

`aresample=async=1` and the 44.1 kHz -> 48 kHz conversion were timed on a development laptop encoding 900 s of synthetic sine
audio to AAC (single thread, two repeats each). The machine was under memory pressure, so run-to-run noise (2-3x between identical
repeats) was larger than any difference between variants:

| Variant | Throughput (two runs) |
|---------|-----------------------|
| no filter, 44.1k -> 48k | 7x and 19x realtime |
| `aresample=async=1`, 44.1k -> 48k | 6x and 17x realtime |
| no filter, 48k source | 8x and 10x realtime |
| `aresample=async=1`, 48k source | 19x and 11x realtime |

Conclusion: audio costs on the order of 5-15% of **one** core even in the slow runs, and no variant is distinguishable from noise.
The Pulse sink is already 48 kHz, so for the live path the resampler is mostly a pass-through that exists to keep audio in step
with video (`async=1`). **There is no evidence that audio resampling is a meaningful CPU consumer, and removing `aresample`
risks audio drift/pops, so the recommendation is: leave it.** Do not tune audio for CPU.

## Where the CPU is most likely going (hypotheses to measure, not facts)

1. **x264 encode** (~0.75-1.0 core): the largest single item and the one with a quality trade-off.
2. **Chrome software rendering** of the Zoom web client / web pages (`--disable-gpu`): plausible second item, scales with the
   amount of motion in the page. Not measured separately on production.
3. **Preview / VNC / screenshots**: measured earlier as small and viewer-driven; they stop when nobody watches.
4. Zoom desktop client (when used) and Xvfb.

## Measure first (safe on production, read-only)

```
bash scripts/measure-cpu.sh 120 5      # 2 minutes, steady state; run once while a slide is shown and once during video
```

It reports ffmpeg / chrome / zoom / Xvfb / audio / x11vnc / other as a share of one core and of the whole machine. Decide only from
these numbers.

## Options, in order of how little they change (each needs your approval to try on production)

| # | Option | Expected effect | Cost / risk |
|---|--------|-----------------|-------------|
| 1 | Do nothing; keep monitoring with `measure-cpu.sh` | - | None. At ~1.0 core of 4 there is headroom. |
| 2 | Web sources only: keep Chrome's default but lower the page frame rate via Zoom's own "reduce frame rate"/gallery limits | Fewer repaints | Visible only on motion; per-meeting setting |
| 3 | `-threads` / `x264 -preset` one step slower (`faster`) *only if* CPU headroom becomes a problem | Lower bitrate at equal quality, higher CPU | Opposite of savings; listed so nobody tries `ultrafast` thinking it saves quality-neutral CPU |
| 4 | `-preset ultrafast`/`superfast` | Less CPU | Needs more bitrate for equal quality; **quality loss - never automatic** |
| 5 | Chrome GPU flags (`--use-gl=angle --use-angle=swiftshader`) | Unclear; SwiftShader is not faster than the current software path | Can break the Zoom web client rendering; test locally first |

Anything touching `stream.sh`'s encoder arguments or Chrome flags changes the live pipeline and is outside this deployment.

## Local A/B recipe (no VPS)

Record 5 minutes of a representative source with `scripts/test-recording.sh`/`record-stream.sh` style capture on a development
machine, then re-encode it with each variant while sampling `scripts/measure-cpu.sh 60 5` in a second terminal, so comparisons
use the same input. Repeat each variant 3 times and compare the medians; a single run is not evidence (see the noise above).

## Production measurements (read-only audit, 2026-11-05)

| Process | Share of one core | Note |
|---------|-------------------|------|
| ffmpeg encoder (1080p30 `veryfast`) | ~1.1-1.8 | while live; ~0.75-1.0 in the earlier synthetic benchmark |
| Chrome kiosk (browser-source) | ~0.6 | `--disable-gpu`: `VizCompositorThread` alone ~33% of a core (software compositing of every frame) |
| PSI `cpu some avg300` | 15% | some contention, no I/O pressure, no D-state processes |

The box has 4 vCPU, so the measured total (~2-2.5 cores at worst) leaves headroom, but Chrome's compositor and the encoder do
compete: when both peak, Chrome's audio thread is the most likely thing to be late (see below).

## Audio crackling and A/V sync: what the chain does

```
Chrome / Zoom  ->  PulseAudio null sink zoom_out (48 kHz, float32, 2ch; Chrome's sink-input is mono 48k, upmixed by Pulse)
               ->  ffmpeg -f pulse -use_wallclock_as_timestamps 1 -i zoom_out.monitor
               ->  -af aresample=async=1:first_pts=0  ->  AAC 48 kHz stereo
```

* Production `ffmpeg.log` had **zero** audio warnings, underruns or "non-monotonous" timestamps during the audited stream, and
  no sample-rate conversion happens on the live path (everything is already 48 kHz). There is no evidence of crackling in the
  logs; if it is heard, the following are the likely mechanisms, in order:
  1. **Wall-clock timestamps + `async=1`.** Each audio packet is stamped with the time it *arrived*. When Chrome or ffmpeg is
     briefly starved, packets arrive in a burst; `aresample=async=1` only does *hard* compensation (inserts silence or drops
     samples once the error passes `min_hard_comp`, 0.1 s by default). A hard correction is an audible click.
  2. **Chrome's audio thread starved** by its own software compositor (above) - the Pulse sink then plays a gap.
  3. Upmix mono -> stereo is lossless duplication; not a crackle source.
* Sync: video (x11grab) and audio (pulse) are both stamped with wall-clock time at capture, so they stay aligned with each other;
  any lip-sync offset seen is Zoom's own (its audio and video arrive separately).

## Prepared audio options (none applied; each needs a local A/B and your approval)

| # | Change in `stream.sh` | Effect | Risk |
|---|------------------------|--------|------|
| A1 | `aresample=async=1000:min_hard_comp=0.100:first_pts=0` | Small timing errors are absorbed by *stretching* (up to ~2%), hard insert/drop only for >100 ms gaps: removes most clicks | Barely audible pitch wobble during heavy jitter |
| A2 | Drop `-use_wallclock_as_timestamps 1` on the **pulse** input only (keep `-wallclock` default) | Timestamps follow the sample count, so bursty arrival no longer looks like a gap | Long-run drift vs. video possible; must be checked over a 2 h test |
| A3 | Give the audio server priority (`rtkit`/`nice -5` for pipewire/pulse) | Fewer gaps under CPU load | Unit/config change on the box |

Verify with a recording, not by ear: `ffmpeg -i rec.mp4 -af astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.Number_of_NaNs -f null -`
plus `silencedetect=n=-60dB:d=0.02` to count the micro-gaps before/after, at the same CPU load (`measure-cpu.sh`).

## Prepared Chrome kiosk options (none applied)

| # | Option | Expected effect | Risk |
|---|--------|-----------------|------|
| C1 | Zoom web client: gallery limited / HD video off (per-meeting settings) | Less decoding and compositing | Lower picture detail of participants |
| C2 | Smaller kiosk window / device scale factor 1 (already 1) | Compositor work scales with pixels | Output resolution |
| C3 | Keep `--disable-gpu` | There is no GPU on c5a; removing it makes Chrome fall back to SwiftShader, which is **not** cheaper | - |

The production encoder settings, Chrome flags and audio chain are unchanged by the V2 bundle.
