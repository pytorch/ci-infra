# Per-run sandbox agent Job. Rendered by deploy.sh's `run` helper (or a future
# GitHub Action) with the __PLACEHOLDER__ values substituted, then applied.
#
# Single-use by construction: restartPolicy Never + the entrypoint exits after
# one task, so isolation is per-task. The pod holds NO credentials — HTTPS_PROXY
# points at agent-vault (token injected on the wire) and Bedrock is reached via
# the sigv4 proxy. runtimeClassName gvisor pins it to the ai-sandbox fleet and
# runs it under runsc.
apiVersion: batch/v1
kind: Job
metadata:
  name: sandbox-agent-__RUN_ID__
  namespace: ai-sandbox
  labels:
    osdc.io/module: agent-sandbox
    app: sandbox-agent
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  activeDeadlineSeconds: __TIMEOUT__
  template:
    metadata:
      labels:
        app: sandbox-agent
        osdc.io/module: agent-sandbox
    spec:
      runtimeClassName: gvisor
      restartPolicy: Never
      serviceAccountName: sandbox-agent
      automountServiceAccountToken: false
      containers:
        - name: agent
          image: __AGENT_IMAGE__
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            allowPrivilegeEscalation: false
          env:
            - name: REPO
              value: "__REPO__"
            - name: REPO_REF
              value: "__REPO_REF__"
            - name: TASK
              value: "__TASK__"
            - name: BEDROCK_MODEL_ID
              value: "__BEDROCK_MODEL_ID__"
            - name: AWS_REGION
              value: "__AWS_REGION__"
            # Egress plumbing: HTTPS through agent-vault, Bedrock via sigv4 proxy.
            - name: HTTPS_PROXY
              value: "http://agent-vault.ai-sandbox.svc.cluster.local:14322"
            - name: https_proxy
              value: "http://agent-vault.ai-sandbox.svc.cluster.local:14322"
            - name: SIGV4_PROXY
              value: "sigv4-proxy.ai-sandbox.svc.cluster.local:8080"
            # Trust the agent-vault MITM CA for the injected-credential hosts.
            - name: CURL_CA_BUNDLE
              value: "/etc/agent-vault-ca/ca.crt"
            - name: GIT_SSL_CAINFO
              value: "/etc/agent-vault-ca/ca.crt"
          volumeMounts:
            - name: agent-vault-ca
              mountPath: /etc/agent-vault-ca
              readOnly: true
            - name: output
              mountPath: /output
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2"
              memory: "2Gi"
      volumes:
        - name: agent-vault-ca
          secret:
            secretName: agent-vault-ca
            items:
              - key: ca.crt
                path: ca.crt
        - name: output
          emptyDir: {}
