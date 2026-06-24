# HF cache refresh CronJob.
#
# Downloads the curated model set (modules/hf-cache/models.txt) from the
# HuggingFace Hub and publishes a symlink-free copy to the shared S3 bucket,
# which the mount DaemonSet then serves to runners. This is the only writer.
#
# Publishes the curated model set to this cluster's own bucket
# (s3://__BUCKET__/hub). Only writer for that bucket.
#
# Placeholders substituted by deploy.sh: __NAMESPACE__ __BUCKET__ __REGION__
# __RCLONE_IMAGE__ __SCHEDULE__
apiVersion: batch/v1
kind: CronJob
metadata:
  name: hf-cache-refresh
  namespace: __NAMESPACE__
  labels:
    osdc.io/module: hf-cache
    app.kubernetes.io/name: hf-cache
    app.kubernetes.io/component: refresh
spec:
  schedule: "__SCHEDULE__"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 600
  jobTemplate:
    metadata:
      labels:
        osdc.io/module: hf-cache
        app.kubernetes.io/name: hf-cache
        app.kubernetes.io/component: refresh
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 21600  # 6h
      template:
        metadata:
          labels:
            app: hf-cache-refresh
            osdc.io/module: hf-cache
        spec:
          serviceAccountName: hf-cache-refresh
          restartPolicy: OnFailure
          # Run on the runner fleet; tolerate its taints.
          nodeSelector:
            workload-type: github-runner
          tolerations:
            - key: node-fleet
              operator: Exists
              effect: NoSchedule
            - key: instance-type
              operator: Exists
              effect: NoSchedule
          containers:
            - name: refresh
              image: __RCLONE_IMAGE__
              command:
                - /bin/sh
                - -c
                - |
                  set -eu
                  apk add --no-cache python3 py3-pip >/dev/null
                  pip install --no-cache-dir --break-system-packages \
                    'huggingface_hub>=0.24' >/dev/null
                  exec python3 /scripts/refresh_cache.py \
                    --models /config/models.txt \
                    --bucket "$HF_CACHE_BUCKET" \
                    --region "$AWS_REGION" \
                    --cache-dir /work/hub \
                    --prefix hub
              env:
                - name: HF_CACHE_BUCKET
                  value: "__BUCKET__"
                - name: AWS_REGION
                  value: "__REGION__"
                # Token for gated/private models. Optional — public models work
                # without it. Create with:
                #   kubectl create secret generic hf-cache-token \
                #     -n __NAMESPACE__ --from-literal=token=hf_xxx
                - name: HF_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: hf-cache-token
                      key: token
                      optional: true
              resources:
                requests:
                  cpu: "1"
                  memory: 4Gi
                  ephemeral-storage: 50Gi
                limits:
                  cpu: "4"
                  memory: 8Gi
                  ephemeral-storage: 200Gi
              volumeMounts:
                - name: scripts
                  mountPath: /scripts
                - name: models
                  mountPath: /config
                - name: work
                  mountPath: /work
          volumes:
            - name: scripts
              configMap:
                name: hf-cache-refresh-scripts
            - name: models
              configMap:
                name: hf-cache-models
            - name: work
              emptyDir:
                sizeLimit: 200Gi
