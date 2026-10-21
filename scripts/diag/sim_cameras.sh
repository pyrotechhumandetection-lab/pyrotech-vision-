#!/bin/sh
# PYROTECH VISION - simulate N cameras from ONE real camera, and measure the AI box under that load.
#
# A temporary MediaMTX relay on the box pulls the camera ONCE and serves it N times:
#     rtsp://127.0.0.1:8554/sim1 ... /simN      (the camera itself sees a single connection)
# Add those N URLs as cameras in the vendor app (or in PYROTECH VISION), then run "monitor".
#
#   sudo CAMERA_URL='rtsp://user:pass@192.168.192.87:554/Streaming/Channels/101' sh sim_cameras.sh start [N]
#   sudo sh sim_cameras.sh monitor [SECONDS]      # every 5 s: NPU, CPU per AI process, network, open streams
#   sudo sh sim_cameras.sh status
#   sudo sh sim_cameras.sh stop                   # stops the relay, deletes its config (it holds the camera password)
#
# Optional: SIM_BIND=0.0.0.0  -> also reachable from the LAN (read-only user + random password, printed in the URLs);
#           default 127.0.0.1 = only programs on this box can read the simulated cameras.
#           SIM_DIR=/userdata/pyrotech-sim (default; the root partition of RK boxes is usually nearly full)
#           SIM_PORT=8554
# Changes nothing on the system: no services, packages or vendor files are touched. Everything lives in SIM_DIR.
# All N streams carry the same picture - valid for load (decode + NPU + CPU), not for accuracy.

MTX_VERSION=v1.9.3
SIM_DIR=${SIM_DIR:-/userdata/pyrotech-sim}
[ -d "$(dirname "$SIM_DIR")" ] || SIM_DIR=$HOME/pyrotech-sim
PORT=${SIM_PORT:-8554}
BIND=${SIM_BIND:-127.0.0.1}
CONF=$SIM_DIR/mediamtx.yml
PIDF=$SIM_DIR/mediamtx.pid
INFO=$SIM_DIR/urls.txt
LOG=$SIM_DIR/mediamtx.log
mask() { sed -E 's#(rtsps?)://[^/@ ]*@#\1://***@#g'; }
die() { echo "ERROR: $*" >&2; exit 1; }
alive() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

get_mediamtx() {
  [ -x "$SIM_DIR/mediamtx" ] && return
  case "$(uname -m)" in
    aarch64|arm64) a=linux_arm64v8 ;; armv7l) a=linux_armv7 ;; x86_64) a=linux_amd64 ;; *) die "unsupported CPU $(uname -m)" ;;
  esac
  url="https://github.com/bluenviron/mediamtx/releases/download/$MTX_VERSION/mediamtx_${MTX_VERSION}_$a.tar.gz"
  echo "downloading MediaMTX $MTX_VERSION ($a) ..."
  if command -v wget >/dev/null; then wget -q -O "$SIM_DIR/m.tgz" "$url"; else curl -fsSL -o "$SIM_DIR/m.tgz" "$url"; fi \
    || die "download failed - copy mediamtx_${MTX_VERSION}_$a.tar.gz into $SIM_DIR by hand and run start again"
  tar -xzf "$SIM_DIR/m.tgz" -C "$SIM_DIR" mediamtx && rm -f "$SIM_DIR/m.tgz" || die "could not unpack MediaMTX"
}

cmd_start() {
  N=${1:-8}
  [ -n "$CAMERA_URL" ] || die "set CAMERA_URL='rtsp://user:pass@ip:554/...' (the real camera)"
  case "$CAMERA_URL" in rtsp://*|rtsps://*) ;; *) die "CAMERA_URL must start with rtsp://" ;; esac
  alive && die "relay already running (stop it first)"
  if command -v ss >/dev/null && ss -tln | grep -q ":$PORT "; then die "port $PORT already in use - set SIM_PORT"; fi
  mkdir -p "$SIM_DIR" && chmod 700 "$SIM_DIR"
  get_mediamtx
  q() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
  cred=""
  {
    echo "# temporary camera simulator - written by sim_cameras.sh; contains the camera password"
    echo "logLevel: info"
    echo "logDestinations: [stdout]"
    echo "readTimeout: 10s"
    echo "writeTimeout: 10s"
    echo "writeQueueSize: 1024"
    echo "api: no"; echo "metrics: no"; echo "pprof: no"; echo "playback: no"
    echo "rtmp: no"; echo "hls: no"; echo "webrtc: no"; echo "srt: no"
    echo "rtsp: yes"; echo "protocols: [tcp]"; echo "encryption: \"no\""
    echo "rtspAddress: $BIND:$PORT"
    echo "authMethod: internal"
    echo "authInternalUsers:"
    if [ "$BIND" = "127.0.0.1" ]; then
      echo "- user: any"; echo "  ips: ['127.0.0.1/32']"; echo "  permissions:"; echo "  - action: read"; echo "    path:"
    else
      p=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 20); cred="sim:$p@"
      echo "- user: sim"; echo "  pass: $p"; echo "  permissions:"; echo "  - action: read"; echo "    path:"
    fi
    echo "pathDefaults:"
    echo "  rtspTransport: tcp"
    echo "paths:"
    echo "  cam:"
    echo "    source: $(q "$CAMERA_URL")"
    echo "    sourceOnDemand: no"
    i=1; while [ "$i" -le "$N" ]; do
      echo "  sim$i:"
      echo "    source: rtsp://${cred}127.0.0.1:$PORT/cam"
      echo "    sourceOnDemand: yes"
      echo "    sourceOnDemandStartTimeout: 15s"
      i=$((i+1)); done
  } > "$CONF"
  chmod 600 "$CONF"
  nohup "$SIM_DIR/mediamtx" "$CONF" > "$LOG" 2>&1 &
  echo $! > "$PIDF"; chmod 600 "$LOG"
  i=0; while [ $i -lt 30 ]; do
    grep -q "\[path cam\] .*ready" "$LOG" 2>/dev/null && break
    alive || { mask < "$LOG" | tail -20; die "relay exited (log above)"; }
    sleep 1; i=$((i+1)); done
  grep -q "\[path cam\] .*ready" "$LOG" || { mask < "$LOG" | tail -20; echo "WARNING: camera not ready yet (log above) - check CAMERA_URL"; }
  host=$BIND; [ "$BIND" = "0.0.0.0" ] && host=$(hostname -I 2>/dev/null | awk '{print $1}')
  : > "$INFO"; i=1; while [ "$i" -le "$N" ]; do echo "rtsp://${cred}$host:$PORT/sim$i" >> "$INFO"; i=$((i+1)); done
  chmod 600 "$INFO"
  echo "relay running (PID $(cat "$PIDF")), camera pulled ONCE. Add these $N cameras in the vendor app / PYROTECH VISION:"
  cat "$INFO"
  echo "then:  sudo sh $0 monitor 120"
}

