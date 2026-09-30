"""Unprivileged system statistics (CPU load/frequency, memory, NVIDIA GPU details).

The NVIDIA query is deliberately conservative: nvidia-smi is only run when the
discrete GPU is already awake *and* the laptop is on AC power, so the monitor
never wakes a sleeping dGPU or keeps it from runtime-suspending on battery.
"""

import glob
import shutil
import subprocess
import time

from .hw import p, read, read_int


class SysInfo:
    def __init__(self):
        self._prev_cpu = None
        self._gpu_cache = (0.0, None)
        self.nvidia_smi = shutil.which("nvidia-smi")
        self.cpu_name = self._cpu_name()
        self.gpu_names = self._gpu_names()

    def _cpu_name(self):
        try:
            with open(p("/proc/cpuinfo")) as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return None

    def _gpu_names(self):
        if not shutil.which("lspci"):
            return []
        try:
            out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        names = []
        for line in out.splitlines():
            if " VGA " in line or "3D controller" in line:
                names.append(line.split(": ", 1)[-1])
        return names

    def cpu_usage(self):
        try:
            with open(p("/proc/stat")) as f:
                fields = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields[:8])
        prev, self._prev_cpu = self._prev_cpu, (idle, total)
        if not prev or total == prev[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (idle - prev[0]) / float(total - prev[1]))))

    def cpu_freq(self):
        freqs = [read_int(f) for f in glob.glob(p("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))]
        freqs = [f for f in freqs if f]
        if not freqs:
            return None
        return {"avg": sum(freqs) / len(freqs) / 1e6, "max": max(freqs) / 1e6}

    def memory(self):
        info = {}
        try:
            with open(p("/proc/meminfo")) as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) * 1024
        except (OSError, ValueError):
            return None
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        if not total or avail is None:
            return None
        return {"total": total, "used": total - avail, "pct": 100.0 * (total - avail) / total}

    def uptime(self):
        v = read(p("/proc/uptime"))
        return float(v.split()[0]) if v else None

    def nvidia(self, dgpu_state, ac_online):
        if not self.nvidia_smi or dgpu_state != "active" or not ac_online:
            return None
        ts, cached = self._gpu_cache
        if time.monotonic() - ts < 2.0:
            return cached
        data = None
        try:
            out = subprocess.run(
                [self.nvidia_smi, "--query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.gr,memory.used,memory.total",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout
            vals = [v.strip() for v in out.strip().split(",")]
            if len(vals) == 6:
                def num(v):
                    try:
                        return float(v)
                    except ValueError:
                        return None
                data = {"util": num(vals[0]), "temp": num(vals[1]), "power": num(vals[2]),
                        "clock": num(vals[3]), "mem_used": num(vals[4]), "mem_total": num(vals[5])}
        except (OSError, subprocess.SubprocessError):
            data = None
        self._gpu_cache = (time.monotonic(), data)
        return data

    def sample(self, dgpu_state=None, ac_online=None):
        return {
            "cpu_usage": self.cpu_usage(),
            "cpu_freq": self.cpu_freq(),
            "memory": self.memory(),
            "uptime": self.uptime(),
            "nvidia": self.nvidia(dgpu_state, ac_online),
        }
