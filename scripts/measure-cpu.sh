#!/usr/bin/env bash
# READ-ONLY CPU sampler for the streaming pipeline. Reads /proc only; changes nothing, starts nothing, prints no command lines
# (command lines can contain the stream key) - only the process name and its CPU share.
#
#   measure-cpu.sh [SECONDS=60] [INTERVAL=5]
#
# Output: per process group, the average share of ONE core (100 = one full core; the VPS has 4) over the window, plus the
# total, so "where is the CPU going?" has a measured answer before anything is tuned. Groups: ffmpeg (encoder),
# chrome (browser source / Zoom web client, software rendering), zoom (desktop client), Xvfb, pulseaudio/pipewire, other.
set -u
DUR="${1:-60}"; STEP="${2:-5}"
CLK=$(getconf CLK_TCK)
declare -A first last
snap() {   # prints "group utime+stime" per pid
  for d in /proc/[0-9]*; do
    [ -r "$d/stat" ] || continue
    read -r line <"$d/stat" 2>/dev/null || continue
    comm=${line#*(}; comm=${comm%%)*}; rest=${line##*) }
    set -- $rest
    t=$(( ${12} + ${13} ))
    case "$comm" in
      ffmpeg) g=ffmpeg ;; chrome*|google-chrome*|chromium*) g=chrome ;; zoom|zoom.real|ZoomLauncher) g=zoom ;;
      Xvfb) g=Xvfb ;; pulseaudio|pipewire*|pipewire-pulse|wireplumber) g=audio ;; x11vnc) g=x11vnc ;; *) g=other ;;
    esac
    echo "$g ${d#/proc/} $t"
  done
}
sum() { awk '{a[$1]+=$3} END{for(k in a) print k, a[k]}'; }
s0=$(snap | sum); t0=$(date +%s.%N 2>/dev/null || date +%s)
sleep "$DUR"
s1=$(snap | sum); t1=$(date +%s.%N 2>/dev/null || date +%s)
echo "window: ${DUR}s, CLK_TCK=$CLK, cores=$(nproc)"
join <(echo "$s0" | sort) <(echo "$s1" | sort) | awk -v dur="$DUR" -v clk="$CLK" '{d=$3-$2; if (d<0) d=0; p=d/clk/dur*100; printf "%-8s %6.1f %% of one core\n",$1,p; tot+=p} END{printf "%-8s %6.1f %% of one core (%.1f%% of the machine)\n","TOTAL",tot,tot/'"$(nproc)"'}'
echo "(processes that started or exited inside the window are under-counted; sample a steady state)"
