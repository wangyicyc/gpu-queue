# gq — GPU Task Queue

**[English](#english) | [中文](#中文)**

---

## 中文

一个轻量的本地 GPU 任务队列工具：你把命令丢进队列，它盯着 GPU，一旦空闲就自动跑下一个，跑完一个接一个。适合单机跑一串实验（不同 seed、不同超参）时不用手动等。

### 特点

- **单文件、零依赖**：纯 Python 标准库，一个 `gq` 脚本，无需 `pip install`
- **空闲检测按"你的进程"判断**：不是看 GPU 利用率数字，而是看 `nvidia-smi` 里有没有**当前用户**的进程在占 GPU。你的进程结束 = 空闲 = 拖下一个任务，比显存阈值更准
- **一次只跑一个**：同步执行，stdout/stderr 直接打到 daemon 终端
- **安全**：fcntl 文件锁防并发写坏；Ctrl-C 一次优雅停、连按两次强制杀当前任务；daemon 崩溃重启能识别并清理孤儿进程
- **任意 shell 命令**：`python train.py`、`bash run.sh` 都能排队

### 安装

```bash
# 一行装好（把 gq 放到 ~/.local/bin）
curl -fsSL https://raw.githubusercontent.com/<你的用户名>/gpu-queue/main/gq -o ~/.local/bin/gq
chmod +x ~/.local/bin/gq

# 确保 ~/.local/bin 在 PATH 里（加到 ~/.bashrc）
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# 验证
gq --help
```

> 也可以直接 `git clone` 后 `cp gq ~/.local/bin/gq && chmod +x ~/.local/bin/gq`。

### 用法

在 tmux 里开两个窗口（daemon 要常驻，tmux 保证关终端不死）：

```bash
# 窗口 A：常驻 daemon（任务输出打到这里）
gq watch --poll 15        # 每 15 秒检查一次 GPU

# 窗口 B：加任务、看状态
gq add 'python train.py --seed 1'
gq add 'python train.py --seed 2'
gq add 'python train.py --seed 3'
gq list                   # 看队列 + 当前运行状态
gq cancel <id>            # 取消某个排队中的任务（支持 ID 前缀）
gq clear                  # 清空队列（不影响正在跑的）
```

GPU 一空闲，daemon 自动把队列里的任务挂上去跑，一个接一个。

### 命令一览

| 命令 | 作用 |
|------|------|
| `gq watch [--poll N]` | 启动 daemon（默认 15 秒轮询；在 tmux 里跑） |
| `gq add 'cmd'` | 追加任务到队尾（用当前目录作为工作目录） |
| `gq list` | 查看运行中的任务 + 待跑队列 + daemon 状态 |
| `gq cancel <id>` | 按 ID 或唯一前缀移除排队中的任务 |
| `gq clear` | 清空所有待跑任务 |

### 空闲检测原理

调用 `nvidia-smi pmon -s um -c 1` 获取当前所有占 GPU 的进程，逐个检查 `/proc/<pid>` 的属主是否为当前用户（`os.getuid()`）：

- **没有任何我的进程** → 空闲 → 拖下一个任务
- **有我的进程**（不管占多少显存/算力） → 忙 → 等下一次轮询
- `nvidia-smi` 调用失败 → 当作"忙"（安全默认，不会误启动）

### Ctrl-C 行为

- **第一次 Ctrl-C**：打印提示，等当前任务自然跑完，再退出
- **第二次 Ctrl-C**：强制杀当前任务及其子进程组（SIGKILL），立即退出

### 状态文件

运行状态存于 `~/.gpu-queue/`：

- `queue.json` —— 待跑任务列表
- `state.json` —— daemon PID + 当前运行任务

两个文件都受 `fcntl.flock` 保护，CLI 和 daemon 同时读写不会写坏。文件损坏会自动重置并告警，不会崩溃。

### 测试

```bash
git clone https://github.com/<你的用户名>/gpu-queue.git
cd gpu-queue
python -m pytest tests/ -v
```

38 个测试，覆盖 GPU 检测、文件锁、并发安全、命令、daemon 循环、信号处理、崩溃恢复。

### 限制

- 一次只跑一个任务（单 GPU、单用户场景设计）
- 不支持多 GPU 调度、多用户、集群（那是 Slurm 的活）
- 任务会以前台方式打到 daemon 终端，没有独立日志文件（设计如此，用 tmux 的滚动缓冲即可）

### 许可证

MIT

---

## English

A lightweight local GPU job queue: throw shell commands into a queue, and `gq` watches the GPU — as soon as it's idle, it launches the next job automatically, one after another. Handy for running a sweep of experiments on a single workstation without babysitting each run.

### Features

- **Single file, zero dependencies** — pure Python stdlib, one `gq` script, no `pip install`
- **Idle = "your process is gone"** — instead of GPU utilization thresholds, it checks `nvidia-smi` for any process owned by the **current user**. Your process ends → idle → next job runs. More accurate than memory thresholds.
- **One job at a time** — synchronous execution, stdout/stderr stream straight to the daemon's terminal
- **Safe** — `fcntl` file locks prevent corruption; Ctrl-C once = graceful, twice = force-kill; crash recovery detects and reaps orphaned subprocesses
- **Any shell command** — `python train.py`, `bash run.sh`, anything

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/<your-username>/gpu-queue/main/gq -o ~/.local/bin/gq
chmod +x ~/.local/bin/gq

# Ensure ~/.local/bin is on PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

gq --help
```

### Usage

Run the daemon in tmux (so it survives terminal close), add jobs from another pane:

```bash
gq watch --poll 15          # pane A: daemon, prints job output here
gq add 'python train.py --seed 1'   # pane B
gq add 'python train.py --seed 2'
gq list
gq cancel <id>
gq clear
```

### Commands

| Command | Description |
|---------|-------------|
| `gq watch [--poll N]` | Start the daemon (default poll 15s; run in tmux) |
| `gq add 'cmd'` | Append a job (uses current dir as cwd) |
| `gq list` | Show running job + pending queue + daemon status |
| `gq cancel <id>` | Remove a pending job by ID or unique prefix |
| `gq clear` | Clear all pending jobs |

### Idle detection

`nvidia-smi pmon -s um -c 1` lists all GPU-occupying processes; each PID's owner is checked via `/proc/<pid>`. No process owned by you → idle. `nvidia-smi` failure → treated as busy (safe default).

### Tests

```bash
python -m pytest tests/ -v    # 38 tests
```

### License

MIT
