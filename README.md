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
- **自动带上你的环境**：`gq add` 时会快照当前 `conda`/`venv` 环境,daemon 执行时原样还原。`conda activate myenv` 后再 `gq add 'python train.py'`,任务就在 `myenv` 里跑
- **任意 shell 命令**：`python train.py`、`bash run.sh` 都能排队

### 安装

```bash
# 一行装好（把 gq 放到 ~/.local/bin）
curl -fsSL https://raw.githubusercontent.com/wangyicyc/gpu-queue/main/gq -o ~/.local/bin/gq
chmod +x ~/.local/bin/gq

# 确保 ~/.local/bin 在 PATH 里（加到 ~/.bashrc）
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# 验证
gq --help
```

> 也可以直接 `git clone` 后 `cp gq ~/.local/bin/gq && chmod +x ~/.local/bin/gq`。

### 用法

`gq` 是 daemon 模型：你在 tmux 里常驻一个 `gq watch` 守护进程，它盯着 GPU；你从别的窗口用 `gq add` 把命令丢进队列。GPU 一空闲，daemon 自动拖下一个任务跑，一个接一个，直到队列清空。

#### 快速开始

```bash
# 1) 开 tmux，起常驻 daemon（所有任务输出都打到这里）
tmux new -s gq
gq watch --poll 15

# 2) 另开一个 tmux 窗口（Ctrl-B c），加任务
gq add 'python train.py --seed 1'
gq add 'python train.py --seed 2'

# 3) 看状态
gq list
```

`gq list` 大致长这样：

```
[gq] running:
  3f1a  python train.py --seed 1   started 0:04:12 ago  [myenv]

[gq] queue (1 job):
  #1  a9c2  python train.py --seed 2  [myenv]

[gq] daemon: running (pid 12345)
```

三段分别是：**正在跑的任务**（含已运行时长 + 环境名）、**待跑队列**（按顺序编号）、**daemon 状态**（running / stale pid / not running）。

#### 一个任务的生命周期

1. **`gq add`** —— 命令进入 `queue.json` 待跑队列。同时**快照**你当前的 shell 环境（conda/venv、PATH 等）和工作目录。
2. **等待** —— daemon 每 `--poll` 秒检查一次 GPU 是否空闲。
3. **执行** —— 空闲后，daemon 从队首弹出任务，用快照的环境还原后跑（`Popen(env=...)`）。stdout/stderr 直接打到 daemon 终端。
4. **完成** —— 任务结束（exit 0 = DONE，非 0 = FAILED），daemon 清掉运行状态，继续拖下一个。
5. **队列空** —— daemon 继续轮询，等你 `gq add` 新任务。

### 带上你的 conda / venv 环境

这是 `gq` 的关键能力：你 `add` 时激活了什么环境，任务就在什么环境里跑——而不是 daemon 自己的环境。

```bash
# 激活你想用的环境
conda activate myenv          # 或 source ~/.venvs/ml/bin/activate

# 然后 add，gq 会自动记住这个环境
gq add 'python train.py --seed 1'    # → 会在 myenv 里跑
```

每个任务**各自**记住自己 `add` 时的环境，所以可以混排不同环境的任务：

```bash
conda activate torch200 && gq add 'python train.py --seed 1'
conda activate torch210 && gq add 'python train.py --seed 2'
# 两个任务会分别在 torch200 / torch210 里跑
```

`gq list` 会在每个任务后显示环境名（优先 `CONDA_DEFAULT_ENV`，否则 venv 目录名）：

```
  #1  3f1a  python train.py --seed 1  [torch200]
  #2  a9c2  python train.py --seed 2  [torch210]
```

> 环境是 `add` 时刻的**快照**。之后 conda 环境路径变了不会自动更新——符合"用我 add 时的环境跑"的语义。
> 环境快照（可能含 API key 等）会存到 `~/.gpu-queue/` 下，仅本机本用户可读。

### 命令详解

