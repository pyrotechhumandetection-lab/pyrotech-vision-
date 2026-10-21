#!/bin/sh
# PYROTECH VISION - read-only inspection of an AI box (hardware, accelerators, AI software, load, streams).
#
#   sh aibox_inspect.sh            # ~2 minutes, writes ./aibox_report_<host>_<time>.txt
#   sudo sh aibox_inspect.sh       # same, plus NPU/GPU load counters and all process names (recommended)
#
# Includes a MODEL section: which classes the box's models detect, model family, input size, precision -
# i.e. what can be learned about how they were trained (the training images themselves are never in a model).
#
# Changes NOTHING: no installs, no restarts, no config edits. Passwords in RTSP URLs / command lines are
# masked; environment variables and config file CONTENTS are never printed (only file names).
# Optional: CAMERA_URL="rtsp://user:pass@ip:554/..." sh aibox_inspect.sh  -> measures the camera's real fps
# with ffprobe if it is installed (the URL itself is not written to the report).

OUT="aibox_report_$(hostname 2>/dev/null || echo box)_$(date +%Y%m%d_%H%M%S).txt"
mask() { LC_ALL=C tr -cd '\11\12\15\40-\176' | sed -E 's#(rtsps?|https?)://[^/@ ]*@#\1://***@#g; s#(pass(word)?|pwd|token|secret|key)=[^ &]*#\1=***#Ig'; }
sec() { printf '\n==================== %s ====================\n' "$1"; }
run() { printf '\n$ %s\n' "$*"; sh -c "$*" 2>&1 | mask | head -n "${LINES_MAX:-120}"; }
have() { command -v "$1" >/dev/null 2>&1; }

{
sec "SYSTEM"
run "date; uptime"
run "uname -a"
run "cat /etc/os-release"
run "cat /proc/device-tree/model 2>/dev/null; echo; cat /sys/firmware/devicetree/base/compatible 2>/dev/null | tr '\0' ' '"
run "lscpu 2>/dev/null || cat /proc/cpuinfo | grep -E 'model name|Hardware|processor' | head -20"
run "nproc; free -m"
run "df -h -x tmpfs -x devtmpfs"
run "lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,MODEL 2>/dev/null"
run "cat /sys/class/thermal/thermal_zone*/type 2>/dev/null | paste - - - - - -; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | paste - - - - - -"

sec "ACCELERATORS (GPU / NPU / VPU)"
run "lspci 2>/dev/null | grep -iE 'vga|3d|nvidia|accel|npu'"
run "nvidia-smi 2>/dev/null || echo 'no nvidia-smi'"
run "ls /dev 2>/dev/null | grep -iE 'npu|rknpu|dri|nvhost|nvidia|video|mpp|vpu|rga|hailo|apex|galcore|mali|accel' | tr '\n' ' '"
run "lsmod 2>/dev/null | grep -iE 'npu|rknpu|galcore|vpu|mali|hailo|apex|nvgpu|nvidia|mpp|rga'"
run "cat /sys/kernel/debug/rknpu/version /sys/kernel/debug/rknpu/load 2>/dev/null || echo 'rknpu debugfs not readable (Rockchip only, needs sudo)'"
run "cat /sys/class/devfreq/*/name 2>/dev/null; for d in /sys/class/devfreq/*; do echo \"\$d: \$(cat \$d/cur_freq 2>/dev/null) / \$(cat \$d/max_freq 2>/dev/null) load=\$(cat \$d/load 2>/dev/null)\"; done"
run "cat /etc/nv_tegra_release 2>/dev/null || echo 'not a Jetson'"
have tegrastats && run "timeout 6 tegrastats --interval 2000"

sec "AI SOFTWARE"
run "python3 --version; pip3 --version"
run "pip3 list 2>/dev/null | grep -iE 'torch|ultralytics|onnx|rknn|openvino|tensorrt|tflite|tensorflow|opencv|paddle|mmdet|hailo|numpy'"
run "ls /usr/lib/librknn* /usr/lib/aarch64-linux-gnu/librknn* /usr/lib/librga* /usr/lib/libnvinfer* /usr/lib/aarch64-linux-gnu/libnvinfer* /opt/* 2>/dev/null"
run "docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}' 2>/dev/null || echo 'no docker'"
MODELS=/tmp/.aib_models
find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /run \) -prune -o -type f \
  \( -name '*.rknn' -o -name '*.onnx' -o -name '*.engine' -o -name '*.trt' -o -name '*.pt' -o -name '*.tflite' \
     -o -name '*.om' -o -name '*.hef' -o -name '*.wk' -o -name '*.nb' -o -name '*.bmodel' \) \
  -size +300k -print 2>/dev/null | grep -v -E 'site-packages/.*/tests|/share/doc' | head -80 > "$MODELS"
