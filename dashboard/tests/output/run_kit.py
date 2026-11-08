"""Run the whole output test kit and keep everything for a human look:
    python tests/output/run_kit.py --out C:/tmp/output-kit
Writes <out>/report.json, <out>/report.md, per case <case>.mp4 (H.264 with stream.sh's settings) and <case>_sheet.png
(before | after). Exit code 0 = every Auto case passed."""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import outputkit as K  # noqa: E402

# What production did before, on the same 1920x1080 webinar screen (for comparison only).
OLD_MEETING = {
    "old_meeting_only_fill": "crop=1650:928:135:56,scale=1920:1080:flags=lanczos,setsar=1",
    "old_meeting_only_fit": ("crop=1920:928:0:56,scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                             "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1"),
    "old_fullscreen_live": "",
    "old_clean_resample_1004": "crop=1912:935:4:69,scale=1920:938:flags=lanczos,pad=1920:1080:0:71:color=0x0B0D10,setsar=1",
}


def line(r):
    black = [k for k, v in r["black_edges"].items() if v]
    s = (f"{r['case']:44s} {r['state']:10s} corners={sum(r['corners_kept'].values())}/4 circle={r.get('circle_wh')} "
         f"ui_rows={r['zoom_ui_rows']} black={black or '-'} ssim={r.get('ssim')} psnr={r.get('psnr')} "
         f"sharp={r.get('text_sharpness')}")
    if "verdict" in r:
        s += "  -> " + ("PASS" if r["verdict"]["pass"] else "FAIL " + str([k for k, v in r["verdict"]["checks"].items() if not v]))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="output-kit-results")
    ap.add_argument("--scenes", nargs="*", default=list(K.SCENES))
    a = ap.parse_args()
    d = pathlib.Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    rows, t0 = [], time.time()
    for name in a.scenes:
        sc = K.make_scene(name, d)
        for mode in ("preserve", "fit", "fill"):
            r = K.run_case(sc, mode, d)
            if mode == "preserve":
                r["verdict"] = K.verdict(r)
            rows.append(r)
            print(line(r), flush=True)
        if name == "meeting_16x9":
            for label, vf in OLD_MEETING.items():
                r = K.run_case(sc, "custom", d, vf_override=vf, label=f"meeting_16x9__{label}")
                rows.append(r)
                print(line(r), flush=True)
    (d / "report.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    auto = [r for r in rows if r["mode"] == "preserve"]
    md = ["| scene | Auto result | corners | circle w x h | black bars | Zoom UI rows | text SSIM | PSNR dB | sharpness | verdict |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in auto:
        md.append(f"| {r['scene']} | {r['label']} | {sum(r['corners_kept'].values())}/4 | {r.get('circle_wh')} | "
                  f"{[k for k, v in r['black_edges'].items() if v] or 'none'} | {r['zoom_ui_rows']} | {r.get('ssim')} | "
                  f"{r.get('psnr')} | {r.get('text_sharpness')} | {'PASS' if r['verdict']['pass'] else 'FAIL'} |")
    (d / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    ok = all(r["verdict"]["pass"] for r in auto)
    print(f"\n{sum(r['verdict']['pass'] for r in auto)}/{len(auto)} Auto cases passed in {time.time() - t0:.0f}s -> {d}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