| 命令 | 作用 |
|------|------|
| `gq watch [--poll N]` | 启动 daemon（默认 15 秒轮询；在 tmux 里跑） |
| `gq add 'cmd'` | 追加任务到队尾（用当前目录作 cwd，快照当前环境） |
| `gq list` | 查看运行中的任务 + 待跑队列 + daemon 状态 |
| `gq cancel <id>` | 按 ID 或唯一前缀移除排队中的任务 |
| `gq clear` | 清空所有待跑任务（不影响正在跑的） |

**`gq watch [--poll N]`**　启动守护进程。若已有 daemon 在跑会拒绝启动。启动时做崩溃恢复：发现上次崩溃留下的孤儿任务进程会杀掉它的进程组再清状态。`--poll` 控制检查 GPU 的间隔（秒），默认 15。

**`gq add '<command>'`**　把任意 shell 命令追加到队尾。命令用**当前目录**作为工作目录，并**快照当前环境**。例：`gq add 'bash run.sh'`、`gq add 'python -m foo.bar --x 1'`。

**`gq list`**　三段输出：运行中的任务（含已运行时长 + 环境名）、待跑队列（按顺序编号）、daemon 状态（running / stale pid / not running）。

**`gq cancel <id>`**　按完整 ID 或**唯一前缀**移除一个**排队中**的任务。正在跑的任务不能 cancel——去 daemon 终端按 Ctrl-C。前缀匹配多个任务时会列出所有匹配项并拒绝移除（让你写更具体的前缀）。

**`gq clear`**　清空所有待跑任务，正在跑的不受影响。

### 常见场景

**跑一组超参 sweep：**

```bash
for seed in 1 2 3 4 5; do
  gq add "python train.py --seed $seed"
done
gq list    # 确认 5 个都在队列里
```

**取消排错的任务：**

```bash
gq list              # 看到 #2 是 a9c2，想取消它
gq cancel a9c2       # 或用前缀：gq cancel a9
```

**daemon 崩溃 / 重启电脑后：** 重新 `gq watch` 即可。它会自动清理上次没跑完的孤儿进程，继续处理队列里剩下的任务。

**临时切到别的环境加任务：** 直接 `conda activate 别的env` 再 `gq add`，新任务带新环境；已经在队列里的旧任务不受影响。

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
git clone https://github.com/wangyicyc/gpu-queue.git
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
- **Carries your environment** — `gq add` snapshots the current `conda`/`venv` env and the daemon restores it at execution time. `conda activate myenv` then `gq add 'python train.py'` runs the job inside `myenv`
- **Any shell command** — `python train.py`, `bash run.sh`, anything

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/wangyicyc/gpu-queue/main/gq -o ~/.local/bin/gq
chmod +x ~/.local/bin/gq

# Ensure ~/.local/bin is on PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

gq --help
```

### Usage

`gq` is a daemon model: you keep a `gq watch` daemon running in tmux, and it watches the GPU; from another pane you `gq add` commands to the queue. The moment the GPU is idle, the daemon launches the next job, one after another, until the queue drains.

#### Quick start

```bash
# 1) Open tmux, start the daemon (all job output lands here)
tmux new -s gq
gq watch --poll 15

# 2) Open another tmux pane (Ctrl-B c), add jobs
gq add 'python train.py --seed 1'
gq add 'python train.py --seed 2'

# 3) Check status
gq list
```

`gq list` looks roughly like this:

```
[gq] running:
  3f1a  python train.py --seed 1   started 0:04:12 ago  [myenv]

[gq] queue (1 job):
  #1  a9c2  python train.py --seed 2  [myenv]