printf '\n$ model files (max 80)\n'
while read -r f; do printf '%s  %s\n' "$(du -h "$f" 2>/dev/null | cut -f1)" "$f"; done < "$MODELS"

sec "MODELS: WHAT THEY DETECT AND HOW THEY WERE BUILT"
echo "Training IMAGES are never stored in a model. What can be read: class list, model family, input size,"
echo "precision, toolkit/version. 80 classes in COCO order (person, bicycle, car, motorcycle, airplane, bus...)"
echo "= generic public COCO model; a short custom list = trained by the vendor on its own data."
# printable text of a binary file (busybox-safe fallback when 'strings' is missing)
txt() { if have strings; then strings -n 5 "$1"; else tr -c '[:print:]' '\n' < "$1" | grep -E '.{5,}'; fi; }
n=0
while read -r f; do
  n=$((n+1)); [ "$n" -gt 12 ] && { echo "(more models not shown)"; break; }
  printf '\n----- %s (%s)\n' "$f" "$(du -h "$f" 2>/dev/null | cut -f1)"
  have file && file -b "$f" | head -c 200 && echo
  # model family / toolkit / target / precision / input size hints
  txt "$f" 2>/dev/null | grep -iE 'yolo|ultralytics|coco|dataset|annotation|train|data\.yaml|imgsz|stride|names|rknn|toolkit|target_platform|rk35|rk3588|int8|fp16|float16|quant|mean_values|std_values|input.*shape|tensorrt|openvino|paddle|ssd|mobilenet|retina|nanodet|efficientdet|version|author|description|date' \
    | grep -E '[A-Za-z]{4}' | grep -vE '^.{200,}$' | sort | uniq | head -40 | mask
  # COCO class names present inside the file?
  # COCO class names inside the file (distinctive ones; also matches pickled .pt names / coco.yaml references)
  c=$(txt "$f" 2>/dev/null | grep -ciE "toothbrush|hair drier|fire hydrant|teddy bear|parking meter|coco\.yaml|coco128")
  [ "${c:-0}" -gt 0 ] && echo ">> COCO markers found ($c): generic COCO classes / trained from coco.yaml"
  # ONNX: metadata + input/output shapes (only if python3 has the onnx package)
  case "$f" in *.onnx)
    python3 - "$f" <<'PY' 2>/dev/null || echo "(python onnx package not installed - shapes not read)"
import sys, onnx
m = onnx.load(sys.argv[1], load_external_data=False)
print("metadata:", {p.key: p.value[:300] for p in m.metadata_props})
dims = lambda t: [d.dim_value or d.dim_param for d in t.type.tensor_type.shape.dim]
print("inputs :", [(i.name, dims(i)) for i in m.graph.input][:4])
outs = [(o.name, dims(o)) for o in m.graph.output][:6]
print("outputs:", outs)
for _, d in outs:
    if len(d) == 3 and all(isinstance(x, int) and x for x in d):
        a, b = d[1], d[2]
        if min(a, b) in range(5, 400):
            k = min(a, b)
            print(f"   classes guess: YOLOv8-style {k}-4 = {k-4}, or YOLOv5-style {k}-5 = {k-5}")
print("opset:", [o.version for o in m.opset_import][:3], "producer:", m.producer_name, m.producer_version)
PY
  ;; esac
done < "$MODELS"

