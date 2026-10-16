name: OSDC Drain Test {{RUN_KEY}}

on:
  pull_request:
    # Editing the PR body releases the probe and must not create another run.
    types: [opened]

permissions:
  contents: read
  pull-requests: read

jobs:
  drain-inflight:
    runs-on:
      group: meta-staging-aws-uw1
      labels: [c-mt-l-x86iamx-8-16]
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
                      body = json.load(response).get("body") or ""
              except urllib.error.HTTPError as error:
                  if error.code not in (429, 500, 502, 503, 504):
                      raise
                  print("Retrying PR read after transient HTTP error", error.code)
                  body = ""
              except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, ConnectionError):
                  print("Retrying PR read after network error")
                  body = ""
              if os.environ["RELEASE_MARKER"] in body:
                  print("Drain observed; in-flight job finishes normally")
                  break
              time.sleep(5)
          else:
              raise SystemExit("Controller did not release the in-flight job before its deadline")
          PY

  drain-held:
    needs: drain-inflight
    runs-on:
      group: meta-staging-aws-uw1
      labels: [c-mt-l-x86iamx-8-16]
    timeout-minutes: 5
    strategy:
      fail-fast: false
      matrix:
        probe: [1, 2, 3]
    container:
      image: python:3.12.9-slim
    steps:
      - name: Complete after undrain
        run: python -c 'print("Held job completed on uw1 after undrain")'
