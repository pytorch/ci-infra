# `gpu-dev submit` smoke tests

Three tests that exercise `gpu-dev submit` end-to-end. Each test lives in its
own folder so you can `--runtime` it directly. Output files written by the
script are pulled back into the same folder via the post-run rsync.

> Requires `gpu-dev >= 0.5.19`. No Lambda update needed.

## 1. success — single T4 GPU, exit 0

```bash
cd tests/submit/success
gpu-dev submit --gpu-type t4 --gpus 1 --runtime ./ -- bash run.sh
echo $?    # 0
ls         # nvidia-info.txt, compute.txt, status.txt all created
```

## 2. fail — single T4 GPU, exit 7

Writes a partial file before exploding so you can confirm rsync still pulls
output on failure and the local exit code is the remote's.

```bash
cd tests/submit/fail
gpu-dev submit --gpu-type t4 --gpus 1 --runtime ./ -- bash run.sh
echo $?    # 7
ls         # step1.txt, step2.txt, gpus-before-fail.txt — but no step3.txt
```

## 3. multinode — 2x H100 nodes, exit 0

Reserves 16 H100s (= 2 nodes), verifies env vars + peer ssh + NCCL all_reduce
across the whole cluster via mpirun (orchestrated entirely from rank 0).

```bash
cd tests/submit/multinode
gpu-dev submit --gpu-type h100 --gpus 16 --runtime ./ -- bash run.sh
echo $?    # 0
cat multinode-env.txt resolved-ips.txt peer-ssh.txt nccl-all_reduce.log
```

## What each test proves

| Test       | Proves                                                                        |
|------------|-------------------------------------------------------------------------------|
| success    | reserve → rsync up → exec → rsync back → cancel → exit 0                      |
| fail       | exit code propagation; rsync-back still runs on non-zero exit; cancel fires   |
| multinode  | MULTINODE_* env vars; peer DNS / passwordless ssh; cross-node NCCL via mpirun |

After every run, `gpu-dev list` should show neither reservation — both auto-cancelled.
Use `--keep-alive` on any of them if you want to debug interactively afterward.

## Other submit flags (forwarded to `reserve`)

- `--hours N` — reservation lifetime ceiling (default 1.0)
- `--disk NAME` — attach a persistent disk to the master node
- `--no-persistent-disk` — skip persistent disk
- `--dockerfile PATH` — build a custom image from this Dockerfile
- `--dockerimage REF` — use a pre-built container image
- `--preserve-entrypoint` — keep the custom image's ENTRYPOINT (you must run sshd yourself for submit to work)
- `--timeout MINUTES` — wait-for-active timeout (default 1440 = 24h, since reservations may queue)
- `--no-pull` — skip the post-run sync-back
- `--keep-alive` — skip auto-cancel