[gq] daemon: running (pid 12345)
```

The three blocks are: the **running job** (with elapsed time + env name), the **pending queue** (numbered in order), and the **daemon status** (running / stale pid / not running).

#### A job's lifecycle

1. **`gq add`** — the command enters the pending queue in `queue.json`. It also **snapshots** your current shell environment (conda/venv, PATH, …) and working directory.
2. **Wait** — the daemon checks whether the GPU is idle every `--poll` seconds.
3. **Run** — once idle, the daemon pops the head job and runs it with the snapshot's environment restored (`Popen(env=...)`). stdout/stderr stream straight to the daemon's terminal.
4. **Finish** — when the job exits (0 = DONE, non-zero = FAILED), the daemon clears the running state and moves to the next job.
5. **Empty queue** — the daemon keeps polling, waiting for you to `gq add` more.

### Carry your conda / venv environment

This is `gq`'s key capability: the environment you had active when you `add` is the environment the job runs in — not the daemon's own environment.

```bash
# Activate the env you want
conda activate myenv          # or: source ~/.venvs/ml/bin/activate

# Then add — gq remembers this env automatically
gq add 'python train.py --seed 1'    # → runs inside myenv
```

Each job remembers **its own** environment from `add` time, so you can interleave jobs across different envs:

```bash
conda activate torch200 && gq add 'python train.py --seed 1'
conda activate torch210 && gq add 'python train.py --seed 2'
# the two jobs run inside torch200 / torch210 respectively
```

`gq list` shows the env name after each job (prefers `CONDA_DEFAULT_ENV`, else the venv dir name):

```
  #1  3f1a  python train.py --seed 1  [torch200]
  #2  a9c2  python train.py --seed 2  [torch210]
```

> The environment is a **snapshot** taken at `add` time. If the conda env's install path changes later, the job does not auto-update — this matches the "run in the env I had when I added it" semantics.
> The env snapshot (which may contain API keys) is stored under `~/.gpu-queue/`, readable only by you on this machine.

### Commands

| Command | Description |
|---------|-------------|
| `gq watch [--poll N]` | Start the daemon (default poll 15s; run in tmux) |
| `gq add 'cmd'` | Append a job (uses current dir as cwd, snapshots current env) |
| `gq list` | Show running job + pending queue + daemon status |
| `gq cancel <id>` | Remove a pending job by ID or unique prefix |
| `gq clear` | Clear all pending jobs (does not affect a running job) |

**`gq watch [--poll N]`** — Starts the daemon. Refuses to start if a daemon is already running. On startup it performs crash recovery: if a previous crash left an orphaned job process, it kills that process group and clears the state. `--poll` sets the GPU-check interval in seconds (default 15).

**`gq add '<command>'`** — Appends any shell command to the queue tail. The command runs with the **current directory** as its working directory and a **snapshot of the current environment**. e.g. `gq add 'bash run.sh'`, `gq add 'python -m foo.bar --x 1'`.

**`gq list`** — Three blocks: the running job (with elapsed time + env name), the pending queue (numbered in order), and the daemon status (running / stale pid / not running).

**`gq cancel <id>`** — Remove a **pending** job by full ID or **unique prefix**. A running job cannot be cancelled this way — Ctrl-C the daemon instead. If a prefix matches multiple jobs, it lists them and removes nothing (so you can give a more specific prefix).

**`gq clear`** — Clears all pending jobs; a running job is unaffected.

### Common scenarios

**Run a hyperparameter sweep:**

```bash
for seed in 1 2 3 4 5; do
  gq add "python train.py --seed $seed"
done
gq list    # confirm all 5 are queued
```

**Cancel a mis-queued job:**

```bash
gq list             # see #2 is a9c2, want to drop it
gq cancel a9c2      # or by prefix: gq cancel a9
```

**After a daemon crash / reboot:** just run `gq watch` again. It reaps any orphaned process from the previous run and resumes the remaining queue.

**Switch envs for a new job:** just `conda activate otherenv` and `gq add`; the new job carries the new env, while jobs already in the queue are unaffected.

### Idle detection

`nvidia-smi pmon -s um -c 1` lists all GPU-occupying processes; each PID's owner is checked via `/proc/<pid>`. No process owned by you → idle. `nvidia-smi` failure → treated as busy (safe default).

### Tests

```bash
python -m pytest tests/ -v    # 38 tests
```

### License

MIT
