# gq — GPU Task Queue

**[English](#english) | [中文](#中文)**

---

## 中文

一个轻量的本地 GPU 任务队列工具：你把命令丢进队列，它盯着 GPU，一旦空闲就自动跑下一个，跑完一个接一个。适合单机跑一串实验（不同 seed、不同超参）时不用手动等。

### 特点

- **TUI 可视化面板**：敲 `gq`（不带子命令）进入全屏 TUI，htop 式 GPU 占用条 + 方向键选择操作 + 上下文敏感底部提示。Add 对话框内嵌 bash（vim 式 exec/submit 模式切换，Ctrl-S 切换，颜色区分），支持 cd/tab 补全/试跑命令，F5 或 Enter 提交
- **多卡并行**：多张卡上可同时跑多个任务，每个任务用 `--gpus N` 声明要几张卡，gq 自动挑空闲卡分配并注入 `CUDA_VISIBLE_DEVICES`
- **自动带上你的环境**：`gq add` 时会快照当前 `conda`/`venv` 环境，daemon 执行时原样还原。`conda activate myenv` 后再 `gq add 'python train.py'`，任务就在 `myenv` 里跑
- **空闲检测按"你的进程"判断**：不是看 GPU 利用率数字，而是看 `nvidia-smi` 里有没有**当前用户**的进程在占 GPU。你的进程结束 = 空闲 = 拖下一个任务，比显存阈值更准
- **安全**：fcntl 文件锁防并发写坏；Ctrl-C 一次优雅停、连按两次强制杀当前任务；daemon 崩溃重启能识别并清理孤儿进程；killpg 三重条件守卫，绝不会误杀用户会话
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

# 可选：TUI 的 Add 功能需要 pyte（watch/命令行不需要）
pip install pyte
```

> 也可以直接 `git clone` 后 `cp gq ~/.local/bin/gq && chmod +x ~/.local/bin/gq`。

### 用法

`gq` 的主要交互方式是 **TUI 可视化面板**——敲 `gq`（不带子命令）进入全屏面板，用方向键选操作、回车执行，不用打命令名。命令行子命令（`gq add`/`gq list`/`gq stop` 等）作为可选的快捷方式。

> daemon 是前台常驻进程——关掉它所在终端，daemon 就会被杀。**tmux 不是必须的**：你开着终端不关就行；只有想关终端/断 SSH 后让任务继续跑，才需要 tmux（或 `nohup`）。

#### 快速开始

```bash
# 1) 起常驻 daemon（所有任务摘要打到这里）
gq watch --poll 15

# 2) 另开一个终端，敲 gq 进入 TUI 面板
gq

# 3) 在 TUI 里：上下选 Add job → 回车 → 在嵌入式 bash 里打命令 → Ctrl-S 切到提交模式 → Enter 提交
#    或直接按 F5 提交（不用切模式）
```

TUI 面板长这样：

```
    gq  1 GPUs (1 idle)  0 running  0 queued   14:32:07
    ----------------------------------------------------------------------
    GPU 0  ░░░░░░░░░░░░   0%

    ▸ Add job
      Stop running job  (none running)
      Open log  (none running)
      Cancel queued job  (queue empty)
      Clear queue
      Quit
 ↑↓ select  Enter confirm  q quit  |  Add: compose in bash, F5 submit, Esc cancel
```

- 顶部：GPU 占用条（彩色，每 2 秒刷新）+ 运行中/队列数量
- 中间：操作列表（上下方向键选，回车执行）
- 底部：上下文敏感提示（光标在哪个命令就提示哪个）

**Add job 对话框**（选中 Add job 回车后展开）：
- 嵌入式 bash，可以 `cd`、Tab 补全、试跑命令
- **EXEC 模式**（默认，绿色）：Enter 执行 bash 命令（cd/source/试跑）
- **SUBMIT 模式**（Ctrl-S 切换，黄色）：Enter 提交命令给队列（不执行）
- **F5** 始终提交（不管模式）
- **Esc** 取消退出
- **Ctrl-C** 中断试跑的命令

### 命令行用法（可选）

如果你更喜欢命令行，所有操作也都有子命令：

```bash
gq add 'python train.py --seed 1'          # 加任务（默认单卡）
gq add --gpus 4 'torchrun --nproc_per_node=4 train.py'  # 多卡
gq list                                     # 看状态
gq stop <id>                                # 停运行中任务
gq cancel <id>                              # 取消排队任务
gq clear                                    # 清空队列
```

`gq list` 大致长这样：

```
[gq] running:
  3f1a  python train.py --seed 1  GPU 0   started 0:04:12 ago  [myenv]

[gq] queue (1 job):
  #1  a9c2  python train.py --seed 2  [myenv]

[gq] daemon: running (pid 12345)
```

三段分别是：**正在跑的任务**（含已运行时长 + 环境名）、**待跑队列**（按顺序编号）、**daemon 状态**（running / stale pid / not running）。

#### 多卡任务

给任务指定要几张卡，gq 自动挑空闲卡分配：

```bash
gq add --gpus 4 'torchrun --nproc_per_node=4 train.py'
# gq 挑 4 张空闲卡，注入 CUDA_VISIBLE_DEVICES=0,2,5,7 后跑
# 多个任务可同时在不同卡上并行跑

gq add 'python eval.py'    # 不指定 --gpus → 默认单卡
```

#### 一个任务的生命周期

1. **Add** —— 命令进入 `queue.json` 待跑队列。同时**快照**你当前的 shell 环境（conda/venv、PATH 等）和工作目录。
2. **等待** —— daemon 每 `--poll` 秒检查一次 GPU 是否空闲。
3. **执行** —— 空闲后，daemon 从队首弹出任务，用快照的环境还原后跑（`Popen(env=...)`）。stdout/stderr 写到 `~/.gpu-queue/logs/<id>.log`（watch 终端只打印摘要）。
4. **完成** —— 任务结束（exit 0 = DONE，非 0 = FAILED），daemon 清掉运行状态，继续拖下一个。
5. **队列空** —— daemon 继续轮询，等你加新任务。

### 环境捕获（可选阅读）

`gq` 的关键能力：你 `add` 时激活了什么环境，任务就在什么环境里跑——而不是 daemon 自己的环境。

```bash
conda activate myenv          # 或 source ~/.venvs/ml/bin/activate
gq add 'python train.py --seed 1'    # → 会在 myenv 里跑
```

每个任务**各自**记住自己 `add` 时的环境，可以混排不同环境：

```bash
conda activate torch200 && gq add 'python train.py --seed 1'
conda activate torch210 && gq add 'python train.py --seed 2'
```

`gq list` 会在每个任务后显示环境名（优先 `CONDA_DEFAULT_ENV`，否则 venv 目录名）。

> 环境是 `add` 时刻的**快照**。之后 conda 环境路径变了不会自动更新。
> 环境快照（可能含 API key 等）会存到 `~/.gpu-queue/` 下，仅本机本用户可读。

### 命令详解

| 命令 | 作用 |
|------|------|
| `gq watch [--poll N]` | 启动 daemon（默认 15 秒轮询） |
| `gq add [--gpus N] 'cmd'` | 追加任务（--gpus N 指定要几张卡，默认 1） |
| `gq list` | 查看运行中的任务 + 待跑队列 + daemon 状态 |
| `gq cancel <id>` | 按 ID 或唯一前缀移除排队中的任务 |
| `gq clear` | 清空所有待跑任务（不影响正在跑的） |
| `gq stop <id>` | 停掉指定运行中的任务（daemon 继续接下一个） |

**`gq watch [--poll N]`**　启动守护进程。若已有 daemon 在跑会拒绝启动。启动时做崩溃恢复：发现上次崩溃留下的孤儿任务进程会杀掉它的进程组再清状态。`--poll` 控制检查 GPU 的间隔（秒），默认 15。

**`gq add [--gpus N] '<command>'`**　把任意 shell 命令追加到队尾。命令用**当前目录**作为工作目录，并**快照当前环境**。`--gpus N` 指定要几张卡（默认 1）：gq 会挑 N 张空闲卡，执行时注入 `CUDA_VISIBLE_DEVICES=<那些卡>`（如 `CUDA_VISIBLE_DEVICES=0,2,5,7`），命令本身不用改（`--nproc_per_node=N` 跟 `--gpus N` 对上即可）。不指定 `--gpus` 时默认单卡，会提示一句。例：`gq add --gpus 4 'torchrun --nproc_per_node=4 train.py'`、`gq add 'bash run.sh'`。

**`gq list`**　三段输出：运行中的任务（含已运行时长 + 环境名 + 占用卡，多卡显示如 `GPU 0,1`）、待跑队列（按顺序编号）、daemon 状态（running / stale pid / not running）。

**`gq cancel <id>`**　按完整 ID 或**唯一前缀**移除一个**排队中**的任务。正在跑的任务用 `gq stop <id>` 停（daemon 继续跑下一个）。前缀匹配多个任务时会列出所有匹配项并拒绝移除（让你写更具体的前缀）。

**`gq clear`**　清空所有待跑任务，正在跑的不受影响。

**`gq stop <id>`**　停掉**指定运行中**的任务（SIGKILL 其进程组）。必须给一个 `<id>`（运行中任务的 ID 或唯一前缀）；不给 id 会报错。可在任意窗口执行，不用切到 daemon 终端按 Ctrl-C。daemon 不受影响，会接着跑队列里下一个任务。排队中的任务用 `cancel`，不是 `stop`。

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

**停掉跑错的任务（正在跑的那个）：**

```bash
gq list              # 看到正在跑的是 3f1a，想停掉它
gq stop 3f1a         # 在任意窗口执行，立即杀掉，daemon 继续接下一个
```

**多卡并行跑：**

```bash
gq add --gpus 4 'torchrun --nproc_per_node=4 train.py --seed 1'
gq add --gpus 2 'torchrun --nproc_per_node=2 train.py --seed 2'
gq add --gpus 1 'python eval.py'
# gq 会同时把任务分配到空闲卡上并行跑，卡用满就排队
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
- `logs/` —— 每个任务的 stdout/stderr 日志（`<id>.log`）

两个文件都受 `fcntl.flock` 保护，CLI 和 daemon 同时读写不会写坏。文件损坏会自动重置并告警，不会崩溃。

### Shell 补全（可选）

`gq` 带 bash 补全：`gq <Tab>` 补全子命令，`gq add train<Tab>` 补全文件名，`gq cancel <Tab>` 补全队列里的任务 ID。

```bash
# 装到 bash-completion 自动加载目录（无需改 .bashrc）
mkdir -p ~/.local/share/bash-completion/completions
cp completions/gq.bash ~/.local/share/bash-completion/completions/gq

# 开个新终端即生效；或当场 source：
. ~/.local/share/bash-completion/completions/gq
```

> 需要 bash-completion（大多数发行版默认装了）。补全脚本在仓库的 `completions/gq.bash`，clone 后即可用。
>
> **`gq add` 补全文件名时不带引号。** bash 不会在单引号内部触发补全，所以 `gq add 'python eval<Tab>'` 补不出来。简单命令直接不引号：`gq add python eval<Tab>` → `eval.py`。带多参数的命令需要引号时，先把文件名补全再套引号：`gq add train<Tab>` → 改成 `gq add 'python train.py --seed 1'`。

### 测试

```bash
git clone https://github.com/wangyicyc/gpu-queue.git
cd gpu-queue
python -m pytest tests/ -v
```

96 个测试，覆盖 GPU 检测、文件锁、并发安全、命令、daemon 循环、信号处理、崩溃恢复、多卡调度、TUI 逻辑、killpg 安全。

### 限制

- 多卡并行：gq 按卡数自动分配，一次可在多张卡上并行跑多个任务。不支持多用户公平调度、集群（那是 Slurm 的活）
- 任务输出写到 `~/.gpu-queue/logs/<id>.log`（watch 终端只打印摘要，TUI 里 Open log 可查看）

### 许可证

MIT

---

## English

A lightweight local GPU job queue: throw shell commands into a queue, and `gq` watches the GPU — as soon as it's idle, it launches the next job automatically, one after another. Handy for running a sweep of experiments on a single workstation without babysitting each run.

### Features

- **TUI visualization panel** — typing bare `gq` opens a full-screen TUI with htop-style GPU bars, arrow-key operation selection, and context-sensitive bottom hints. The Add dialog embeds a real bash (vim-style exec/submit mode switch via Ctrl-S, color-coded), supporting cd/tab-completion/test-run; F5 or Enter to submit
- **Multi-GPU parallel** — multiple jobs run at once across cards; each job declares `--gpus N`, gq picks N idle cards and injects `CUDA_VISIBLE_DEVICES`
- **Carries your environment** — `gq add` snapshots the current `conda`/`venv` env and the daemon restores it at execution time. `conda activate myenv` then `gq add 'python train.py'` runs the job inside `myenv`
- **Idle = "your process is gone"** — instead of GPU utilization thresholds, it checks `nvidia-smi` for any process owned by the **current user**. Your process ends → idle → next job runs. More accurate than memory thresholds.
- **Safe** — `fcntl` file locks prevent corruption; Ctrl-C once = graceful, twice = force-kill; crash recovery detects and reaps orphaned subprocesses; killpg three-way guard never kills the user's session
- **Any shell command** — `python train.py`, `bash run.sh`, anything

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/wangyicyc/gpu-queue/main/gq -o ~/.local/bin/gq
chmod +x ~/.local/bin/gq

# Ensure ~/.local/bin is on PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

gq --help

# Optional: TUI Add requires pyte (watch/CLI work without it)
pip install pyte
```

### Usage

The primary way to use `gq` is the **TUI visualization panel** — typing bare `gq` opens a full-screen panel where you select operations with arrow keys and Enter, no command typing needed. CLI subcommands (`gq add`/`gq list`/`gq stop` etc.) are available as optional shortcuts.

> The daemon is a foreground process — closing its terminal kills it. **tmux is not required**: just keep the terminal open; use tmux (or `nohup`) only if you want jobs to survive closing the terminal / dropping an SSH session.

#### Quick start

```bash
# 1) Start the daemon (all job summaries land here)
gq watch --poll 15

# 2) Open another terminal, type gq to enter the TUI panel
gq

# 3) In the TUI: arrow keys to select Add job → Enter → type command in the
#    embedded bash → Ctrl-S to switch to submit mode → Enter to submit
#    Or just press F5 to submit (no mode switch needed)
```

The TUI panel looks like:

```
    gq  1 GPUs (1 idle)  0 running  0 queued   14:32:07
    ----------------------------------------------------------------------
    GPU 0  ░░░░░░░░░░░░   0%

    ▸ Add job
      Stop running job  (none running)
      Open log  (none running)
      Cancel queued job  (queue empty)
      Clear queue
      Quit
 ↑↓ select  Enter confirm  q quit  |  Add: compose in bash, F5 submit, Esc cancel
```

- Top: GPU utilization bars (colored, auto-refresh every 2s) + running/queued counts
- Middle: operations list (arrow keys to select, Enter to run)
- Bottom: context-sensitive hint (changes based on which row is focused)

**Add job dialog** (select Add job + Enter):
- Embedded bash — you can `cd`, Tab-complete, test-run commands
- **EXEC mode** (default, green): Enter executes bash commands (cd/source/test-run)
- **SUBMIT mode** (Ctrl-S to toggle, yellow): Enter submits the command to the queue
- **F5** always submits (regardless of mode)
- **Esc** cancels
- **Ctrl-C** interrupts a test-run command

### CLI usage (optional)

If you prefer the command line, all operations have subcommands:

```bash
gq add 'python train.py --seed 1'          # add a job (default single card)
gq add --gpus 4 'torchrun --nproc_per_node=4 train.py'  # multi-GPU
gq list                                     # show status
gq stop <id>                                # stop a running job
gq cancel <id>                              # cancel a queued job
gq clear                                    # clear the queue
```

`gq list` looks roughly like this:

```
[gq] running:
  3f1a  python train.py --seed 1  GPU 0   started 0:04:12 ago  [myenv]

[gq] queue (1 job):
  #1  a9c2  python train.py --seed 2  [myenv]

[gq] daemon: running (pid 12345)
```

The three blocks are: the **running job** (with elapsed time + env name), the **pending queue** (numbered in order), and the **daemon status** (running / stale pid / not running).

#### Multi-GPU

Tell a job how many cards it needs, and `gq` picks idle cards for it:

```bash
gq add --gpus 4 'torchrun --nproc_per_node=4 train.py'
# gq picks 4 idle cards, injects CUDA_VISIBLE_DEVICES=0,2,5,7, then runs

gq add 'python eval.py'    # no --gpus → defaults to a single card
```

#### A job's lifecycle

1. **Add** — the command enters the pending queue in `queue.json`. It also **snapshots** your current shell environment (conda/venv, PATH, …) and working directory.
2. **Wait** — the daemon checks whether the GPU is idle every `--poll` seconds.
3. **Run** — once idle, the daemon pops the head job and runs it with the snapshot's environment restored (`Popen(env=...)`). stdout/stderr go to `~/.gpu-queue/logs/<id>.log` (the watch terminal prints only summaries).
4. **Finish** — when the job exits (0 = DONE, non-zero = FAILED), the daemon clears the running state and moves to the next job.
5. **Empty queue** — the daemon keeps polling, waiting for you to add more.

### Environment capture (optional reading)

`gq`'s key capability: the environment you had active when you `add` is the environment the job runs in — not the daemon's own environment.

```bash
conda activate myenv          # or: source ~/.venvs/ml/bin/activate
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
| `gq watch [--poll N]` | Start the daemon (default poll 15s) |
| `gq add [--gpus N] 'cmd'` | Append a job (`--gpus N` sets how many cards, default 1) |
| `gq list` | Show running job + pending queue + daemon status |
| `gq cancel <id>` | Remove a pending job by ID or unique prefix |
| `gq clear` | Clear all pending jobs (does not affect a running job) |
| `gq stop <id>` | Stop a specific running job (daemon continues with the next) |

**`gq watch [--poll N]`** — Starts the daemon. Refuses to start if a daemon is already running. On startup it performs crash recovery: if a previous crash left an orphaned job process, it kills that process group and clears the state. `--poll` sets the GPU-check interval in seconds (default 15).

**`gq add [--gpus N] '<command>'`** — Appends any shell command to the queue tail. The command runs with the **current directory** as its working directory and a **snapshot of the current environment**. `--gpus N` sets how many cards the job needs (default 1): `gq` picks N idle cards and injects `CUDA_VISIBLE_DEVICES=<those cards>` at run time (e.g. `CUDA_VISIBLE_DEVICES=0,2,5,7`) — the command itself stays unchanged (just keep `--nproc_per_node=N` matching `--gpus N`). Without `--gpus` it defaults to a single card and prints a one-line notice. e.g. `gq add --gpus 4 'torchrun --nproc_per_node=4 train.py'`, `gq add 'bash run.sh'`.

**`gq list`** — Three blocks: the running job (with elapsed time + env name + cards used, multi-card shown like `GPU 0,1`), the pending queue (numbered in order), and the daemon status (running / stale pid / not running).

**`gq cancel <id>`** — Remove a **pending** job by full ID or **unique prefix**. A running job cannot be cancelled — use `gq stop <id>` to stop it (the daemon moves on to the next job). If a prefix matches multiple jobs, it lists them and removes nothing (so you can give a more specific prefix).

**`gq clear`** — Clears all pending jobs; a running job is unaffected.

**`gq stop <id>`** — Stops a **specific running** job (SIGKILLs its process group). You must pass an `<id>` (ID or unique prefix of a running job); running it with no id is an error. Run from any window — no need to switch to the daemon terminal and Ctrl-C. The daemon is unaffected and moves on to the next queued job. For a *pending* job, use `cancel`, not `stop`.

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

**Stop a misbehaving running job (the one currently running):**

```bash
gq list             # see 3f1a is running, want to stop it
gq stop 3f1a        # run from any window; kills it immediately, daemon moves on
```

**Run jobs in parallel across multiple cards:**

```bash
gq add --gpus 4 'torchrun --nproc_per_node=4 train.py --seed 1'
gq add --gpus 2 'torchrun --nproc_per_node=2 train.py --seed 2'
gq add --gpus 1 'python eval.py'
# gq dispatches jobs onto idle cards in parallel; once cards are full, the rest queue
```

**After a daemon crash / reboot:** just run `gq watch` again. It reaps any orphaned process from the previous run and resumes the remaining queue.

**Switch envs for a new job:** just `conda activate otherenv` and `gq add`; the new job carries the new env, while jobs already in the queue are unaffected.

### Idle detection

`nvidia-smi pmon -s um -c 1` lists all GPU-occupying processes; each PID's owner is checked via `/proc/<pid>`. No process owned by you → idle. `nvidia-smi` failure → treated as busy (safe default).

### Shell completion (optional)

`gq` ships a bash completion: `gq <Tab>` completes subcommands, `gq add train<Tab>` completes filenames, `gq cancel <Tab>` completes pending job IDs from the queue.

```bash
# Install to bash-completion's auto-load dir (no .bashrc edit needed)
mkdir -p ~/.local/share/bash-completion/completions
cp completions/gq.bash ~/.local/share/bash-completion/completions/gq

# A new shell picks it up; or source it now:
. ~/.local/share/bash-completion/completions/gq
```

> Requires bash-completion (installed by default on most distros). The completion script lives at `completions/gq.bash` in the repo — usable after `git clone`.
>
> **For `gq add` filename completion, don't use quotes.** bash doesn't trigger completion inside single quotes, so `gq add 'python eval<Tab>'` won't complete. For simple commands, skip the quotes: `gq add python eval<Tab>` → `eval.py`. For commands with multiple arguments that need quotes, complete the filename first then wrap: `gq add train<Tab>` → edit to `gq add 'python train.py --seed 1'`.

### Tests

```bash
python -m pytest tests/ -v    # 96 tests
```

### Limitations

- Multi-GPU parallelism: `gq` assigns jobs by card count and can run several jobs in parallel across multiple cards at once. No multi-user fair scheduling, no clustering (that's Slurm's job).
- Job output goes to `~/.gpu-queue/logs/<id>.log` (view via TUI Open log or `tail -f`); the watch terminal prints only summaries.

### License

MIT
