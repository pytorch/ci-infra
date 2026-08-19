# nodepools-agent-sandbox

The dedicated Karpenter fleet for the AI agent sandbox (`modules/agent-sandbox`):
a single small CPU pool whose nodes boot with **gVisor (runsc)** available as a
containerd runtime handler. Thin shim over `modules/nodepools` — same pattern as
`nodepools-h100` / `nodepools-b200` — so the fleet only exists on clusters that
list this module, and can never reach prod through the shared nodepools defs.

## gVisor comes from a custom AMI, not userData

`packer/` builds an AMI from the EKS-optimized AL2023 image with `runsc` and
`containerd-shim-runsc-v1` baked in at a **pinned** gVisor release.

This used to be a userData script, which was wrong on a Karpenter-managed fleet
in three ways:

- it fetched the gVisor release **over the network on every scale-up**, and
  tracked `latest`, so two nodes launched a day apart could differ;
- it edited `/etc/containerd/config.toml`, which nodeadm **regenerates at every
  boot** anyway;
- it then ran `systemctl restart containerd` — *after* kubelet had started,
  racing anything already scheduled onto the node.

The runtime handler is instead registered declaratively. The AMI ships a nodeadm
NodeConfig at `/etc/eks/nodeadm.d/10-gvisor.yaml`; nodeadm merges everything in
that directory with the user-data config and writes `/etc/containerd/config.toml`
**before** starting containerd, so `runsc` is registered at containerd's first
start and never needs a restart:

```yaml
spec:
  containerd:
    config: |
      [plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
        runtime_type = "io.containerd.runsc.v1"
```

The key must be `io.containerd.cri.v1.runtime` — EKS AL2023 ships containerd v2.x
with `version = 3` config, and a containerd-1.x `io.containerd.grpc.v1.cri` stanza
is silently ignored, so runsc would simply never register.

## Build the AMI

Required before the fleet can launch a node — the def selects the AMI by tag, and
an unbuilt AMI means Karpenter silently never provisions (the module smoke tests
fail loudly for exactly this).

```bash
just build-agent-sandbox-ami <cluster>          # e.g. meta-staging-aws-ue1
just build-agent-sandbox-ami <cluster> -var gvisor_release=20260901
```

AMIs are regional: build once per cluster region. Existing nodes keep their
current AMI until recycled (`just recycle-nodes <cluster>`).

The build launches an instance with IMDSv2 required — an org SCP denies
`ec2:RunInstances` otherwise — and the resulting AMI is IMDSv2-only, which is what
keeps the node role out of reach of a compromised agent.

## Rebuild cadence (the trade-off)

Stock fleets track `alias: al2023@latest` and pick up AL2023 CVE fixes
automatically on node rotation. **This fleet does not.** It pins whatever
EKS-optimized AL2023 image was current at build time, so security updates land
only when the AMI is rebuilt.

Each rebuild re-resolves the base from the EKS SSM parameter, so rebuilding is
the whole refresh procedure. Worth automating on a schedule if this fleet outlives
the prototype.

An EKS version bump needs a rebuild too, and nothing enforces it at launch time:
the def selects on the `osdc.io/ami` tag only, so the `K8sVersion` recorded by the
build is ignored and Karpenter would happily keep launching pre-upgrade nodes. The
smoke tests fail on that skew (`test_ami_exists_in_region`) rather than the fleet
catching it — see the TODO in `defs/ai-sandbox.yaml` for the fail-closed fix.