printf '\n$ label / class-name files (first 100 lines each, max 6 files)\n'
find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path '*site-packages*' -o -path '*dist-packages*' -o -path /usr/share \) -prune -o -type f \
  \( -iname '*label*.txt' -o -iname '*labels*' -o -iname '*.names' -o -iname '*class*.txt' -o -iname '*coco*.txt' -o -iname '*classes*' \) \
  -size -64k -print 2>/dev/null | head -6 | while read -r f; do
    printf '\n----- %s (%s lines)\n' "$f" "$(wc -l < "$f")"; head -n 100 "$f" | mask; done
printf '\n$ config files of AI / video apps (names only)\n'
find /etc /opt /home /root /usr/local /app /data -maxdepth 4 -type f \( -iname '*.conf' -o -iname '*.json' -o -iname '*.yaml' -o -iname '*.yml' -o -iname '*.ini' \) 2>/dev/null \
  | grep -iE 'ai|detect|camera|video|stream|rtsp|algo|model|infer|vision|nvr|vms' | head -60

sec "RUNNING SERVICES AND PROCESSES"
run "systemctl list-units --type=service --state=running --no-pager 2>/dev/null | head -60"
LINES_MAX=40 run "ps -eo pid,ppid,etimes,pcpu,pmem,rss,nlwp,comm,args --sort=-pcpu"
run "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null"

sec "VIDEO STREAMS (who pulls cameras / who serves video)"
run "ss -tnp 2>/dev/null | grep -E ':554 |:8554 |:80 |:8000 ' | head -40"
run "echo 'RTSP connections OUT to cameras (:554):'; ss -tn state established '( dport = :554 )' 2>/dev/null | tail -n +2 | wc -l"
printf '\n$ network receive/transmit rate per interface (10 s)\n'
cat /proc/net/dev > /tmp/.aib1; sleep 10; cat /proc/net/dev > /tmp/.aib2
awk 'NR==FNR{if(NR>2){gsub(":"," ");a[$1]=$2;b[$1]=$10};next} FNR>2{gsub(":"," ");printf "%-12s RX %7.2f Mbit/s   TX %7.2f Mbit/s\n",$1,($2-a[$1])*8/1e7,($10-b[$1])*8/1e7}' /tmp/.aib1 /tmp/.aib2
rm -f /tmp/.aib1 /tmp/.aib2

sec "LOAD OVER 60 s (CPU per process, every 15 s)"
for i in 1 2 3 4; do
  printf '\n--- sample %s  %s\n' "$i" "$(date +%T)"
  top -b -n 1 2>/dev/null | head -n 22 | mask
  [ -r /sys/kernel/debug/rknpu/load ] && printf 'NPU: %s\n' "$(cat /sys/kernel/debug/rknpu/load)"
  have nvidia-smi && nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null
  [ "$i" -lt 4 ] && sleep 15
done

sec "CAMERA INPUT FPS (only with CAMERA_URL and ffprobe)"
if [ -n "$CAMERA_URL" ] && have ffprobe; then
  ffprobe -v error -rtsp_transport tcp -select_streams v:0 -show_entries stream=codec_name,width,height,avg_frame_rate,r_frame_rate -of default=nw=1 "$CAMERA_URL" 2>&1 | mask
  printf 'frames received in 10 s: '
  timeout 15 ffprobe -v error -rtsp_transport tcp -read_intervals %+10 -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of csv=p=0 "$CAMERA_URL" 2>&1 | mask
else
  echo "skipped (set CAMERA_URL and install ffprobe to measure)"
fi

sec "RECENT WARNINGS / ERRORS (system log, last 80)"
run "journalctl -p warning -n 80 --no-pager 2>/dev/null || tail -n 80 /var/log/syslog 2>/dev/null || dmesg 2>/dev/null | tail -n 80"
run "dmesg 2>/dev/null | grep -iE 'oom|killed process|npu|rknpu|thermal|throttl' | tail -20"

rm -f "$MODELS"
sec "DONE"
echo "report: $OUT"
} > "$OUT" 2>&1
echo "Report written: $(pwd)/$OUT  - attach this file in the chat (passwords in URLs are masked; please glance over it before sharing)."