npu() { cat /sys/kernel/debug/rknpu/load 2>/dev/null | sed 's/NPU load: *//; s/Core/C/g; s/ //g; s/,$//; s/,/ /g'; }
rx() { awk 'NR>2{gsub(":"," "); if($1!="lo") s+=$2} END{print s}' /proc/net/dev; }

cmd_monitor() {
  SECS=${1:-120}
  [ -r /sys/kernel/debug/rknpu/load ] || echo "note: NPU load not readable - run with sudo"
  mkdir -p "$SIM_DIR"; CSV=$SIM_DIR/monitor_$(date +%Y%m%d_%H%M%S).csv
  echo "time,npu,cpu_idle_pct,rx_mbit,sim_readers,camera_conns,algo_cpu,backend_cpu,mediaserver_cpu,relay_cpu,java_cpu" > "$CSV"
  printf '%-8s %-22s %6s %7s %5s %5s %7s %7s %7s %7s %7s\n' time npu cpuIdle RXMbit simRd camCn algo% api% media% relay% java%
  end=$(( $(date +%s) + SECS ))
  while [ "$(date +%s)" -lt "$end" ]; do
    r0=$(rx)
    # top: 2 iterations 5 s apart, only the second one (current CPU, not since start)
    t=$(top -b -n 2 -d 5 2>/dev/null | awk '/^top -/{n++} n==2')
    r1=$(rx)
    idle=$(printf '%s\n' "$t" | awk -F',' '/Cpu\(s\)/{for(i=1;i<=NF;i++) if($i~/id/){gsub(/[^0-9.]/,"",$i); print $i}}')
    cpu() { printf '%s\n' "$t" | awk -v n="$1" '$12 ~ n {s+=$9} END{printf "%.1f", s}'; }
    # readers = connections TO the simulator owned by other programs (the relay's own sim->cam pulls excluded)
    rd=$(ss -tnp state established "( dport = :$PORT )" 2>/dev/null | tail -n +2 | grep -vc '"mediamtx"')
    cc=$(ss -tn state established '( dport = :554 )' 2>/dev/null | tail -n +2 | wc -l)
    mb=$(awk -v a="$r0" -v b="$r1" 'BEGIN{printf "%.2f", (b-a)*8/5/1e6}')
    row="$(date +%T),$(npu),$idle,$mb,$rd,$cc,$(cpu iismart),$(cpu '^api$'),$(cpu MediaSe),$(cpu mediamtx),$(cpu '^java$')"
    echo "$row" >> "$CSV"
    echo "$row" | awk -F, '{printf "%-8s %-22s %6s %7s %5s %5s %7s %7s %7s %7s %7s\n",$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11}'
  done
  echo "saved: $CSV"
  awk -F, 'NR>1{n++; i+=$3; m+=$4; a+=$7; k+=$8} END{if(n) printf "average: CPU idle %.0f%%, RX %.1f Mbit/s, vendor engine %.0f%% of one core, PYROTECH backend %.0f%%\n", i/n, m/n, a/n, k/n}' "$CSV"
  echo "simRd = streams being read from the simulator (should equal the number of simulated cameras added);"
  echo "camCn = connections to real cameras on port 554 (the simulator itself is 1 of them)."
}

cmd_status() {
  if alive; then echo "relay running, PID $(cat "$PIDF")"; [ -f "$INFO" ] && mask < "$INFO"
  else echo "relay not running"; fi
  [ -f "$LOG" ] && { echo "--- last relay log lines"; mask < "$LOG" | tail -8; }
}

cmd_stop() {
  if alive; then kill "$(cat "$PIDF")"; sleep 1; echo "relay stopped"; else echo "relay was not running"; fi
  rm -f "$PIDF" "$CONF" "$INFO"          # the config holds the camera password
  echo "config removed (MediaMTX binary, log and monitor CSVs kept in $SIM_DIR)"
}

case "$1" in
  start) shift; cmd_start "$@" ;;
  monitor) shift; cmd_monitor "$@" ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
