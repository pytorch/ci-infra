name: OSDC Drain Test {{RUN_KEY}}

"on":
  pull_request:
    # Editing the PR body releases the probe and must not create another run.
    types: [opened, synchronize]

permissions:
  contents: read
  pull-requests: read

jobs:
  drain-inflight:
    if: github.event.action == 'opened' && github.event.pull_request.head.repo.full_name == github.repository
    runs-on:
      group: {{RUNNER_GROUP}}
      labels: [{{PROBE_LABEL}}]
    timeout-minutes: 45
    container:
      image: python:3.12.9-slim
    steps:
      - name: Wait for drain
        env:
          PR_URL: ${{ github.event.pull_request.url }}
          GH_TOKEN: ${{ github.token }}
          RELEASE_MARKER: OSDC_DRAIN_RELEASE_{{RUN_KEY}}
        run: |
          python - <<'PY'
          import http.client
          import json
          import os
          import time
          import urllib.error
          import urllib.request

          deadline = time.monotonic() + 2400
          while time.monotonic() < deadline:
              request = urllib.request.Request(
                  os.environ["PR_URL"],
                  headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"]},
              )
              try:
                  with urllib.request.urlopen(request, timeout=10) as response:
                      parsed = json.load(response)
                      body = (parsed.get("body") if isinstance(parsed, dict) else "") or ""
              except urllib.error.HTTPError as error:
                  if error.code not in (429, 500, 502, 503, 504):
                      raise
                  print("Retrying PR read after transient HTTP error", error.code)
                  body = ""
              except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, ConnectionError, json.JSONDecodeError, AttributeError, TypeError):
                  print("Retrying PR read after network error")
                  body = ""
              if os.environ["RELEASE_MARKER"] in body.splitlines():
                  print("Drain observed; in-flight job finishes normally")
                  break
              time.sleep(5)
          else:
              raise SystemExit("Controller did not release the in-flight job before its deadline")
          PY

  drain-held:
    name: drain-held (${{ matrix.probe }})
    if: github.event.action == 'opened' && github.event.pull_request.head.repo.full_name == github.repository
    needs: drain-inflight
    runs-on:
      group: {{RUNNER_GROUP}}
      labels: [{{PROBE_LABEL}}]
    timeout-minutes: 15
    strategy:
      fail-fast: false
      matrix:
        probe: [1, 2, 3]
    container:
      image: python:3.12.9-slim
    steps:
      - name: Wait for controller release
        env:
          PR_URL: ${{ github.event.pull_request.url }}
          GH_TOKEN: ${{ github.token }}
          RELEASE_MARKER: OSDC_HELD_RELEASE_{{RUN_KEY}}_${{ matrix.probe }}
        run: |
          python - <<'PY'
          import http.client
          import json
          import os
          import time
          import urllib.error
          import urllib.request

          deadline = time.monotonic() + 840
          while time.monotonic() < deadline:
              request = urllib.request.Request(
                  os.environ["PR_URL"],
                  headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"]},
              )
              try:
                  with urllib.request.urlopen(request, timeout=10) as response:
                      parsed = json.load(response)
                      body = (parsed.get("body") if isinstance(parsed, dict) else "") or ""
              except urllib.error.HTTPError as error:
                  if error.code not in (429, 500, 502, 503, 504):
                      raise
                  print("Retrying PR read after transient HTTP error", error.code)
                  body = ""
              except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, ConnectionError, json.JSONDecodeError, AttributeError, TypeError):
                  print("Retrying PR read after network error")
                  body = ""
              if os.environ["RELEASE_MARKER"] in body.splitlines():
                  print("Placement verified; held job finishes normally")
                  break
              time.sleep(5)
          else:
              raise SystemExit("Controller did not verify and release the held job before its deadline")
          PY

      - name: Complete after undrain
        run: python -c 'print("Held job completed after undrain and placement verification")'

  # New traffic wakes the original queue after capacity restoration
  # (https://github.com/actions/actions-runner-controller/issues/4715).
  drain-wakeup:
    if: github.event.action == 'synchronize' && github.event.pull_request.head.repo.full_name == github.repository
    runs-on:
      group: {{RUNNER_GROUP}}
      labels: [{{PROBE_LABEL}}]
    timeout-minutes: 15
    container:
      image: python:3.12.9-slim
    steps:
      - name: Wake original queued jobs
        run: |
          python -c 'print("ARC #4715 wakeup")'
